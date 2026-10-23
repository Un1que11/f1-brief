"""Stories in any language (F31): a story's title and summary and its updates' `what_changed`
in a language other than the one they are written in.

The texts are stored once, in the language story detection wrote them in (see `stories`).
Another language is generated on demand by `claude -p` and cached in the state file, so each
text is generated once per language. The model gets one story's stored texts and the sourced
updates asked for (headline, outlet, link, date, `what_changed`) and nothing else, through a
template per language (`prompts/<language>/story_texts.md`); each update's text is written from
that update alone. The answer is checked before anything is cached: every text asked for, no
other ids, nothing empty. The source URL is never generated: it is the stored one.

When a text cannot be had in the language asked for (no model allowed, or the model failed),
the stored text is used, and `Localized.language` says which language each text is in.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from f1brief import digest
from f1brief.digest import GenerationError, TextModel
from f1brief.send_log import StateError
from f1brief.stories import Story, StoryError, StoryStore, StoryText, StoryUpdate

TEMPLATE = "story_texts"  # prompts/<language>/story_texts.md
SYSTEM_PROMPT = (
    "You write the texts of an F1 storyline in another language strictly from the document "
    "in the user's message. You have no tools and use no other knowledge. You answer with "
    "JSON only."
)

_FENCE = re.compile(r"```(?:json)?\s*\n(.*)\n\s*```", re.DOTALL)


class StoryTextError(Exception):
    """The model's answer is not the JSON the template asks for."""


@dataclass(frozen=True)
class Localized:
    """A story's texts for one language. `language` is the language of the title and summary,
    `update_languages` that of each update's text: the one asked for, or the stored one when
    no text in it could be had."""

    title: str
    summary: str
    language: str
    what_changed: dict[int, str] = field(default_factory=dict)
    update_languages: dict[int, str] = field(default_factory=dict)


def _date(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def build_document(
    story: Story, updates: Sequence[StoryUpdate], first: StoryUpdate | None, story_text: bool
) -> dict[str, Any]:
    """What the model gets: the story's stored texts (when its title and summary are asked
    for, together with the update it was created from) and the updates asked for."""
    document: dict[str, Any] = {}
    if story_text:
        document["story"] = {
            "language": story.language,
            "title": story.title,
            "summary": story.summary,
            "source": _source(first) if first else None,
        }
    document["updates"] = [_source(u) for u in updates]
    return document


def _source(entry: StoryUpdate) -> dict[str, Any]:
    update = entry.update
    return {
        "id": entry.id,
        "language": update.language,
        "headline": update.headline,
        "source": update.source,
        "url": update.url,
        "published": _date(update.published_at),
        "what_changed": update.what_changed,
    }


def build_prompt(document: dict[str, Any], language: str) -> str:
    return digest.build_prompt(document, TEMPLATE, language)


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StoryTextError(f"{key} is missing")
    return value.strip()


def parse_answer(
    text: str, story_text: bool, update_ids: Sequence[int]
) -> tuple[StoryText | None, dict[int, str]]:
    """The texts in the model's answer: the title and summary (if asked for) and each
    update's `what_changed` by id. Raises StoryTextError unless exactly the texts asked for
    are there (a code fence around the JSON is tolerated)."""
    fenced = _FENCE.search(text)
    body = fenced.group(1) if fenced else text.strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise StoryTextError(f"the answer is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise StoryTextError("the answer is not a JSON object")
    story = StoryText(_text(data, "title"), _text(data, "summary")) if story_text else None
    raw = data.get("updates", [])
    if not isinstance(raw, list):
        raise StoryTextError('"updates" is not a list')
    texts: dict[int, str] = {}
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), int):
            raise StoryTextError(f"an update is an object with an integer id, not {entry!r}")
        update_id = entry["id"]
        if update_id not in update_ids:
            raise StoryTextError(f"update {update_id} was not asked for")
        if update_id in texts:
            raise StoryTextError(f"update {update_id} is in the answer twice")
        texts[update_id] = _text(entry, "what_changed")
    if missing := [i for i in update_ids if i not in texts]:
        raise StoryTextError(f"no text for updates {', '.join(map(str, missing))}")
    return story, texts


def localize(
    store: StoryStore,
    story: Story,
    updates: Sequence[StoryUpdate],
    language: str,
    model: TextModel | None,
    now: datetime,
) -> tuple[Localized, str | None]:
    """The story's title and summary and the `what_changed` of `updates` (updates of this
    story) in `language`: stored, cached, or generated with `model` and cached, in one call
    for whatever is missing. Without a model nothing is generated. Where a text cannot be had
    in `language`, the stored one stands in. Returns the texts and, when generating failed,
    why (nothing is cached then)."""
    cached = store.story_text(story.id, language)
    missing = [u for u in updates if store.update_text(u.id, language) is None]
    error: str | None = None
    if (cached is None or missing) and model is not None:
        timeline = store.timeline(story.id)
        document = build_document(story, missing, timeline[0] if timeline else None, cached is None)
        try:
            answer = model(build_prompt(document, language))
            text, generated = parse_answer(answer, cached is None, [u.id for u in missing])
            store.cache_texts(story.id, language, text, generated, now)
        except (GenerationError, StoryTextError, StoryError, StateError) as exc:
            error = f"story {story.id}: no {language} texts: {exc}"
        else:
            cached = cached or text
    title, summary, title_language = (
        (cached.title, cached.summary, language)
        if cached is not None
        else (story.title, story.summary, story.language)
    )
    result = Localized(title=title, summary=summary, language=title_language)
    for entry in updates:
        text = store.update_text(entry.id, language)
        if text is None:
            result.what_changed[entry.id] = entry.update.what_changed
            result.update_languages[entry.id] = entry.update.language
        else:
            result.what_changed[entry.id] = text
            result.update_languages[entry.id] = language
    return result, error


def default_model(model: str | None = None) -> TextModel:
    return digest.ClaudeCli(model=model, system_prompt=SYSTEM_PROMPT)

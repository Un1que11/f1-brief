"""Story detection (F22): which news items start or continue a hot story.

Hot items are news about the favourites or the entities of followed stories that touch
contracts, seats, transfers, team principals, regulations or penalties (see "Hot stories" in
CLAUDE.md). Only the hot items that detection has not checked before go to `claude -p`,
together with the stories that are not resolved. It answers with JSON, one decision per item:

    {"decisions": [
        {"url": ..., "action": "attach", "story": <slug>, "what_changed_ru": ...,
         "status": ... (optional)},
        {"url": ..., "action": "new", "slug": ..., "title_ru": ..., "summary_ru": ...,
         "status": ..., "entities": [{"kind": ..., "ref": ...}], "what_changed_ru": ...},
        {"url": ..., "action": "ignore"}
    ]}

`url` must be the link of one of the items sent: it is the source of the story or update, and
a decision without one is rejected. The headline, outlet and date come from the item, never
from the model. The answer is checked against the store first (`plan`, which writes nothing),
then stored in one transaction (`apply`). A status only moves forward along the story's
lifecycle (`stories.can_move`): an attachment that would move it back keeps its sourced update
but not the status, and a new story never starts resolved. The items answered are marked
checked, rejected ones included, so a bad answer is reported once instead of being asked again
every day; items the model left out stay unchecked and go to the next run.
"""

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from f1brief import digest, news
from f1brief.digest import TextModel
from f1brief.news import NewsItem
from f1brief.stories import (
    INITIAL_STATUSES,
    Entity,
    EntityKind,
    Story,
    StoryError,
    StoryStatus,
    StoryStore,
    Update,
    can_move,
    check_story,
    check_update,
)

DETECTION_TEMPLATE = "stories"  # prompts/stories.md, the same for every language
DETECTION_SYSTEM_PROMPT = (
    "You sort F1 news items into developing storylines strictly from the document in the "
    "user's message. You have no tools and use no other knowledge. You answer with JSON only."
)

# News topics (`news.TOPICS`) that make an item hot; the contracts topic covers seats and moves.
HOT_TOPICS = frozenset({"contracts", "regulations", "penalties"})
# Hot wording the news topics do not cover.
EXTRA_TERMS = (
    "team principal",
    "team principals",
    "team boss",
    "team chief",
    "transfer",
    "transfers",
)
_EXTRA = news.term_pattern(EXTRA_TERMS)

# The stories detection may attach items to; a resolved story is closed.
OPEN_STATUSES = (
    StoryStatus.RUMOR,
    StoryStatus.DEVELOPING,
    StoryStatus.CONFIRMED,
    StoryStatus.DENIED,
)
# How many of a story's latest updates the model sees, to tell what is new.
RECENT_UPDATES = 3

_FENCE = re.compile(r"```(?:json)?\s*\n(.*)\n\s*```", re.DOTALL)


class DetectionError(Exception):
    """The model's answer is not the JSON detection asks for."""


def entity_terms(entities: Iterable[Entity]) -> list[str]:
    """How news names the entities: a team by its whole ref ("red_bull" -> "red bull"), a
    driver or a person by the last word of theirs ("max_verstappen" -> "verstappen")."""
    terms: list[str] = []
    for entity in entities:
        words = entity.ref.replace("_", " ").split()
        if not words:
            continue
        term = " ".join(words) if entity.kind == EntityKind.TEAM else words[-1]
        if term not in terms:
            terms.append(term)
    return terms


def watch_terms(store: StoryStore, favourites: Iterable[str]) -> list[str]:
    """The favourites' names plus the entities of every followed story."""
    terms = [t for t in favourites if t.strip()]
    for story in store.followed():
        terms += [t for t in entity_terms(store.entities(story.id)) if t not in terms]
    return terms


def is_hot(item: NewsItem, about: re.Pattern[str]) -> bool:
    text = f"{item.title} {item.summary}"
    return bool(about.search(text)) and bool(item.topics & HOT_TOPICS or _EXTRA.search(text))


def hot_items(items: Iterable[NewsItem], watch: Iterable[str]) -> list[NewsItem]:
    """The items about one of `watch` (names, matched as whole words) on a hot topic."""
    terms = [t for t in watch if t.strip()]
    if not terms:
        return []
    about = news.term_pattern(terms)
    return [i for i in items if is_hot(i, about)]


def pending(store: StoryStore, items: Iterable[NewsItem], watch: Iterable[str]) -> list[NewsItem]:
    """The hot items detection has not checked yet, each link once."""
    hot = list({i.link: i for i in hot_items(items, watch)}.values())
    fresh = set(store.unchecked([i.link for i in hot]))
    return [i for i in hot if i.link in fresh]


def build_document(store: StoryStore, items: Sequence[NewsItem]) -> dict[str, Any]:
    """What the model gets: the open stories and the new items, nothing else."""
    stories = []
    for story in store.stories(OPEN_STATUSES):
        recent = store.timeline(story.id)[-RECENT_UPDATES:]
        stories.append(
            {
                "slug": story.slug,
                "title_ru": story.title_ru,
                "summary_ru": story.summary_ru,
                "status": story.status.value,
                "entities": [
                    {"kind": e.kind.value, "ref": e.ref} for e in store.entities(story.id)
                ],
                "recent_updates": [
                    {
                        "headline": u.update.headline,
                        "published_at": (
                            u.update.published_at.isoformat() if u.update.published_at else None
                        ),
                        "what_changed_ru": u.update.what_changed_ru,
                    }
                    for u in recent
                ],
            }
        )
    return {
        "stories": stories,
        "items": [
            {
                "url": i.link,
                "title": i.title,
                "summary": i.summary,
                "published": i.published.isoformat() if i.published else None,
                "sources": list(i.sources),
                "topics": sorted(i.topics),
            }
            for i in items
        ],
    }


def build_prompt(store: StoryStore, items: Sequence[NewsItem]) -> str:
    return digest.build_prompt(build_document(store, items), DETECTION_TEMPLATE, None)


def parse_answer(text: str) -> list[Any]:
    """The decisions in the model's answer (a code fence around the JSON is tolerated)."""
    fenced = _FENCE.search(text)
    body = fenced.group(1) if fenced else text.strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DetectionError(f"the answer is not JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        raise DetectionError('the answer has no "decisions" list')
    return data["decisions"]


@dataclass(frozen=True)
class NewStory:
    slug: str
    title_ru: str
    summary_ru: str
    status: StoryStatus
    entities: tuple[Entity, ...]
    first_update: Update


@dataclass(frozen=True)
class Attachment:
    story_slug: str
    update: Update
    status: StoryStatus | None


@dataclass(frozen=True)
class Rejection:
    url: str | None  # None: the decision named no source
    reason: str


@dataclass(frozen=True)
class Plan:
    """What an answer would store. `checked` are the links of the items answered.
    `kept_status` are the attachments stored without the status the model gave, which the
    story's lifecycle does not allow."""

    new_stories: tuple[NewStory, ...]
    attachments: tuple[Attachment, ...]
    ignored: tuple[str, ...]
    rejected: tuple[Rejection, ...]
    unanswered: tuple[str, ...]
    checked: tuple[str, ...]
    kept_status: tuple[Rejection, ...] = ()


def _text(decision: dict[str, Any], key: str) -> str:
    value = decision.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is missing")
    return value.strip()


def _status(value: Any) -> StoryStatus:
    if not isinstance(value, str):
        raise ValueError(f"status must be one of {', '.join(StoryStatus)}, not {value!r}")
    try:
        return StoryStatus(value)
    except ValueError:
        raise ValueError(f"status must be one of {', '.join(StoryStatus)}, not {value!r}") from None


def _update(decision: dict[str, Any], item: NewsItem) -> Update:
    update = Update(
        headline=item.title,
        url=item.link,
        source=item.sources[0] if item.sources else "",
        what_changed_ru=_text(decision, "what_changed_ru"),
        published_at=item.published,
    )
    check_update(update)
    return update


def _entities(value: Any) -> tuple[Entity, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("a story needs at least one entity")
    entities: list[Entity] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError(f"an entity is an object with kind and ref, not {raw!r}")
        kind = raw.get("kind")
        if kind not in tuple(EntityKind):
            raise ValueError(f"entity kind must be one of {', '.join(EntityKind)}, not {kind!r}")
        entity = Entity(EntityKind(kind), _text(raw, "ref"))
        if entity not in entities:
            entities.append(entity)
    return tuple(entities)


def _new_story(decision: dict[str, Any], item: NewsItem, taken: set[str]) -> NewStory:
    slug = _text(decision, "slug")
    if slug in taken:
        raise ValueError(f"the slug {slug!r} is taken")
    status = _status(decision.get("status"))
    if status not in INITIAL_STATUSES:
        raise ValueError(f"a new story cannot start as {status.value}")
    story = NewStory(
        slug=slug,
        title_ru=_text(decision, "title_ru"),
        summary_ru=_text(decision, "summary_ru"),
        status=status,
        entities=_entities(decision.get("entities")),
        first_update=_update(decision, item),
    )
    check_story(story.slug, story.title_ru, story.summary_ru, story.status, story.entities)
    return story


def _attachment(decision: dict[str, Any], item: NewsItem, open_slugs: set[str]) -> Attachment:
    slug = _text(decision, "story")
    if slug not in open_slugs:
        raise ValueError(f"no open story {slug!r}")
    status = decision.get("status")
    return Attachment(slug, _update(decision, item), None if status is None else _status(status))


def plan(decisions: Sequence[Any], items: Sequence[NewsItem], stories: Sequence[Story]) -> Plan:
    """Checks the model's decisions about `items` against the stored `stories`; an invalid
    decision is rejected with the reason, the others are kept. An item may join a story that
    another decision of the same answer starts. Statuses are checked in the answer's order,
    as `apply` stores them."""
    by_url = {i.link: i for i in items}
    taken = {s.slug for s in stories}
    open_slugs = {s.slug for s in stories if s.status in OPEN_STATUSES}
    statuses = {s.slug: s.status for s in stories}
    new_stories: list[NewStory] = []
    attachments: list[tuple[dict[str, Any], NewsItem]] = []
    ignored: list[str] = []
    rejected: list[Rejection] = []
    answered: set[str] = set()
    for decision in decisions:
        url = decision.get("url") if isinstance(decision, dict) else None
        if not isinstance(decision, dict) or not isinstance(url, str) or not url.strip():
            rejected.append(Rejection(None, f"no source url in {decision!r}"))
            continue
        item = by_url.get(url)
        if item is None:
            rejected.append(Rejection(url, "the url is not one of the items sent"))
            continue
        if url in answered:
            rejected.append(Rejection(url, "a second decision for the same item"))
            continue
        answered.add(url)
        action = decision.get("action")
        try:
            if action == "ignore":
                ignored.append(url)
            elif action == "new":
                story = _new_story(decision, item, taken)
                taken.add(story.slug)
                open_slugs.add(story.slug)
                statuses[story.slug] = story.status
                new_stories.append(story)
            elif action == "attach":
                attachments.append((decision, item))
            else:
                raise ValueError(f"unknown action {action!r}")
        except ValueError as exc:
            rejected.append(Rejection(url, str(exc)))
    attached: list[Attachment] = []
    kept_status: list[Rejection] = []
    for decision, item in attachments:
        try:
            attachment = _attachment(decision, item, open_slugs)
        except ValueError as exc:
            rejected.append(Rejection(item.link, str(exc)))
            continue
        current = statuses[attachment.story_slug]
        if attachment.status is current:
            attachment = replace(attachment, status=None)
        elif attachment.status is not None and not can_move(current, attachment.status):
            kept_status.append(
                Rejection(
                    item.link,
                    f"{attachment.story_slug} cannot move from {current.value} to "
                    f"{attachment.status.value}",
                )
            )
            attachment = replace(attachment, status=None)
        if attachment.status is not None:
            statuses[attachment.story_slug] = attachment.status
        attached.append(attachment)
    return Plan(
        new_stories=tuple(new_stories),
        attachments=tuple(attached),
        ignored=tuple(ignored),
        rejected=tuple(rejected),
        unanswered=tuple(i.link for i in items if i.link not in answered),
        checked=tuple(i.link for i in items if i.link in answered),
        kept_status=tuple(kept_status),
    )


def detect(store: StoryStore, items: Sequence[NewsItem], model: TextModel | None = None) -> Plan:
    """Asks the model about `items` (see `pending`) and checks its answer; stores nothing.
    Raises DetectionError for an unreadable answer and GenerationError when the model fails."""
    model = model or digest.ClaudeCli(system_prompt=DETECTION_SYSTEM_PROMPT)
    decisions = parse_answer(model(build_prompt(store, items)))
    return plan(decisions, items, store.stories())


def apply(store: StoryStore, result: Plan, now: datetime) -> None:
    """Stores the plan's stories and updates and marks its items checked: all or nothing."""
    with store.transaction():
        for new in result.new_stories:
            store.create(
                new.slug,
                new.title_ru,
                new.summary_ru,
                new.status,
                new.entities,
                new.first_update,
                now,
            )
        for attachment in result.attachments:
            story = store.get_by_slug(attachment.story_slug)
            if story is None:
                raise StoryError(f"no story {attachment.story_slug!r}")
            store.add_update(story.id, attachment.update, now, attachment.status)
        store.mark_checked(result.checked, now)


def format_plan(result: Plan, stored: bool) -> str:
    counts = (
        f"{len(result.new_stories)} new stories, {len(result.attachments)} updates, "
        f"{len(result.ignored)} ignored, {len(result.rejected)} rejected, "
        f"{len(result.unanswered)} unanswered"
    )
    lines = [counts if stored else f"{counts} (dry run: nothing stored)"]
    lines += [
        f"  new story {s.slug} ({s.status.value}): {s.first_update.url}" for s in result.new_stories
    ]
    lines += [
        f"  update for {a.story_slug}"
        + (f" ({a.status.value})" if a.status else "")
        + f": {a.update.url}"
        for a in result.attachments
    ]
    lines += [f"  status not changed for {r.url}: {r.reason}" for r in result.kept_status]
    lines += [f"  rejected {r.url or '(no url)'}: {r.reason}" for r in result.rejected]
    lines += [f"  unanswered {url}" for url in result.unanswered]
    return "\n".join(lines)

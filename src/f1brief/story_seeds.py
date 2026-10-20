"""Seed stories (F27): articles that start a story through the normal story detection.

`story_seeds.toml` lists articles by hand, each with what a news feed would carry for it (link,
title, outlet, date, summary). `f1brief seed-stories` turns the ones no story has yet into news
items and sends them to `claude -p` with the stored stories, exactly as `detect-stories` sends
the hot items of the feeds: the same prompt, the same checks on the answer (a source URL for
every story and update, statuses only forward) and the same storing. The story's title,
summary, status and entities come from the model, never from this file or from code. A seed
marked `follow` (the default) then follows the story it started or joined, in the same
transaction, so a story that came from a seed is followed from its first update.

A seed is done once a story has an update from its URL; one the model ignored is sent again by
the next `seed-stories`. The file is optional for every other command.
"""

import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from f1brief import news
from f1brief.config import Favourites
from f1brief.news import NewsItem
from f1brief.story_detection import Plan

DEFAULT_PATH = Path("story_seeds.toml")

_KEYS = ("url", "title", "source", "published", "summary", "follow")


class SeedError(Exception):
    """The seeds file is missing, unreadable or invalid."""


@dataclass(frozen=True)
class Seed:
    url: str
    title: str
    source: str  # the outlet, as a feed names it
    published: datetime | None  # UTC
    summary: str = ""
    follow: bool = True


def load(path: str | Path = DEFAULT_PATH) -> tuple[Seed, ...]:
    file = Path(path)
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SeedError(f"{file}: no such file") from None
    except OSError as exc:
        raise SeedError(f"{file}: {exc.strerror or exc}") from exc
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SeedError(f"{file}: not valid TOML: {exc}") from exc
    try:
        return parse(data)
    except SeedError as exc:
        raise SeedError(f"{file}: {exc}") from exc


def parse(data: Mapping[str, Any]) -> tuple[Seed, ...]:
    unknown = sorted(set(data) - {"seed"})
    if unknown:
        raise SeedError(f"top level: unknown key(s) {', '.join(unknown)}")
    entries = data.get("seed", [])
    if not isinstance(entries, list):
        raise SeedError("seed: must be a list of tables ([[seed]])")
    seeds: list[Seed] = []
    seen: set[str] = set()
    for n, entry in enumerate(entries, start=1):
        at = f"seed[{n}]"
        if not isinstance(entry, dict):
            raise SeedError(f"{at}: must be a table")
        unknown = sorted(set(entry) - set(_KEYS))
        if unknown:
            raise SeedError(f"{at}: unknown key(s) {', '.join(unknown)}")
        url = _text(entry, "url", at)
        if not url.startswith(("https://", "http://")):
            raise SeedError(f"{at}.url: {url!r} is not a web link")
        if url in seen:
            raise SeedError(f"{at}.url: {url!r} is listed twice")
        seen.add(url)
        published = entry.get("published")
        if published is not None and not (
            isinstance(published, datetime) and published.tzinfo is not None
        ):
            raise SeedError(f"{at}.published: must be a date and time with an offset")
        summary = entry.get("summary", "")
        if not isinstance(summary, str):
            raise SeedError(f"{at}.summary: must be a string")
        follow = entry.get("follow", True)
        if not isinstance(follow, bool):
            raise SeedError(f"{at}.follow: must be true or false")
        seeds.append(
            Seed(
                url=url,
                title=_text(entry, "title", at),
                source=_text(entry, "source", at),
                published=published.astimezone(UTC) if published else None,
                summary=summary.strip(),
                follow=follow,
            )
        )
    return tuple(seeds)


def _text(entry: Mapping[str, Any], key: str, at: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SeedError(f"{at}.{key}: required, a non-empty string")
    return value.strip()


def items(seeds: Iterable[Seed], favourites: Favourites, now: datetime) -> list[NewsItem]:
    """The seeds as news items, classified like the items of a feed."""
    result = []
    for seed in seeds:
        year = (seed.published or now).year
        mentioned, topics, future = news.classify(seed.title, seed.summary, favourites, year)
        result.append(
            NewsItem(
                title=seed.title,
                link=seed.url,
                published=seed.published,
                summary=seed.summary,
                sources=(seed.source,),
                favourites=mentioned,
                topics=topics,
                future=future,
            )
        )
    return result


def stored(result: Plan) -> set[str]:
    """The URLs that `result` stores as a story's update."""
    return {s.first_update.url for s in result.new_stories} | {
        a.update.url for a in result.attachments
    }


def to_follow(result: Plan, seeds: Sequence[Seed]) -> list[str]:
    """The slugs of the stories that the seeds marked `follow` start or join in `result`, in
    the plan's order."""
    urls = {s.url for s in seeds if s.follow}
    slugs = [s.slug for s in result.new_stories if s.first_update.url in urls]
    slugs += [a.story_slug for a in result.attachments if a.update.url in urls]
    return list(dict.fromkeys(slugs))

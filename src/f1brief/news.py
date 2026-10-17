"""F1 news from RSS feeds, filtered for the favourites and key topics.

Feeds were checked on 2026-10-15: all are RSS 2.0. The official formula1.com feed has no item
dates at all (and declares the wrong encoding, which feedparser handles); its items are kept
with an unknown date unless the same story appears, dated, in another feed.
"""

import html
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any

import feedparser
import httpx

from f1brief.config import Favourites


@dataclass(frozen=True)
class Feed:
    name: str
    url: str


FEEDS = (
    Feed("Formula 1", "https://www.formula1.com/en/latest/all.xml"),
    Feed("Motorsport.com", "https://www.motorsport.com/rss/f1/news/"),
    Feed("Autosport", "https://www.autosport.com/rss/f1/news/"),
    Feed("RaceFans", "https://www.racefans.net/feed/"),
    Feed("BBC Sport", "https://feeds.bbci.co.uk/sport/formula1/rss.xml"),
)

TOPICS: dict[str, tuple[str, ...]] = {
    "upgrades": (
        "upgrade",
        "upgrades",
        "upgraded",
        "update package",
        "new floor",
        "new front wing",
    ),
    "penalties": (
        "penalty",
        "penalties",
        "penalised",
        "penalized",
        "grid drop",
        "disqualified",
        "disqualification",
        "reprimand",
        "stewards",
    ),
    "regulations": (
        "regulation",
        "regulations",
        "rule change",
        "rule changes",
        "technical directive",
        "FIA",
    ),
    # Phrases rather than single words: "signs", "deal", "exit" and "seat" on their own mostly
    # appear in race reports ("shows signs of", "deal with", "corner exit").
    "contracts": (
        "contract",
        "contracts",
        "contract extension",
        "signs for",
        "signed for",
        "signs with",
        "signed with",
        "new deal",
        "one-year deal",
        "two-year deal",
        "multi-year deal",
        "race seat",
        "seat for",
        "replaces",
        "replacement driver",
        "to leave",
        "will leave",
        "leaves the team",
        "departure",
    ),
    # Off-season (F18): car launches and tests. Phrases again: "test" alone is too common.
    "testing": (
        "pre-season testing",
        "pre-season test",
        "winter testing",
        "shakedown",
        "filming day",
        "car launch",
        "livery launch",
    ),
}

# A favourite's future: wording about contracts, seats or what comes next for them. Checked
# only for the favourites with `track_future` in the config.
FUTURE_TERMS = TOPICS["contracts"] + ("future", "career", "next season", "2027")

USER_AGENT = "f1brief (+https://github.com/Un1que11/f1-brief)"


class NewsError(Exception):
    """A feed could not be fetched or parsed."""


def term_pattern(terms: Iterable[str]) -> re.Pattern[str]:
    """Matches any of the terms as a whole word or phrase, ignoring case."""
    alternatives = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


_TOPIC_PATTERNS = {name: term_pattern(terms) for name, terms in TOPICS.items()}
_FUTURE = term_pattern(FUTURE_TERMS)


@cache
def _favourite_patterns(favourites: Favourites) -> tuple[tuple[str, re.Pattern[str], bool], ...]:
    """(name, pattern of its aliases, track_future) for every favourite team and driver."""
    return tuple(
        (fav.name, term_pattern(fav.aliases), fav.track_future)
        for fav in favourites.teams + favourites.drivers
    )


@dataclass(frozen=True)
class NewsItem:
    title: str
    link: str
    # UTC; None when no feed carrying the story gave a date.
    published: datetime | None
    summary: str
    # Every feed that carried the story, first one first.
    sources: tuple[str, ...]
    # Names of the favourites it mentions, as in the config.
    favourites: frozenset[str] = field(default_factory=frozenset)
    topics: frozenset[str] = field(default_factory=frozenset)
    # About the future in F1 of a favourite whose future is tracked (contract, moves).
    future: bool = False

    @property
    def relevant(self) -> bool:
        return bool(self.favourites or self.topics)


@dataclass(frozen=True)
class NewsReport:
    since: datetime
    # Newest first; undated items last.
    items: tuple[NewsItem, ...]
    failed_feeds: tuple[str, ...]

    @property
    def future(self) -> tuple[NewsItem, ...]:
        return tuple(i for i in self.items if i.future)


def _clean(text: str) -> str:
    """HTML to plain text: tags removed, entities decoded, whitespace collapsed."""
    without_tags = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def classify(
    title: str, summary: str, favourites: Favourites
) -> tuple[frozenset[str], frozenset[str], bool]:
    """(favourites mentioned, topics matched, is about the future of a favourite whose future
    is tracked)."""
    text = f"{title} {summary}"
    mentioned = [(n, f) for n, p, f in _favourite_patterns(favourites) if p.search(text)]
    topics = frozenset(n for n, p in _TOPIC_PATTERNS.items() if p.search(text))
    future = any(tracked for _, tracked in mentioned) and bool(_FUTURE.search(text))
    return frozenset(n for n, _ in mentioned), topics, future


def _published(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=UTC)


def parse_feed(content: bytes, source: str, favourites: Favourites) -> list[NewsItem]:
    """Items of one feed (RSS or Atom), classified. Unrelated items are kept here; filtering
    happens in `select`."""
    parsed = feedparser.parse(content)
    # An HTML error page parses "successfully" with no entries; only a recognised RSS/Atom
    # document (feedparser sets `version`) or one with entries counts as a feed.
    if not parsed.entries and (parsed.bozo or not parsed.get("version")):
        reason = parsed.get("bozo_exception") or "not an RSS or Atom document"
        raise NewsError(f"{source}: unreadable feed ({reason})")
    items = []
    for entry in parsed.entries:
        title = _clean(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not link:
            continue
        summary = _clean(entry.get("summary", ""))
        mentioned, topics, future = classify(title, summary, favourites)
        items.append(
            NewsItem(
                title=title,
                link=link,
                published=_published(entry),
                summary=summary,
                sources=(source,),
                favourites=mentioned,
                topics=topics,
                future=future,
            )
        )
    return items


def _story_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def deduplicate(items: Iterable[NewsItem]) -> list[NewsItem]:
    """Merges the same story (same link or same title) from several feeds: sources are
    combined, the earliest known date is kept, and tags are united."""
    merged: dict[str, NewsItem] = {}
    by_link: dict[str, str] = {}
    for item in items:
        key = by_link.get(item.link) or _story_key(item.title)
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
        else:
            dates = [d for d in (existing.published, item.published) if d is not None]
            merged[key] = replace(
                existing,
                published=min(dates) if dates else None,
                sources=existing.sources
                + tuple(s for s in item.sources if s not in existing.sources),
                favourites=existing.favourites | item.favourites,
                topics=existing.topics | item.topics,
                future=existing.future or item.future,
            )
        by_link[item.link] = key
    return list(merged.values())


def select(items: Iterable[NewsItem], since: datetime) -> list[NewsItem]:
    """Relevant items published since `since` (undated ones included), newest first."""
    chosen = [
        i
        for i in deduplicate(items)
        if i.relevant and (i.published is None or i.published >= since)
    ]
    return sorted(chosen, key=lambda i: (i.published is None, -(i.published or since).timestamp()))


def fetch_news(
    since: datetime,
    favourites: Favourites,
    feeds: Sequence[Feed] = FEEDS,
    client: httpx.Client | None = None,
) -> NewsReport:
    """Fetches every feed; a feed that fails is listed in `failed_feeds` instead of failing all."""
    if since.tzinfo is None:
        raise ValueError("since must be timezone-aware")
    owns_client = client is None
    client = client or httpx.Client(timeout=20.0, follow_redirects=True)
    items: list[NewsItem] = []
    failed: list[str] = []
    try:
        for feed in feeds:
            try:
                response = client.get(feed.url, headers={"User-Agent": USER_AGENT})
                response.raise_for_status()
                items.extend(parse_feed(response.content, feed.name, favourites))
            except (httpx.HTTPError, NewsError):
                failed.append(feed.name)
    finally:
        if owns_client:
            client.close()
    if failed and len(failed) == len(feeds):
        raise NewsError(f"no feed could be read: {', '.join(failed)}")
    return NewsReport(since=since, items=tuple(select(items, since)), failed_feeds=tuple(failed))


def default_since(now: datetime, days: int = 7) -> datetime:
    return now - timedelta(days=days)


def _line(item: NewsItem, fmt: Callable[[datetime], str]) -> str:
    when = fmt(item.published) if item.published else "date unknown"
    tags = sorted(item.favourites) + sorted(item.topics)
    sources = ", ".join(item.sources)
    return f"  [{when}] {item.title} ({sources}; {', '.join(tags)})\n      {item.link}"


def format_report(report: NewsReport) -> str:
    def fmt(d: datetime) -> str:
        return d.strftime("%d %b %H:%M UTC")

    lines = [f"F1 news since {fmt(report.since)}: {len(report.items)} relevant items"]
    if report.failed_feeds:
        lines.append(f"Could not read: {', '.join(report.failed_feeds)}")
    lines += ["", "Favourites' future"]
    lines += [_line(i, fmt) for i in report.future] or ["  nothing new"]
    lines += ["", "Favourites and key topics"]
    lines += [_line(i, fmt) for i in report.items if not i.future] or ["  nothing new"]
    return "\n".join(lines)

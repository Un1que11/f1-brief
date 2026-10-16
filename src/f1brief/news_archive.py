"""Relevant news items kept between runs, in the state file (git-ignored, see `*.db`).

RSS feeds carry only their latest items, often a week or less. The off-season summary (F18)
covers a whole month, so every off-season run adds the relevant items it fetched here, and the
summary reads them back for its period.
"""

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from f1brief.news import NewsItem
from f1brief.send_log import StateError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_items (
    link TEXT NOT NULL PRIMARY KEY,
    title TEXT NOT NULL,
    published TEXT,
    summary TEXT NOT NULL,
    sources TEXT NOT NULL,
    favourites TEXT NOT NULL,
    topics TEXT NOT NULL,
    ocon_future INTEGER NOT NULL,
    first_seen TEXT NOT NULL
)
"""


def _stamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).isoformat()


class NewsArchive:
    """The `news_items` table of the state file at `path`. Use as a context manager."""

    def __init__(self, path: str | Path) -> None:
        self.path = path
        try:
            self._db = sqlite3.connect(path, timeout=10.0, isolation_level=None)
            self._db.execute(_SCHEMA)
        except sqlite3.Error as exc:
            raise StateError(f"cannot open {path}: {exc}") from exc

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "NewsArchive":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def add(self, items: Iterable[NewsItem], now: datetime) -> int:
        """Stores the items not stored yet (by link); returns how many were new."""
        rows = [
            (
                i.link,
                i.title,
                _stamp(i.published) if i.published else None,
                i.summary,
                json.dumps(list(i.sources)),
                json.dumps(sorted(i.favourites)),
                json.dumps(sorted(i.topics)),
                int(i.ocon_future),
                _stamp(now),
            )
            for i in items
        ]
        try:
            before = self._db.total_changes
            self._db.executemany(
                "INSERT OR IGNORE INTO news_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
            )
            return self._db.total_changes - before
        except sqlite3.Error as exc:
            raise StateError(f"{self.path}: {exc}") from exc

    def since(self, moment: datetime) -> list[NewsItem]:
        """Items published at or after `moment`; an undated one counts from when it was first
        stored."""
        try:
            rows = self._db.execute(
                "SELECT title, link, published, summary, sources, favourites, topics, "
                "ocon_future FROM news_items "
                "WHERE published >= ? OR (published IS NULL AND first_seen >= ?)",
                (_stamp(moment), _stamp(moment)),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateError(f"{self.path}: {exc}") from exc
        return [
            NewsItem(
                title=title,
                link=link,
                published=datetime.fromisoformat(published) if published else None,
                summary=summary,
                sources=tuple(json.loads(sources)),
                favourites=frozenset(json.loads(favourites)),
                topics=frozenset(json.loads(topics)),
                ocon_future=bool(ocon_future),
            )
            for title, link, published, summary, sources, favourites, topics, ocon_future in rows
        ]

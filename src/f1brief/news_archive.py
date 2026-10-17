"""Relevant news items kept between runs, in the state file (git-ignored, see `*.db`).

RSS feeds carry only their latest items, often a week or less. The off-season summary (F18)
covers a whole month, so every off-season run adds the relevant items it fetched here, and the
summary reads them back for its period.

Boolean tags of an item (today only `future`) are stored as a JSON list of names in `flags`,
not as one column each, so that new ones need no schema change. Names this version does not
know are ignored when reading; `ocon_future`, the name before F20, still reads as `future`.
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
    flags TEXT NOT NULL,
    first_seen TEXT NOT NULL
)
"""

_COLUMNS = "link, title, published, summary, sources, favourites, topics, flags, first_seen"
_MARKS = ", ".join("?" * len(_COLUMNS.split(", ")))

# Boolean `NewsItem` fields kept in `flags`.
_FLAGS = ("future",)
# Flags stored under an earlier name: before F20 only Ocon's future was tracked.
_OLD_FLAGS = {"ocon_future": "future"}

# The first version (F18) had an `ocon_future INTEGER` column instead of `flags`.
_MIGRATE_OCON_FUTURE = (
    "ALTER TABLE news_items ADD COLUMN flags TEXT NOT NULL DEFAULT '[]'",
    """UPDATE news_items SET flags = '["ocon_future"]' WHERE ocon_future""",
    "ALTER TABLE news_items DROP COLUMN ocon_future",
)


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
            self._migrate()
        except sqlite3.Error as exc:
            raise StateError(f"cannot open {path}: {exc}") from exc

    def _migrate(self) -> None:
        # IMMEDIATE takes the write lock before the columns are read, so of two runs that
        # open an old file at once only the first migrates it.
        self._db.execute("BEGIN IMMEDIATE")
        try:
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(news_items)")}
            if "ocon_future" in columns:
                for statement in _MIGRATE_OCON_FUTURE:
                    self._db.execute(statement)
            self._db.execute("COMMIT")
        except sqlite3.Error:
            self._db.execute("ROLLBACK")
            raise

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
                json.dumps([f for f in _FLAGS if getattr(i, f)]),
                _stamp(now),
            )
            for i in items
        ]
        try:
            before = self._db.total_changes
            self._db.executemany(
                f"INSERT OR IGNORE INTO news_items ({_COLUMNS}) VALUES ({_MARKS})", rows
            )
            return self._db.total_changes - before
        except sqlite3.Error as exc:
            raise StateError(f"{self.path}: {exc}") from exc

    def since(self, moment: datetime) -> list[NewsItem]:
        """Items published at or after `moment`; an undated one counts from when it was first
        stored."""
        try:
            rows = self._db.execute(
                "SELECT title, link, published, summary, sources, favourites, topics, flags "
                "FROM news_items "
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
                **_flags(json.loads(flags)),
            )
            for title, link, published, summary, sources, favourites, topics, flags in rows
        ]


def _flags(stored: list[str]) -> dict[str, bool]:
    names = {_OLD_FLAGS.get(name, name) for name in stored}
    return {f: f in names for f in _FLAGS}

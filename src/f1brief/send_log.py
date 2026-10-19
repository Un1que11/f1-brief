"""What has been sent: a small SQLite file (git-ignored, see `*.db`).

One row per round's digest (`sends`), per round's message after qualifying
(`qualifying_sends`, F19) and per off-season month's summary (`summaries`).
A run claims the row right before sending (`claim`, atomic, so two runs at once cannot both
send), then records the outcome. A row that says `sending` or `partial` means some messages
may be in the chat already, so it is never sent again by itself; `forget` removes the row
after Misha has checked the chat.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType

DEFAULT_STATE_FILE = "state.db"
_ROUND = "season = ? AND round = ?"
_MONTH = "month = ?"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sends (
    season INTEGER NOT NULL,
    round INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'partial')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    messages INTEGER,
    PRIMARY KEY (season, round)
);
CREATE TABLE IF NOT EXISTS qualifying_sends (
    season INTEGER NOT NULL,
    round INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'partial')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    messages INTEGER,
    PRIMARY KEY (season, round)
);
CREATE TABLE IF NOT EXISTS summaries (
    month TEXT NOT NULL PRIMARY KEY,  -- YYYY-MM, Warsaw time
    status TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'partial')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    messages INTEGER
);
"""


class StateError(Exception):
    """The state file could not be opened, read or written."""


class Status(StrEnum):
    # Claimed by a run that has not finished (or that crashed while sending).
    SENDING = "sending"
    SENT = "sent"
    # Some messages were (or may have been) delivered before a failure.
    PARTIAL = "partial"


@dataclass(frozen=True)
class SendRecord:
    season: int
    round: int
    status: Status
    started_at: datetime
    finished_at: datetime | None
    # Messages delivered: all of them for `sent`, the confirmed ones for `partial`.
    messages: int | None


@dataclass(frozen=True)
class SummaryRecord:
    month: str  # YYYY-MM
    status: Status
    started_at: datetime
    finished_at: datetime | None
    messages: int | None


def _stamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).isoformat()


class SendLog:
    """The state file at `path` (created on first use). Use as a context manager."""

    def __init__(self, path: str | Path = DEFAULT_STATE_FILE) -> None:
        self.path = path
        try:
            # Autocommit: every statement below is a transaction of its own.
            self._db = sqlite3.connect(path, timeout=10.0, isolation_level=None)
            self._db.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise StateError(f"cannot open {path}: {exc}") from exc

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "SendLog":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _execute(self, sql: str, params: tuple[object, ...]) -> sqlite3.Cursor:
        try:
            return self._db.execute(sql, params)
        except sqlite3.Error as exc:
            raise StateError(f"{self.path}: {exc}") from exc

    # Every operation works on one row of a table, picked by `where` and its `key` values.

    def _get(self, table: str, where: str, key: tuple[object, ...]) -> tuple | None:
        return self._execute(
            f"SELECT status, started_at, finished_at, messages FROM {table} WHERE {where}", key
        ).fetchone()

    def _claim(self, table: str, columns: str, key: tuple[object, ...], now: datetime) -> bool:
        marks = ", ".join("?" * (len(key) + 2))
        cursor = self._execute(
            f"INSERT OR IGNORE INTO {table} ({columns}, status, started_at) VALUES ({marks})",
            (*key, Status.SENDING.value, _stamp(now)),
        )
        return cursor.rowcount == 1

    def _finish(
        self,
        table: str,
        where: str,
        key: tuple[object, ...],
        status: Status,
        now: datetime,
        n: int,
        what: str,
    ) -> None:
        cursor = self._execute(
            f"UPDATE {table} SET status = ?, finished_at = ?, messages = ? "
            f"WHERE {where} AND status = ?",
            (status.value, _stamp(now), n, *key, Status.SENDING.value),
        )
        if cursor.rowcount != 1:
            raise StateError(f"{what} was not claimed for sending")

    def _release(self, table: str, where: str, key: tuple[object, ...]) -> None:
        self._execute(
            f"DELETE FROM {table} WHERE {where} AND status = ?", (*key, Status.SENDING.value)
        )

    def _forget(self, table: str, where: str, key: tuple[object, ...]) -> bool:
        return self._execute(f"DELETE FROM {table} WHERE {where}", key).rowcount == 1

    def _round_record(self, table: str, season: int, round: int) -> SendRecord | None:
        row = self._get(table, _ROUND, (season, round))
        if row is None:
            return None
        status, started_at, finished_at, messages = row
        return SendRecord(
            season=season,
            round=round,
            status=Status(status),
            started_at=datetime.fromisoformat(started_at),
            finished_at=datetime.fromisoformat(finished_at) if finished_at else None,
            messages=messages,
        )

    # --- a round's digest ------------------------------------------------------------------

    def get(self, season: int, round: int) -> SendRecord | None:
        return self._round_record("sends", season, round)

    def claim(self, season: int, round: int, now: datetime) -> bool:
        """Marks the round as being sent; False when it already has a row (sent, partly
        sent, or claimed by another run)."""
        return self._claim("sends", "season, round", (season, round), now)

    def mark_sent(self, season: int, round: int, now: datetime, messages: int) -> None:
        self._finish(
            "sends", _ROUND, (season, round), Status.SENT, now, messages, f"round {season}/{round}"
        )

    def mark_partial(self, season: int, round: int, now: datetime, delivered: int) -> None:
        self._finish(
            "sends",
            _ROUND,
            (season, round),
            Status.PARTIAL,
            now,
            delivered,
            f"round {season}/{round}",
        )

    def release(self, season: int, round: int) -> None:
        """Drops a claim when nothing reached the chat, so a later run may try again."""
        self._release("sends", _ROUND, (season, round))

    def forget(self, season: int, round: int) -> bool:
        """Removes the round's row whatever its status; False when there was none."""
        return self._forget("sends", _ROUND, (season, round))

    def last_digest_at(self, before: datetime) -> datetime | None:
        """When the latest round's digest started going out before `before`, or None when
        there is none. Any status counts: a digest that was claimed may be in the chat."""
        row = self._execute(
            "SELECT MAX(started_at) FROM sends WHERE started_at < ?", (_stamp(before),)
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row[0] else None

    # --- a round's message after qualifying (F19) ------------------------------------------

    def get_qualifying(self, season: int, round: int) -> SendRecord | None:
        return self._round_record("qualifying_sends", season, round)

    def claim_qualifying(self, season: int, round: int, now: datetime) -> bool:
        return self._claim("qualifying_sends", "season, round", (season, round), now)

    def mark_qualifying_sent(self, season: int, round: int, now: datetime, messages: int) -> None:
        what = f"qualifying message {season}/{round}"
        self._finish("qualifying_sends", _ROUND, (season, round), Status.SENT, now, messages, what)

    def mark_qualifying_partial(
        self, season: int, round: int, now: datetime, delivered: int
    ) -> None:
        what = f"qualifying message {season}/{round}"
        self._finish(
            "qualifying_sends", _ROUND, (season, round), Status.PARTIAL, now, delivered, what
        )

    def release_qualifying(self, season: int, round: int) -> None:
        self._release("qualifying_sends", _ROUND, (season, round))

    def forget_qualifying(self, season: int, round: int) -> bool:
        return self._forget("qualifying_sends", _ROUND, (season, round))

    # --- an off-season month's summary -----------------------------------------------------

    def get_summary(self, month: str) -> SummaryRecord | None:
        row = self._get("summaries", _MONTH, (month,))
        if row is None:
            return None
        status, started_at, finished_at, messages = row
        return SummaryRecord(
            month=month,
            status=Status(status),
            started_at=datetime.fromisoformat(started_at),
            finished_at=datetime.fromisoformat(finished_at) if finished_at else None,
            messages=messages,
        )

    def claim_summary(self, month: str, now: datetime) -> bool:
        return self._claim("summaries", "month", (month,), now)

    def mark_summary_sent(self, month: str, now: datetime, messages: int) -> None:
        self._finish("summaries", _MONTH, (month,), Status.SENT, now, messages, f"summary {month}")

    def mark_summary_partial(self, month: str, now: datetime, delivered: int) -> None:
        self._finish(
            "summaries", _MONTH, (month,), Status.PARTIAL, now, delivered, f"summary {month}"
        )

    def release_summary(self, month: str) -> None:
        self._release("summaries", _MONTH, (month,))

    def forget_summary(self, month: str) -> bool:
        return self._forget("summaries", _MONTH, (month,))

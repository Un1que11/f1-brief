"""Which rounds the digest has been sent for: a small SQLite file (git-ignored, see `*.db`).

One row per round. A run claims the round right before sending (`claim`, atomic, so two runs
at once cannot both send), then records the outcome. A row that says `sending` or `partial`
means some messages may be in the chat already, so the round is never sent again by itself;
`forget` removes the row after Misha has checked the chat.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType

DEFAULT_STATE_FILE = "state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sends (
    season INTEGER NOT NULL,
    round INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'partial')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    messages INTEGER,
    PRIMARY KEY (season, round)
)
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
            self._db.execute(_SCHEMA)
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

    def get(self, season: int, round: int) -> SendRecord | None:
        row = self._execute(
            "SELECT status, started_at, finished_at, messages FROM sends "
            "WHERE season = ? AND round = ?",
            (season, round),
        ).fetchone()
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

    def claim(self, season: int, round: int, now: datetime) -> bool:
        """Marks the round as being sent; False when it already has a row (sent, partly
        sent, or claimed by another run)."""
        cursor = self._execute(
            "INSERT OR IGNORE INTO sends (season, round, status, started_at) VALUES (?, ?, ?, ?)",
            (season, round, Status.SENDING.value, _stamp(now)),
        )
        return cursor.rowcount == 1

    def _finish(self, season: int, round: int, status: Status, now: datetime, n: int) -> None:
        cursor = self._execute(
            "UPDATE sends SET status = ?, finished_at = ?, messages = ? "
            "WHERE season = ? AND round = ? AND status = ?",
            (status.value, _stamp(now), n, season, round, Status.SENDING.value),
        )
        if cursor.rowcount != 1:
            raise StateError(f"round {season}/{round} was not claimed for sending")

    def mark_sent(self, season: int, round: int, now: datetime, messages: int) -> None:
        self._finish(season, round, Status.SENT, now, messages)

    def mark_partial(self, season: int, round: int, now: datetime, delivered: int) -> None:
        self._finish(season, round, Status.PARTIAL, now, delivered)

    def release(self, season: int, round: int) -> None:
        """Drops a claim when nothing reached the chat, so a later run may try again."""
        self._execute(
            "DELETE FROM sends WHERE season = ? AND round = ? AND status = ?",
            (season, round, Status.SENDING.value),
        )

    def forget(self, season: int, round: int) -> bool:
        """Removes the round's row whatever its status; False when there was none."""
        cursor = self._execute("DELETE FROM sends WHERE season = ? AND round = ?", (season, round))
        return cursor.rowcount == 1

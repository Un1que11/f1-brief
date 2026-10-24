"""Numbered schema migrations for the parts of the state file (`state.db`).

Each part of the file that migrates (the users, the stories, the send log) has a `component`
name and a tuple of steps. A step is a tuple of SQL statements run in order; the file records
how many steps each component has had in `schema_versions`, one row per component. All the
steps a file still needs run in one `BEGIN IMMEDIATE` transaction, which takes the write lock
before the version is read, so of two programs that open an old file at once only the first
migrates it. A file migrated by a newer version of the program is refused.

Never edit a released step: add a new one.
"""

import sqlite3
from collections.abc import Sequence
from pathlib import Path

DEFAULT_STATE_FILE = "state.db"


class StateError(Exception):
    """The state file could not be opened, read or written."""


VERSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_versions (
    component TEXT NOT NULL PRIMARY KEY,
    version INTEGER NOT NULL
)
"""

Step = tuple[str, ...]


def version(db: sqlite3.Connection, component: str) -> int:
    """How many steps of `component` the file has had (0 for a new file)."""
    db.execute(VERSIONS_TABLE)
    row = db.execute(
        "SELECT version FROM schema_versions WHERE component = ?", (component,)
    ).fetchone()
    return row[0] if row else 0


def migrate(db: sqlite3.Connection, path: str | Path, component: str, steps: Sequence[Step]) -> int:
    """Runs the steps of `component` the file at `path` (open as `db`, in autocommit mode)
    has not had yet and returns how many ran. Raises StateError for a file that is newer than
    `steps` or a step that fails (nothing is changed then)."""
    try:
        db.execute(VERSIONS_TABLE)
        outer = db.in_transaction
        if not outer:
            db.execute("BEGIN IMMEDIATE")
        try:
            current = version(db, component)
            if current > len(steps):
                raise StateError(
                    f"{path}: the {component} schema is version {current}, newer than this "
                    f"program's {len(steps)}"
                )
            for step in steps[current:]:
                for statement in step:
                    db.execute(statement)
            if current < len(steps):
                db.execute(
                    "INSERT OR REPLACE INTO schema_versions (component, version) VALUES (?, ?)",
                    (component, len(steps)),
                )
        except BaseException:
            if not outer:
                db.rollback()
            raise
        if not outer:
            db.execute("COMMIT")
        return len(steps) - current
    except sqlite3.Error as exc:
        raise StateError(f"{path}: {exc}") from exc

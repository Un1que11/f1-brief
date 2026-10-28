"""Hot stories (F21): storylines that span weeks or months, kept in the state file (git-ignored,
see `*.db`) next to the send log and the news archive.

The tables follow "Hot stories" in CLAUDE.md: `stories`, `story_entities`, `story_updates` and
`subscriptions`. Every update carries the URL of the article it came from, and a story is
created together with its first update, so there is no story and no update without a source.
A story's status changes only together with a sourced update (`add_update(..., status=...)`),
only forward along its lifecycle (`can_move`), and the update records the status it set.
Unfollowing keeps the story and its timeline. `checked_news` lists the news links story
detection (F22) has already looked at, so each article is judged once. `story_alerts` records
which updates of followed stories an alert (F24) has sent or skipped. `stale_offers` records
the offers to unfollow a followed story that has gone quiet (F26).

Texts are language-neutral (F31): a story's title and summary and an update's `what_changed`
are stored as written, with the `language` they are written in (story detection writes them in
`DETECTION_LANGUAGE`). Their other languages are generated on demand (see `story_texts`) and
cached in `story_texts` / `update_texts`; a cached text is derived from the stored one and
never replaces it, and the update's URL stays the one source of what it says.

Following is per account (F32): `subscriptions`, `story_alerts` and `stale_offers` are keyed
by `user_id`, and a `StoryStore` follows, alerts and offers for one account (the owner by
default, see `users`). The stories, their entities, updates and texts are shared by everyone.

The schema is created and upgraded by the numbered steps in `_MIGRATIONS` (see
`state_schema`). A file migrated by a newer version is refused.
"""

import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from urllib.parse import urlsplit

from f1brief import users
from f1brief.state_schema import DEFAULT_STATE_FILE, StateError, migrate

_COMPONENT = "stories"

# Each step is a list of statements run in one transaction; never edit a released step, add
# a new one.
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE stories (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            title_ru TEXT NOT NULL,
            summary_ru TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('rumor', 'developing', 'confirmed', 'denied', 'resolved')),
            created_at TEXT NOT NULL,
            last_update_at TEXT NOT NULL
        )""",
        """CREATE TABLE story_entities (
            story_id INTEGER NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('driver', 'team', 'person')),
            ref TEXT NOT NULL,
            PRIMARY KEY (story_id, kind, ref)
        )""",
        """CREATE TABLE story_updates (
            id INTEGER PRIMARY KEY,
            story_id INTEGER NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
            published_at TEXT,  -- null: the article has no date
            headline TEXT NOT NULL,
            url TEXT NOT NULL CHECK (url LIKE 'http://%' OR url LIKE 'https://%'),
            source TEXT NOT NULL,
            what_changed_ru TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            UNIQUE (story_id, url)
        )""",
        "CREATE INDEX story_updates_recorded_at ON story_updates (recorded_at)",
        """CREATE TABLE subscriptions (
            story_id INTEGER NOT NULL PRIMARY KEY REFERENCES stories (id) ON DELETE CASCADE,
            active INTEGER NOT NULL CHECK (active IN (0, 1)),
            followed_at TEXT NOT NULL,
            unfollowed_at TEXT,
            muted_until TEXT
        )""",
    ),
    (
        """CREATE TABLE checked_news (
            url TEXT NOT NULL PRIMARY KEY,
            checked_at TEXT NOT NULL
        )""",
    ),
    (
        # One row per update an alert (F24) has dealt with: sent, or skipped (muted story,
        # too old). One alert covers a story's waiting updates, so its rows share story_id
        # and decided_at.
        """CREATE TABLE story_alerts (
            update_id INTEGER NOT NULL PRIMARY KEY
                REFERENCES story_updates (id) ON DELETE CASCADE,
            story_id INTEGER NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
            outcome TEXT NOT NULL CHECK (outcome IN ('sent', 'skipped')),
            decided_at TEXT NOT NULL
        )""",
        "CREATE INDEX story_alerts_decided_at ON story_alerts (outcome, decided_at)",
    ),
    (
        # The status an update moved its story to (F26): set on a story's first update and on
        # each one that changed the status, null otherwise. Updates stored before this step
        # keep null: which of them changed the status was not recorded.
        "ALTER TABLE story_updates ADD COLUMN status TEXT CHECK (status IS NULL OR status IN "
        "('rumor', 'developing', 'confirmed', 'denied', 'resolved'))",
        # One row per offer to unfollow a followed story with no news (F26). `quiet_since` is
        # when the quiet stretch began (the latest update, or the follow if later), so each
        # stretch is offered once.
        """CREATE TABLE stale_offers (
            story_id INTEGER NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
            quiet_since TEXT NOT NULL,
            offered_at TEXT NOT NULL,
            PRIMARY KEY (story_id, quiet_since)
        )""",
    ),
    (
        # Language-neutral texts (F31): the columns lose their `_ru` suffix and record the
        # language they are written in. Every row stored before this step was written in
        # Russian by story detection, so they all get 'ru'.
        "ALTER TABLE stories RENAME COLUMN title_ru TO title",
        "ALTER TABLE stories RENAME COLUMN summary_ru TO summary",
        "ALTER TABLE stories ADD COLUMN language TEXT NOT NULL DEFAULT 'ru'",
        "ALTER TABLE story_updates RENAME COLUMN what_changed_ru TO what_changed",
        "ALTER TABLE story_updates ADD COLUMN language TEXT NOT NULL DEFAULT 'ru'",
        # The texts generated in other languages, cached: one row per story (or update) and
        # language, never in the language the row itself is written in.
        """CREATE TABLE story_texts (
            story_id INTEGER NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
            language TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            PRIMARY KEY (story_id, language)
        )""",
        """CREATE TABLE update_texts (
            update_id INTEGER NOT NULL REFERENCES story_updates (id) ON DELETE CASCADE,
            language TEXT NOT NULL,
            what_changed TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            PRIMARY KEY (update_id, language)
        )""",
    ),
    (
        # Per account (F32): following, alerts and offers to unfollow get a `user_id`, part
        # of their key. The rows stored before there were users are the owner's.
        """CREATE TABLE subscriptions_new (
            user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            story_id INTEGER NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
            active INTEGER NOT NULL CHECK (active IN (0, 1)),
            followed_at TEXT NOT NULL,
            unfollowed_at TEXT,
            muted_until TEXT,
            PRIMARY KEY (user_id, story_id)
        )""",
        f"INSERT INTO subscriptions_new SELECT {users.OWNER_ID}, story_id, active, followed_at, "
        "unfollowed_at, muted_until FROM subscriptions",
        "DROP TABLE subscriptions",
        "ALTER TABLE subscriptions_new RENAME TO subscriptions",
        """CREATE TABLE story_alerts_new (
            user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            update_id INTEGER NOT NULL REFERENCES story_updates (id) ON DELETE CASCADE,
            story_id INTEGER NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
            outcome TEXT NOT NULL CHECK (outcome IN ('sent', 'skipped')),
            decided_at TEXT NOT NULL,
            PRIMARY KEY (user_id, update_id)
        )""",
        f"INSERT INTO story_alerts_new SELECT {users.OWNER_ID}, update_id, story_id, outcome, "
        "decided_at FROM story_alerts",
        "DROP TABLE story_alerts",
        "ALTER TABLE story_alerts_new RENAME TO story_alerts",
        "CREATE INDEX story_alerts_decided_at ON story_alerts (user_id, outcome, decided_at)",
        """CREATE TABLE stale_offers_new (
            user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            story_id INTEGER NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
            quiet_since TEXT NOT NULL,
            offered_at TEXT NOT NULL,
            PRIMARY KEY (user_id, story_id, quiet_since)
        )""",
        f"INSERT INTO stale_offers_new SELECT {users.OWNER_ID}, story_id, quiet_since, "
        "offered_at FROM stale_offers",
        "DROP TABLE stale_offers",
        "ALTER TABLE stale_offers_new RENAME TO stale_offers",
    ),
)

SCHEMA_VERSION = len(_MIGRATIONS)

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_LANGUAGE = re.compile(r"[a-z]{2}")

# The language story detection writes titles, summaries and `what_changed` in. The bot and
# the alerts show them as they are (their UI is Russian until F30).
DETECTION_LANGUAGE = "ru"


class StoryError(Exception):
    """A story operation that does not fit the stored stories (unknown id, taken slug)."""


class StoryStatus(StrEnum):
    RUMOR = "rumor"
    DEVELOPING = "developing"
    CONFIRMED = "confirmed"
    DENIED = "denied"
    RESOLVED = "resolved"


# A story's lifecycle (see "Hot stories" in CLAUDE.md): rumor -> developing -> confirmed or
# denied -> resolved. A status only moves forward, it may skip a step (an official
# announcement can confirm a rumour), and only a confirmed or denied story becomes resolved.
_STAGES = {
    StoryStatus.RUMOR: 0,
    StoryStatus.DEVELOPING: 1,
    StoryStatus.CONFIRMED: 2,
    StoryStatus.DENIED: 2,
    StoryStatus.RESOLVED: 3,
}
# What a new story may start as: it is resolved only after its outcome was announced.
INITIAL_STATUSES = (
    StoryStatus.RUMOR,
    StoryStatus.DEVELOPING,
    StoryStatus.CONFIRMED,
    StoryStatus.DENIED,
)


def can_move(current: StoryStatus, new: StoryStatus) -> bool:
    """Whether an update may move a story from `current` to `new` (a different status)."""
    if new is StoryStatus.RESOLVED:
        return current in (StoryStatus.CONFIRMED, StoryStatus.DENIED)
    return _STAGES[StoryStatus(new)] > _STAGES[StoryStatus(current)]


class EntityKind(StrEnum):
    DRIVER = "driver"
    TEAM = "team"
    PERSON = "person"


@dataclass(frozen=True)
class Entity:
    """Who a story is about. `ref` is the Jolpica id for drivers and teams ("bearman",
    "haas"); for a person without one, a slug of their name ("fred_vasseur")."""

    kind: EntityKind
    ref: str


@dataclass(frozen=True)
class Update:
    """One sourced development of a story, as it is added."""

    headline: str
    url: str
    source: str  # the outlet, e.g. "BBC Sport"
    what_changed: str  # in `language`
    published_at: datetime | None  # None: the article has no date
    language: str  # the language `what_changed` is written in, e.g. "ru"


@dataclass(frozen=True)
class StoryUpdate:
    """An update as stored: which story, and when it was recorded."""

    id: int
    story_id: int
    update: Update
    recorded_at: datetime
    # The status this update moved the story to; None when it changed none (or was stored
    # before F26 recorded it).
    status: StoryStatus | None = None


@dataclass(frozen=True)
class Story:
    id: int
    slug: str
    title: str  # in `language`
    summary: str  # in `language`
    status: StoryStatus
    created_at: datetime
    # When the latest update was published (or recorded, for an undated article).
    last_update_at: datetime
    language: str  # the language the title and summary are written in


@dataclass(frozen=True)
class StoryText:
    """A story's title and summary in one language."""

    title: str
    summary: str


@dataclass(frozen=True)
class Subscription:
    story_id: int
    active: bool
    followed_at: datetime
    unfollowed_at: datetime | None
    muted_until: datetime | None

    def muted(self, now: datetime) -> bool:
        return self.muted_until is not None and now < self.muted_until


def _stamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).isoformat()


def _moment(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _check_text(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def check_language(language: str) -> None:
    """Raises ValueError for a language that is not a two-letter lowercase code."""
    if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
        raise ValueError(f"a language is a two-letter lowercase code, not {language!r}")


def check_update(update: Update) -> None:
    """Raises ValueError for an update that cannot be stored: no http(s) source URL, or an
    empty headline, source or description."""
    parts = urlsplit(update.url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"an update needs the http(s) URL of its source, not {update.url!r}")
    _check_text("headline", update.headline)
    _check_text("source", update.source)
    _check_text("what_changed", update.what_changed)
    check_language(update.language)
    if update.published_at is not None and update.published_at.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")


def _check_entity(entity: Entity) -> None:
    EntityKind(entity.kind)
    _check_text("entity ref", entity.ref)


def check_story(
    slug: str,
    title: str,
    summary: str,
    status: StoryStatus,
    entities: Sequence[Entity],
    language: str,
) -> None:
    """Raises ValueError for a new story that cannot be stored: a slug that is not lowercase
    words joined by '-', an empty title or summary, an unknown status, entity kind or
    language."""
    if not _SLUG.fullmatch(slug):
        raise ValueError(f"a slug is lowercase words joined by '-', not {slug!r}")
    _check_text("title", title)
    _check_text("summary", summary)
    check_language(language)
    StoryStatus(status)
    for entity in entities:
        _check_entity(entity)


_STORY_COLUMNS = "id, slug, title, summary, status, created_at, last_update_at, language"
_UPDATE_COLUMNS = (
    "id, story_id, headline, url, source, what_changed, published_at, recorded_at, status, language"
)


def _story(row: tuple) -> Story:
    id_, slug, title, summary, status, created_at, last_update_at, language = row
    return Story(
        id=id_,
        slug=slug,
        title=title,
        summary=summary,
        status=StoryStatus(status),
        created_at=datetime.fromisoformat(created_at),
        last_update_at=datetime.fromisoformat(last_update_at),
        language=language,
    )


def _story_update(row: tuple) -> StoryUpdate:
    (
        id_,
        story_id,
        headline,
        url,
        source,
        what_changed,
        published_at,
        recorded_at,
        status,
        language,
    ) = row
    return StoryUpdate(
        id=id_,
        story_id=story_id,
        update=Update(
            headline=headline,
            url=url,
            source=source,
            what_changed=what_changed,
            published_at=_moment(published_at),
            language=language,
        ),
        recorded_at=datetime.fromisoformat(recorded_at),
        status=StoryStatus(status) if status else None,
    )


class StoryStore:
    """The story tables of the state file at `path` (created on first use), following as
    account `user_id`. Use as a context manager."""

    def __init__(
        self, path: str | Path = DEFAULT_STATE_FILE, user_id: int = users.OWNER_ID
    ) -> None:
        self.path = path
        self.user_id = user_id
        try:
            # Autocommit; operations that write several rows use `_transaction`.
            self._db = sqlite3.connect(path, timeout=10.0, isolation_level=None)
            self._db.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as exc:
            raise StateError(f"cannot open {path}: {exc}") from exc
        users.ensure_schema(self._db, path)
        migrate(self._db, path, _COMPONENT, _MIGRATIONS)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._db.in_transaction:
            # Part of a larger one (`transaction`), which commits or rolls back as a whole.
            yield
            return
        self._execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.rollback()
            raise
        self._execute("COMMIT")

    def transaction(self) -> AbstractContextManager[None]:
        """Groups several operations: all of them are stored, or none (on an exception)."""
        return self._transaction()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "StoryStore":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        try:
            return self._db.execute(sql, params)
        except sqlite3.Error as exc:
            raise StateError(f"{self.path}: {exc}") from exc

    def _require(self, story_id: int) -> Story:
        story = self.get(story_id)
        if story is None:
            raise StoryError(f"no story {story_id}")
        return story

    # --- stories and their updates ---------------------------------------------------------

    def create(
        self,
        slug: str,
        title: str,
        summary: str,
        status: StoryStatus,
        entities: Sequence[Entity],
        first_update: Update,
        now: datetime,
        *,
        language: str,
    ) -> Story:
        """Stores a new story with its entities and the update it came from; `language` is
        the one its title and summary are written in. Raises StoryError when the slug is
        taken, ValueError for invalid fields."""
        check_story(slug, title, summary, status, entities, language)
        status = StoryStatus(status)
        check_update(first_update)
        recorded = _stamp(now)
        last = _stamp(first_update.published_at) if first_update.published_at else recorded
        with self._transaction():
            if self.get_by_slug(slug) is not None:
                raise StoryError(f"a story with the slug {slug!r} exists")
            story_id = self._execute(
                "INSERT INTO stories (slug, title, summary, status, created_at, "
                "last_update_at, language) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (slug, title, summary, status.value, recorded, last, language),
            ).lastrowid
            assert story_id is not None
            self._execute_many(
                "INSERT OR IGNORE INTO story_entities (story_id, kind, ref) VALUES (?, ?, ?)",
                [(story_id, EntityKind(e.kind).value, e.ref) for e in entities],
            )
            self._insert_update(story_id, first_update, recorded, status)
        return self._require(story_id)

    def _execute_many(self, sql: str, rows: list[tuple[object, ...]]) -> None:
        try:
            self._db.executemany(sql, rows)
        except sqlite3.Error as exc:
            raise StateError(f"{self.path}: {exc}") from exc

    def _insert_update(
        self, story_id: int, update: Update, recorded: str, status: StoryStatus | None
    ) -> bool:
        published = _stamp(update.published_at) if update.published_at else None
        cursor = self._execute(
            "INSERT OR IGNORE INTO story_updates (story_id, published_at, headline, url, "
            "source, what_changed, recorded_at, status, language) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                story_id,
                published,
                update.headline,
                update.url,
                update.source,
                update.what_changed,
                recorded,
                status.value if status else None,
                update.language,
            ),
        )
        return cursor.rowcount == 1

    def add_update(
        self,
        story_id: int,
        update: Update,
        now: datetime,
        status: StoryStatus | None = None,
    ) -> bool:
        """Attaches a sourced update to a story, and moves the story to `status` if given
        (the story's own status changes nothing). False (and nothing changed) when the story
        already has an update from that URL. Raises StoryError for an unknown story or a move
        its lifecycle does not allow (`can_move`)."""
        check_update(update)
        recorded = _stamp(now)
        moment = _stamp(update.published_at) if update.published_at else recorded
        with self._transaction():
            story = self._require(story_id)
            new = StoryStatus(status) if status else None
            if new is story.status:
                new = None
            if new is not None and not can_move(story.status, new):
                raise StoryError(
                    f"story {story_id} cannot move from {story.status.value} to {new.value}"
                )
            if not self._insert_update(story_id, update, recorded, new):
                return False
            self._execute(
                "UPDATE stories SET last_update_at = MAX(last_update_at, ?), "
                "status = COALESCE(?, status) WHERE id = ?",
                (moment, new.value if new else None, story_id),
            )
        return True

    def get(self, story_id: int) -> Story | None:
        row = self._execute(
            f"SELECT {_STORY_COLUMNS} FROM stories WHERE id = ?", (story_id,)
        ).fetchone()
        return _story(row) if row else None

    def get_by_slug(self, slug: str) -> Story | None:
        row = self._execute(
            f"SELECT {_STORY_COLUMNS} FROM stories WHERE slug = ?", (slug,)
        ).fetchone()
        return _story(row) if row else None

    def stories(self, statuses: Sequence[StoryStatus] | None = None) -> list[Story]:
        """All stories (or those in `statuses`), the most recently updated first."""
        sql = f"SELECT {_STORY_COLUMNS} FROM stories"
        params: tuple[object, ...] = ()
        if statuses is not None:
            sql += f" WHERE status IN ({', '.join('?' * len(statuses))})"
            params = tuple(StoryStatus(s).value for s in statuses)
        rows = self._execute(f"{sql} ORDER BY last_update_at DESC, id DESC", params)
        return [_story(row) for row in rows]

    def entities(self, story_id: int) -> list[Entity]:
        rows = self._execute(
            "SELECT kind, ref FROM story_entities WHERE story_id = ? ORDER BY kind, ref",
            (story_id,),
        )
        return [Entity(EntityKind(kind), ref) for kind, ref in rows]

    def timeline(self, story_id: int) -> list[StoryUpdate]:
        """The story's updates, oldest first (an undated one by when it was recorded)."""
        rows = self._execute(
            f"SELECT {_UPDATE_COLUMNS} FROM story_updates WHERE story_id = ? "
            "ORDER BY COALESCE(published_at, recorded_at), id",
            (story_id,),
        )
        return [_story_update(row) for row in rows]

    def updates_since(self, moment: datetime, followed_only: bool = False) -> list[StoryUpdate]:
        """Updates recorded at or after `moment`, oldest first; with `followed_only`, only
        those of stories the account follows now."""
        sql = f"SELECT {_UPDATE_COLUMNS} FROM story_updates WHERE recorded_at >= ?"
        params: tuple[object, ...] = (_stamp(moment),)
        if followed_only:
            sql += (
                " AND story_id IN (SELECT story_id FROM subscriptions "
                "WHERE user_id = ? AND active = 1)"
            )
            params += (self.user_id,)
        rows = self._execute(f"{sql} ORDER BY recorded_at, id", params)
        return [_story_update(row) for row in rows]

    def get_update(self, update_id: int) -> StoryUpdate | None:
        row = self._execute(
            f"SELECT {_UPDATE_COLUMNS} FROM story_updates WHERE id = ?", (update_id,)
        ).fetchone()
        return _story_update(row) if row else None

    # --- texts in other languages (F31) ----------------------------------------------------

    def story_text(self, story_id: int, language: str) -> StoryText | None:
        """The story's title and summary in `language`: as stored when that is the language
        they are written in, else the cached text, else None."""
        story = self._require(story_id)
        if story.language == language:
            return StoryText(story.title, story.summary)
        row = self._execute(
            "SELECT title, summary FROM story_texts WHERE story_id = ? AND language = ?",
            (story_id, language),
        ).fetchone()
        return StoryText(*row) if row else None

    def update_text(self, update_id: int, language: str) -> str | None:
        """The update's `what_changed` in `language`: as stored, cached, or None."""
        entry = self.get_update(update_id)
        if entry is None:
            raise StoryError(f"no update {update_id}")
        if entry.update.language == language:
            return entry.update.what_changed
        row = self._execute(
            "SELECT what_changed FROM update_texts WHERE update_id = ? AND language = ?",
            (update_id, language),
        ).fetchone()
        return row[0] if row else None

    def cache_texts(
        self,
        story_id: int,
        language: str,
        story: StoryText | None,
        updates: Mapping[int, str],
        now: datetime,
    ) -> None:
        """Caches generated texts in `language`: the story's title and summary (if given)
        and the `what_changed` of its updates by id, all or none. Raises StoryError for an
        update of another story or a text in the language the row is written in, ValueError
        for an empty text."""
        check_language(language)
        generated = _stamp(now)
        with self._transaction():
            current = self._require(story_id)
            if story is not None:
                if current.language == language:
                    raise StoryError(f"story {story_id} is written in {language} already")
                _check_text("title", story.title)
                _check_text("summary", story.summary)
                self._execute(
                    "INSERT OR REPLACE INTO story_texts (story_id, language, title, summary, "
                    "generated_at) VALUES (?, ?, ?, ?, ?)",
                    (story_id, language, story.title, story.summary, generated),
                )
            for update_id, text in updates.items():
                entry = self.get_update(update_id)
                if entry is None or entry.story_id != story_id:
                    raise StoryError(f"story {story_id} has no update {update_id}")
                if entry.update.language == language:
                    raise StoryError(f"update {update_id} is written in {language} already")
                _check_text("what_changed", text)
                self._execute(
                    "INSERT OR REPLACE INTO update_texts (update_id, language, what_changed, "
                    "generated_at) VALUES (?, ?, ?, ?)",
                    (update_id, language, text, generated),
                )

    # --- subscriptions ---------------------------------------------------------------------

    def subscription(self, story_id: int) -> Subscription | None:
        row = self._execute(
            "SELECT active, followed_at, unfollowed_at, muted_until FROM subscriptions "
            "WHERE user_id = ? AND story_id = ?",
            (self.user_id, story_id),
        ).fetchone()
        if row is None:
            return None
        active, followed_at, unfollowed_at, muted_until = row
        return Subscription(
            story_id=story_id,
            active=bool(active),
            followed_at=datetime.fromisoformat(followed_at),
            unfollowed_at=_moment(unfollowed_at),
            muted_until=_moment(muted_until),
        )

    def follow(self, story_id: int, now: datetime) -> bool:
        """Follows the story; False when it was followed already (nothing changes then).
        Following again after unfollowing starts afresh, without a mute."""
        followed = _stamp(now)
        with self._transaction():
            self._require(story_id)
            cursor = self._execute(
                "INSERT INTO subscriptions (user_id, story_id, active, followed_at) "
                "VALUES (?, ?, 1, ?) "
                "ON CONFLICT (user_id, story_id) DO UPDATE SET active = 1, "
                "followed_at = excluded.followed_at, unfollowed_at = NULL, muted_until = NULL "
                "WHERE active = 0",
                (self.user_id, story_id, followed),
            )
        return cursor.rowcount == 1

    def unfollow(self, story_id: int, now: datetime) -> bool:
        """Stops following the story, which stays in the archive; False when it was not
        followed."""
        cursor = self._execute(
            "UPDATE subscriptions SET active = 0, unfollowed_at = ?, muted_until = NULL "
            "WHERE user_id = ? AND story_id = ? AND active = 1",
            (_stamp(now), self.user_id, story_id),
        )
        return cursor.rowcount == 1

    def mute(self, story_id: int, until: datetime) -> bool:
        """Mutes a followed story until `until`; False when it is not followed."""
        cursor = self._execute(
            "UPDATE subscriptions SET muted_until = ? "
            "WHERE user_id = ? AND story_id = ? AND active = 1",
            (_stamp(until), self.user_id, story_id),
        )
        return cursor.rowcount == 1

    def followed(self) -> list[Story]:
        """The stories the account follows now (muted ones included), the most recently
        updated first."""
        rows = self._execute(
            f"SELECT {', '.join('s.' + c for c in _STORY_COLUMNS.split(', '))} "
            "FROM stories s JOIN subscriptions sub ON sub.story_id = s.id "
            "WHERE sub.user_id = ? AND sub.active = 1 ORDER BY s.last_update_at DESC, s.id DESC",
            (self.user_id,),
        )
        return [_story(row) for row in rows]

    # --- news already checked by story detection -------------------------------------------

    def unchecked(self, urls: Sequence[str]) -> list[str]:
        """The URLs of `urls` that story detection has not checked yet, in the same order."""
        if not urls:
            return []
        checked = {
            url
            for (url,) in self._execute(
                f"SELECT url FROM checked_news WHERE url IN ({', '.join('?' * len(urls))})",
                tuple(urls),
            )
        }
        return [url for url in urls if url not in checked]

    def without_updates(self, urls: Sequence[str]) -> list[str]:
        """The URLs of `urls` that no story has an update from, in the same order."""
        if not urls:
            return []
        stored = {
            url
            for (url,) in self._execute(
                f"SELECT url FROM story_updates WHERE url IN ({', '.join('?' * len(urls))})",
                tuple(urls),
            )
        }
        return [url for url in urls if url not in stored]

    def mark_checked(self, urls: Sequence[str], now: datetime) -> None:
        checked_at = _stamp(now)
        with self._transaction():
            self._execute_many(
                "INSERT OR IGNORE INTO checked_news (url, checked_at) VALUES (?, ?)",
                [(url, checked_at) for url in urls],
            )

    # --- alerts on followed stories ---------------------------------------------------------

    def unalerted(self) -> list[StoryUpdate]:
        """Updates of the stories the account follows now, recorded after it followed them,
        that no alert to it has sent or skipped yet; oldest first."""
        rows = self._execute(
            f"SELECT {', '.join('u.' + c for c in _UPDATE_COLUMNS.split(', '))} "
            "FROM story_updates u JOIN subscriptions sub ON sub.story_id = u.story_id "
            "WHERE sub.user_id = ? AND sub.active = 1 AND u.recorded_at > sub.followed_at "
            "AND u.id NOT IN (SELECT update_id FROM story_alerts WHERE user_id = ?) "
            "ORDER BY u.recorded_at, u.id",
            (self.user_id, self.user_id),
        )
        return [_story_update(row) for row in rows]

    def record_alert(self, updates: Sequence[StoryUpdate], sent: bool, now: datetime) -> None:
        """Marks the updates as sent in one alert at `now`, or as skipped."""
        outcome = "sent" if sent else "skipped"
        decided = _stamp(now)
        with self._transaction():
            self._execute_many(
                "INSERT INTO story_alerts (user_id, update_id, story_id, outcome, decided_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [(self.user_id, u.id, u.story_id, outcome, decided) for u in updates],
            )

    def forget_alert(self, updates: Sequence[StoryUpdate]) -> None:
        """Makes the updates wait for an alert again (one that could not be delivered)."""
        with self._transaction():
            self._execute_many(
                "DELETE FROM story_alerts WHERE user_id = ? AND update_id = ?",
                [(self.user_id, u.id) for u in updates],
            )

    def alerts_sent(self, start: datetime, end: datetime) -> int:
        """How many alerts were sent to the account from `start` up to (not including)
        `end`."""
        row = self._execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT story_id, decided_at FROM story_alerts "
            "WHERE user_id = ? AND outcome = 'sent' AND decided_at >= ? AND decided_at < ?)",
            (self.user_id, _stamp(start), _stamp(end)),
        ).fetchone()
        return row[0]

    # --- offers to unfollow a quiet story ---------------------------------------------------

    def stale_offered(self, story_id: int, quiet_since: datetime) -> bool:
        """Whether the quiet stretch of the story that began at `quiet_since` was offered."""
        row = self._execute(
            "SELECT 1 FROM stale_offers WHERE user_id = ? AND story_id = ? AND quiet_since = ?",
            (self.user_id, story_id, _stamp(quiet_since)),
        ).fetchone()
        return row is not None

    def record_stale_offer(self, story_id: int, quiet_since: datetime, now: datetime) -> None:
        self._execute(
            "INSERT OR IGNORE INTO stale_offers (user_id, story_id, quiet_since, offered_at) "
            "VALUES (?, ?, ?, ?)",
            (self.user_id, story_id, _stamp(quiet_since), _stamp(now)),
        )

    def forget_stale_offer(self, story_id: int, quiet_since: datetime) -> None:
        """Makes the offer due again (one that could not be delivered)."""
        self._execute(
            "DELETE FROM stale_offers WHERE user_id = ? AND story_id = ? AND quiet_since = ?",
            (self.user_id, story_id, _stamp(quiet_since)),
        )

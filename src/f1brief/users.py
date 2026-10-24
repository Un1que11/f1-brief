"""Users instead of a single recipient (F32): accounts and their settings in the state file.

`users` has one row per account: its Telegram user id and chat, name, language, time zone and
status (`active` or `blocked`). `user_settings` holds what an account changed from the
defaults: its favourites, how many days before FP1 the digest comes, the alerts' daily cap
and the quiet hours. A setting that is null (or an account with no settings row) takes the
default from `config.toml`, so editing the config still changes every account that kept the
default; `effective` gives the settings in force for an account.

Misha's account is the owner, `OWNER_ID`. The migration that creates these tables creates it,
and the rows stored before there were users (subscriptions, alerts, the send log) are moved
to it. Its chat is the `TELEGRAM_CHAT_ID` from `.env`, recorded with `bind_owner` the first
time a command that talks to Telegram runs. Until sign-up (F33) and per-user feeds (F34) it is
the only account and every message goes to it.

The subscriptions, alerts and send logs that belong to an account live in their own tables
(see `stories` and `send_log`), each keyed by `user_id`.
"""

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from f1brief.config import LANGUAGES, Config, Favourite, Favourites
from f1brief.state_schema import DEFAULT_STATE_FILE, StateError, migrate

_COMPONENT = "users"

# Misha's account: the first row, created with the tables. The migrations of the other parts
# of the state file move their old rows to this id.
OWNER_ID = 1


class UserStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"


_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        # language and timezone: null takes `[digest]` from config.toml. chat_id is null only
        # for the owner until its chat is known (`bind_owner`).
        """CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            telegram_user_id INTEGER UNIQUE,
            chat_id TEXT UNIQUE,
            name TEXT,
            language TEXT,
            timezone TEXT,
            status TEXT NOT NULL CHECK (status IN ('active', 'blocked')),
            created_at TEXT NOT NULL
        )""",
        # One row per account that changed a setting; a null column takes the default.
        # favourites: JSON {"teams": [...], "drivers": [...]}; quiet hours: "HH:MM".
        """CREATE TABLE user_settings (
            user_id INTEGER NOT NULL PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
            favourites TEXT,
            days_before_fp1 INTEGER CHECK (days_before_fp1 IS NULL OR days_before_fp1 >= 0),
            max_alerts_per_day INTEGER
                CHECK (max_alerts_per_day IS NULL OR max_alerts_per_day >= 0),
            quiet_start TEXT,
            quiet_end TEXT
        )""",
        # The owner, created from the current config: every setting is the default.
        f"INSERT INTO users (id, status, created_at) VALUES ({OWNER_ID}, 'active', "
        "strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'))",
    ),
)

SCHEMA_VERSION = len(_MIGRATIONS)


def ensure_schema(db: sqlite3.Connection, path: str | Path) -> None:
    """Creates or upgrades the user tables (and the owner) in an open state file. Every part
    of the state file whose rows belong to an account calls it first, so the owner exists
    before rows are moved to it."""
    migrate(db, path, _COMPONENT, _MIGRATIONS)


@dataclass(frozen=True)
class User:
    id: int
    telegram_user_id: int | None
    chat_id: str | None
    name: str | None
    language: str | None  # None: the config's
    timezone: str | None  # None: the config's
    status: UserStatus
    created_at: datetime


@dataclass(frozen=True)
class UserSettings:
    """What an account changed from the defaults; None keeps the config's value."""

    favourites: Favourites | None = None
    days_before_fp1: int | None = None
    max_alerts_per_day: int | None = None
    quiet_start: time | None = None
    quiet_end: time | None = None


_KEEP: Any = object()  # `update_settings`: leave the setting as it is

_CLOCK = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


def parse_clock(text: str) -> time:
    """ "23:00" -> time(23). Raises ValueError for anything but a 24-hour HH:MM."""
    match = _CLOCK.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise ValueError(f"a time of day is HH:MM (24-hour), not {text!r}")
    return time(int(match.group(1)), int(match.group(2)))


def check_timezone(name: str) -> None:
    """Raises ValueError for a name that is not an IANA time zone ("America/New_York")."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError(f"unknown time zone {name!r}") from None


def check_language(language: str) -> None:
    if language not in LANGUAGES:
        raise ValueError(
            f"{language!r} is not a supported language (supported: {', '.join(LANGUAGES)})"
        )


def _favourite_json(favourite: Favourite) -> dict[str, Any]:
    return {
        "id": favourite.id,
        "name": favourite.name,
        "aliases": list(favourite.aliases),
        "track_future": favourite.track_future,
        "names": dict(favourite.names),
    }


def favourites_json(favourites: Favourites) -> str:
    return json.dumps(
        {
            "teams": [_favourite_json(f) for f in favourites.teams],
            "drivers": [_favourite_json(f) for f in favourites.drivers],
        },
        ensure_ascii=False,
    )


def favourites_from_json(text: str) -> Favourites:
    data = json.loads(text)

    def entries(key: str) -> tuple[Favourite, ...]:
        return tuple(
            Favourite(
                e["id"],
                e["name"],
                tuple(e.get("aliases") or ()),
                bool(e.get("track_future", False)),
                tuple((k, v) for k, v in (e.get("names") or {}).items()),
            )
            for e in data.get(key, [])
        )

    return Favourites(entries("teams"), entries("drivers"))


def effective(defaults: Config, user: User, settings: UserSettings) -> Config:
    """The settings in force for `user`: the config's, with what the account changed."""
    favourites = settings.favourites if settings.favourites is not None else defaults.favourites
    return replace(
        defaults,
        favourites=favourites,
        language=user.language or defaults.language,
        timezone=user.timezone or defaults.timezone,
        days_before_fp1=_default(settings.days_before_fp1, defaults.days_before_fp1),
        max_alerts_per_day=_default(settings.max_alerts_per_day, defaults.max_alerts_per_day),
        quiet_start=_default(settings.quiet_start, defaults.quiet_start),
        quiet_end=_default(settings.quiet_end, defaults.quiet_end),
    )


def _default(value: Any, default: Any) -> Any:
    return default if value is None else value


_USER_COLUMNS = "id, telegram_user_id, chat_id, name, language, timezone, status, created_at"


def _user(row: tuple) -> User:
    id_, telegram_user_id, chat_id, name, language, timezone, status, created_at = row
    return User(
        id=id_,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        name=name,
        language=language,
        timezone=timezone,
        status=UserStatus(status),
        created_at=datetime.fromisoformat(created_at),
    )


def _private_user_id(chat_id: str) -> int | None:
    # In a private chat the chat id is the user's id; a group's id is negative.
    return int(chat_id) if chat_id.isdigit() else None


class UserStore:
    """The accounts in the state file at `path` (created on first use). Use as a context
    manager."""

    def __init__(self, path: str | Path = DEFAULT_STATE_FILE) -> None:
        self.path = path
        try:
            self._db = sqlite3.connect(path, timeout=10.0, isolation_level=None)
            self._db.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as exc:
            raise StateError(f"cannot open {path}: {exc}") from exc
        ensure_schema(self._db, path)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "UserStore":
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

    # --- accounts --------------------------------------------------------------------------

    def get(self, user_id: int) -> User | None:
        row = self._execute(f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,))
        found = row.fetchone()
        return _user(found) if found else None

    def owner(self) -> User:
        user = self.get(OWNER_ID)
        if user is None:  # deleted by hand: the migration created it
            raise StateError(f"{self.path}: the owner's account (user {OWNER_ID}) is missing")
        return user

    def by_chat(self, chat_id: str) -> User | None:
        row = self._execute(f"SELECT {_USER_COLUMNS} FROM users WHERE chat_id = ?", (chat_id,))
        found = row.fetchone()
        return _user(found) if found else None

    def users(self) -> list[User]:
        return [_user(r) for r in self._execute(f"SELECT {_USER_COLUMNS} FROM users ORDER BY id")]

    def bind_owner(self, chat_id: str) -> User:
        """Records `chat_id` (TELEGRAM_CHAT_ID) as the owner's chat; in a private chat it is
        also the owner's Telegram user id. Changing TELEGRAM_CHAT_ID moves the owner to the
        new chat. Raises StateError when another account has that chat."""
        chat_id = chat_id.strip()
        if not chat_id:
            raise ValueError("chat_id must not be empty")
        owner = self.owner()
        if owner.chat_id == chat_id:
            return owner
        other = self.by_chat(chat_id)
        if other is not None:
            raise StateError(f"{self.path}: chat {chat_id} belongs to user {other.id}")
        self._execute(
            "UPDATE users SET chat_id = ?, telegram_user_id = COALESCE(?, telegram_user_id) "
            "WHERE id = ?",
            (chat_id, _private_user_id(chat_id), OWNER_ID),
        )
        return self.owner()

    def create(
        self,
        chat_id: str,
        now: datetime,
        *,
        telegram_user_id: int | None = None,
        name: str | None = None,
        language: str | None = None,
        timezone: str | None = None,
    ) -> User:
        """A new active account (sign-up itself is F33). Raises StateError when the chat or
        Telegram user has an account, ValueError for an unknown language or time zone."""
        if language is not None:
            check_language(language)
        if timezone is not None:
            check_timezone(timezone)
        if now.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        cursor = self._execute(
            "INSERT INTO users (telegram_user_id, chat_id, name, language, timezone, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, 'active', ?)",
            (
                telegram_user_id,
                chat_id,
                name,
                language,
                timezone,
                now.astimezone(UTC).isoformat(),
            ),
        )
        assert cursor.lastrowid is not None
        return self._require(cursor.lastrowid)

    def _require(self, user_id: int) -> User:
        user = self.get(user_id)
        if user is None:
            raise StateError(f"{self.path}: no user {user_id}")
        return user

    def set_language(self, user_id: int, language: str | None) -> User:
        """None goes back to the config's language."""
        if language is not None:
            check_language(language)
        self._require(user_id)
        self._execute("UPDATE users SET language = ? WHERE id = ?", (language, user_id))
        return self._require(user_id)

    def set_timezone(self, user_id: int, timezone: str | None) -> User:
        """None goes back to the config's time zone."""
        if timezone is not None:
            check_timezone(timezone)
        self._require(user_id)
        self._execute("UPDATE users SET timezone = ? WHERE id = ?", (timezone, user_id))
        return self._require(user_id)

    def set_status(self, user_id: int, status: UserStatus) -> User:
        self._require(user_id)
        self._execute(
            "UPDATE users SET status = ? WHERE id = ?", (UserStatus(status).value, user_id)
        )
        return self._require(user_id)

    # --- settings --------------------------------------------------------------------------

    def settings(self, user_id: int) -> UserSettings:
        row = self._execute(
            "SELECT favourites, days_before_fp1, max_alerts_per_day, quiet_start, quiet_end "
            "FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return UserSettings()
        favourites, days, cap, quiet_start, quiet_end = row
        return UserSettings(
            favourites=favourites_from_json(favourites) if favourites is not None else None,
            days_before_fp1=days,
            max_alerts_per_day=cap,
            quiet_start=parse_clock(quiet_start) if quiet_start else None,
            quiet_end=parse_clock(quiet_end) if quiet_end else None,
        )

    def update_settings(
        self,
        user_id: int,
        *,
        favourites: Favourites | None = _KEEP,
        days_before_fp1: int | None = _KEEP,
        max_alerts_per_day: int | None = _KEEP,
        quiet_start: time | None = _KEEP,
        quiet_end: time | None = _KEEP,
    ) -> UserSettings:
        """Changes the given settings of an account (None: back to the default) and returns
        them all. Raises ValueError for a negative number of days or alerts."""
        for name, value in (
            ("days_before_fp1", days_before_fp1),
            ("max_alerts_per_day", max_alerts_per_day),
        ):
            given = value is not _KEEP and value is not None
            if given and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError(f"{name} must be a whole number, 0 or more")
        self._require(user_id)
        current = self.settings(user_id)
        merged = UserSettings(
            favourites=current.favourites if favourites is _KEEP else favourites,
            days_before_fp1=current.days_before_fp1
            if days_before_fp1 is _KEEP
            else days_before_fp1,
            max_alerts_per_day=(
                current.max_alerts_per_day if max_alerts_per_day is _KEEP else max_alerts_per_day
            ),
            quiet_start=current.quiet_start if quiet_start is _KEEP else quiet_start,
            quiet_end=current.quiet_end if quiet_end is _KEEP else quiet_end,
        )
        self._execute(
            "INSERT OR REPLACE INTO user_settings (user_id, favourites, days_before_fp1, "
            "max_alerts_per_day, quiet_start, quiet_end) VALUES (?, ?, ?, ?, ?, ?)",
            (
                user_id,
                favourites_json(merged.favourites) if merged.favourites is not None else None,
                merged.days_before_fp1,
                merged.max_alerts_per_day,
                merged.quiet_start.strftime("%H:%M") if merged.quiet_start else None,
                merged.quiet_end.strftime("%H:%M") if merged.quiet_end else None,
            ),
        )
        return self.settings(user_id)

    def effective(self, user_id: int, defaults: Config) -> Config:
        """The settings in force for the account: `defaults` (the config) with its changes."""
        return effective(defaults, self._require(user_id), self.settings(user_id))

    # --- favourites ------------------------------------------------------------------------

    def favourites(self, user_id: int, defaults: Favourites) -> Favourites:
        """The account's favourites; `defaults` (the config's) until it changes them."""
        own = self.settings(user_id).favourites
        return own if own is not None else defaults

    def add_favourites(
        self,
        user_id: int,
        defaults: Favourites,
        teams: Sequence[Favourite] = (),
        drivers: Sequence[Favourite] = (),
        write: bool = True,
    ) -> Favourites:
        """Adds the teams and drivers that are not the account's favourites yet and returns
        its favourites. The first change copies the defaults, so the account keeps them. With
        `write=False` nothing is stored: the result is what the account would have."""
        current = self.favourites(user_id, defaults)

        def merged(existing: tuple[Favourite, ...], new: Sequence[Favourite]) -> tuple:
            known = {f.id for f in existing}
            fresh = [f for f in {f.id: f for f in new}.values() if f.id not in known]
            return (*existing, *fresh)

        updated = Favourites(merged(current.teams, teams), merged(current.drivers, drivers))
        if updated != current and write:
            self.update_settings(user_id, favourites=updated)
        return updated

    def remove_favourite(
        self, user_id: int, defaults: Favourites, kind: str, fav_id: str, write: bool = True
    ) -> Favourites:
        """Removes one team (`kind` "teams") or driver ("drivers") from the account's
        favourites and returns them; an id that is not a favourite changes nothing. With
        `write=False` nothing is stored."""
        if kind not in ("teams", "drivers"):
            raise ValueError(f"kind must be 'teams' or 'drivers', not {kind!r}")
        current = self.favourites(user_id, defaults)
        entries: tuple[Favourite, ...] = getattr(current, kind)
        if fav_id not in {f.id for f in entries}:
            return current
        updated = replace(current, **{kind: tuple(f for f in entries if f.id != fav_id)})
        if write:
            self.update_settings(user_id, favourites=updated)
        return updated

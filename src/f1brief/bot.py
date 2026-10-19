# Russian UI texts: Cyrillic letters that look like Latin ones are intended here.
# ruff: noqa: RUF001
"""The interactive Telegram bot for hot stories (F23).

A long-polling listener (`getUpdates`, https://core.telegram.org/bots/api#getupdates) meant to
run as its own process. It answers the commands `/stories`, `/story <id>`, `/follow <id>`,
`/unfollow <id>`, `/mute <id> <days>` and `/archive`, and the inline buttons "Следить",
"Отписаться" and "Следить за <entities>". The last one, offered once a story is followed and
never acted on by itself, adds the story's drivers and teams that are not favourites yet to
`config.toml` (people without a Jolpica id are not favourites, so they are left out).

Only updates from the configured chat are handled; any other update is skipped without an
answer. Replies are plain text (no parse mode), so story titles and summaries need no
escaping. The token is part of every Bot API URL, so it is redacted from every error here.

`Bot.handle` turns one update into the actions to take (`Reply`, `Answer`) and changes the
story store; `BotApi` talks to Telegram; `listen` connects the two. `OffsetStore` keeps the
`getUpdates` offset in the state file, so a restarted listener does not answer an update twice.
"""

import sqlite3
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

from f1brief.config import ConfigError, Favourite, Favourites
from f1brief.schedule import WARSAW
from f1brief.send_log import StateError
from f1brief.stories import (
    Entity,
    EntityKind,
    Story,
    StoryError,
    StoryStatus,
    StoryStore,
    StoryUpdate,
)
from f1brief.telegram_send import API_URL, Credentials, redact

# A long poll waits up to this long for an update; the HTTP read timeout must be longer.
POLL_TIMEOUT_S = 50
# Waits after a failed poll, doubling up to the maximum.
RETRY_MIN_S = 5.0
RETRY_MAX_S = 300.0
# Telegram allows 4096 characters (UTF-16 code units) per message; leave room for emoji.
MAX_LENGTH = 4000
MAX_MUTE_DAYS = 365
# Open stories not followed that `/stories` lists.
RECENT_STORIES = 10
# Telegram answers these to `getUpdates` when polling cannot work at all: a wrong token (401,
# 404) or another listener or a webhook on the same bot (409).
FATAL_STATUSES = frozenset({401, 404, 409})

STATUS_RU = {
    StoryStatus.RUMOR: "слух",
    StoryStatus.DEVELOPING: "развивается",
    StoryStatus.CONFIRMED: "подтверждено",
    StoryStatus.DENIED: "опровергнуто",
    StoryStatus.RESOLVED: "завершён",
}

HELP = (
    "Команды:\n"
    "/stories — сюжеты, за которыми ты следишь, и недавние\n"
    "/story <номер> — хронология сюжета с источниками\n"
    "/follow <номер> — следить за сюжетом\n"
    "/unfollow <номер> — отписаться (сюжет останется в архиве)\n"
    "/mute <номер> <дни> — без уведомлений на N дней\n"
    "/archive — все сюжеты, включая завершённые"
)

FOLLOW, UNFOLLOW, FAVOURITES = "follow", "unfollow", "favs"


class BotError(Exception):
    """A Bot API call failed. `status` is the HTTP status, None when no answer came."""

    def __init__(self, message: str, status: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def fatal(self) -> bool:
        return self.status in FATAL_STATUSES


# --- what the bot does ---------------------------------------------------------------------


@dataclass(frozen=True)
class Button:
    text: str
    data: str  # callback data, at most 64 bytes


@dataclass(frozen=True)
class Reply:
    """A message to the chat, with inline buttons (rows of them)."""

    text: str
    buttons: tuple[tuple[Button, ...], ...] = ()


@dataclass(frozen=True)
class Answer:
    """The answer to a pressed button (a short notice shown in the client)."""

    callback_id: str
    text: str


Action = Reply | Answer


@dataclass(frozen=True)
class Command:
    chat_id: str
    name: str  # without the slash and the bot's @username, lowercase
    args: tuple[str, ...]


@dataclass(frozen=True)
class Press:
    chat_id: str
    callback_id: str
    data: str


def parse_update(update: Mapping[str, Any]) -> Command | Press | None:
    """The message or button press in an update, or None for anything else (edited messages,
    channel posts, a message without text)."""
    message = update.get("message")
    if isinstance(message, dict):
        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(chat, dict) or "id" not in chat or not isinstance(text, str):
            return None
        words = text.split()
        if not words or not words[0].startswith("/"):
            return Command(str(chat["id"]), "", ())
        name = words[0][1:].split("@", 1)[0].lower()
        return Command(str(chat["id"]), name, tuple(words[1:]))
    press = update.get("callback_query")
    if isinstance(press, dict):
        message = press.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        if not isinstance(chat, dict) or "id" not in chat or "id" not in press:
            return None
        return Press(str(chat["id"]), str(press["id"]), str(press.get("data") or ""))
    return None


def favourite_for(entity: Entity) -> Favourite | None:
    """The favourite a story's driver or team becomes: named after its Jolpica id
    ("red_bull" -> "Red Bull"); a driver's news alias is the last word ("Verstappen"). A
    person is not a favourite."""
    words = entity.ref.replace("_", " ").split()
    if entity.kind == EntityKind.PERSON or not words:
        return None
    name = " ".join(w[:1].upper() + w[1:] for w in words)
    if entity.kind == EntityKind.DRIVER:
        return Favourite(entity.ref, name, (name.split()[-1],))
    return Favourite(entity.ref, name)


def _date(moment: datetime) -> str:
    return moment.astimezone(WARSAW).strftime("%d.%m.%Y")


def status_line(entry: StoryUpdate) -> list[str]:
    """The status the update moved its story to, as a line of its own; none if it did not."""
    return [f"Статус → {STATUS_RU[entry.status]}"] if entry.status else []


def _line(story: Story) -> str:
    status = STATUS_RU[story.status]
    return f"#{story.id} {story.title_ru} — {status}, {_date(story.last_update_at)}"


def chunks(text: str, limit: int = MAX_LENGTH) -> list[str]:
    """`text` cut at line ends into pieces of at most `limit` characters."""
    pieces: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if current and len(candidate) > limit:
            pieces.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        pieces.append(current)
    return pieces or [text[:limit]]


def _replies(text: str, buttons: tuple[tuple[Button, ...], ...] = ()) -> list[Action]:
    """The text in as many messages as it needs; the buttons go with the last one."""
    parts = chunks(text)
    return [Reply(p, buttons if i == len(parts) - 1 else ()) for i, p in enumerate(parts)]


AddFavourites = Callable[[Sequence[Favourite], Sequence[Favourite]], Favourites]


@dataclass
class Bot:
    """Handles the updates of one chat against the story store. `add_favourites` stores new
    favourite teams and drivers (in `config.toml`) and returns the favourites then in effect."""

    store: StoryStore
    chat_id: str = field(repr=False)
    favourites: Favourites
    add_favourites: AddFavourites

    def handle(self, update: Mapping[str, Any], now: datetime) -> list[Action]:
        incoming = parse_update(update)
        if incoming is None or incoming.chat_id != self.chat_id:
            return []
        if isinstance(incoming, Press):
            return self._press(incoming, now)
        return self._command(incoming, now)

    # --- commands ------------------------------------------------------------------------

    def _command(self, command: Command, now: datetime) -> list[Action]:
        name, args = command.name, command.args
        if name in ("", "start", "help"):
            return _replies(HELP)
        if name == "stories":
            return self._stories(now)
        if name == "archive":
            return self._archive()
        if name not in ("story", "follow", "unfollow", "mute"):
            return _replies(f"Не знаю команду /{name}.\n\n{HELP}")
        story = self._story_arg(args)
        if isinstance(story, str):
            return _replies(story.replace("<команда>", f"/{name}"))
        if name == "story":
            return self._timeline(story)
        if name == "follow":
            return self._follow(story, now)
        if name == "unfollow":
            return self._unfollow(story, now)
        return self._mute(story, args[1:], now)

    def _story_arg(self, args: Sequence[str]) -> Story | str:
        """The story the first argument names, or why there is none."""
        if not args:
            return "Укажи номер сюжета, например: <команда> 3"
        story_id = _number(args[0].removeprefix("#"))
        story = self.store.get(story_id) if story_id is not None else None
        if story is None:
            return f"Нет сюжета {args[0]}. Все сюжеты: /archive"
        return story

    def _stories(self, now: datetime) -> list[Action]:
        followed = self.store.followed()
        followed_ids = {s.id for s in followed}
        open_statuses = [s for s in StoryStatus if s != StoryStatus.RESOLVED]
        recent = [s for s in self.store.stories(open_statuses) if s.id not in followed_ids]
        recent = recent[:RECENT_STORIES]
        if not followed and not recent:
            return _replies("Сюжетов пока нет. Все сюжеты: /archive")
        lines: list[str] = []
        if followed:
            lines.append("Ты следишь:")
            for story in followed:
                subscription = self.store.subscription(story.id)
                note = ""
                if subscription and subscription.muted_until and subscription.muted(now):
                    note = f" (без уведомлений до {_date(subscription.muted_until)})"
                lines.append(_line(story) + note)
        else:
            lines.append("Ты пока не следишь ни за одним сюжетом.")
        if recent:
            lines += ["", "Недавние сюжеты:", *(_line(s) for s in recent)]
        lines += ["", "Подробнее: /story <номер>"]
        buttons = tuple((Button(f"Отписаться #{s.id}", f"{UNFOLLOW}:{s.id}"),) for s in followed)
        buttons += tuple((Button(f"Следить #{s.id}", f"{FOLLOW}:{s.id}"),) for s in recent)
        return _replies("\n".join(lines), buttons)

    def _archive(self) -> list[Action]:
        every = self.store.stories()
        if not every:
            return _replies("Архив пуст: сюжетов пока нет.")
        followed = {s.id for s in self.store.followed()}
        lines = [f"Все сюжеты ({len(every)}):"]
        lines += [_line(s) + (" — следишь" if s.id in followed else "") for s in every]
        lines += ["", "Подробнее: /story <номер>"]
        return _replies("\n".join(lines))

    def _timeline(self, story: Story) -> list[Action]:
        following = self._following(story.id)
        lines = [
            f"#{story.id} {story.title_ru}",
            f"Статус: {STATUS_RU[story.status]} · {'ты следишь' if following else 'не следишь'}",
            story.summary_ru,
            "",
            "Хронология:",
        ]
        for entry in self.store.timeline(story.id):
            update = entry.update
            when = _date(update.published_at or entry.recorded_at)
            lines += [
                "",
                f"{when} — {update.headline} ({update.source})",
                update.what_changed_ru,
                *status_line(entry),
                update.url,
            ]
        return _replies("\n".join(lines), self._story_buttons(story, following))

    def _follow(self, story: Story, now: datetime) -> list[Action]:
        if self.store.follow(story.id, now):
            text = f"Теперь ты следишь за сюжетом #{story.id} «{story.title_ru}»."
        else:
            text = f"Ты уже следишь за сюжетом #{story.id} «{story.title_ru}»."
        return _replies(text, self._story_buttons(story, following=True))

    def _unfollow(self, story: Story, now: datetime) -> list[Action]:
        if self.store.unfollow(story.id, now):
            text = (
                f"Ты больше не следишь за сюжетом #{story.id} «{story.title_ru}». "
                f"Он остаётся в архиве: /story {story.id}"
            )
        else:
            text = f"Ты не следишь за сюжетом #{story.id}."
        return _replies(text, self._story_buttons(story, following=False))

    def _mute(self, story: Story, args: Sequence[str], now: datetime) -> list[Action]:
        days = _number(args[0]) if args else None
        if days is None or not 1 <= days <= MAX_MUTE_DAYS:
            return _replies(
                f"Укажи число дней от 1 до {MAX_MUTE_DAYS}, например: /mute {story.id} 7"
            )
        until = now + timedelta(days=days)
        if not self.store.mute(story.id, until):
            return _replies(f"Ты не следишь за сюжетом #{story.id}. Сначала: /follow {story.id}")
        when = until.astimezone(WARSAW).strftime("%d.%m.%Y %H:%M")
        return _replies(
            f"Сюжет #{story.id} «{story.title_ru}» без уведомлений до {when}.",
            self._story_buttons(story, following=True),
        )

    # --- buttons -------------------------------------------------------------------------

    def _press(self, press: Press, now: datetime) -> list[Action]:
        action, _, raw_id = press.data.partition(":")
        story_id = _number(raw_id)
        story = self.store.get(story_id) if story_id is not None else None
        if story is None or action not in (FOLLOW, UNFOLLOW, FAVOURITES):
            return [Answer(press.callback_id, "Этой кнопки больше нет")]
        if action == FOLLOW:
            return [Answer(press.callback_id, "Следишь"), *self._follow(story, now)]
        if action == UNFOLLOW:
            return [Answer(press.callback_id, "Отписка"), *self._unfollow(story, now)]
        return self._add_favourites(press, story)

    def _add_favourites(self, press: Press, story: Story) -> list[Action]:
        candidates = self.candidates(story)
        if not candidates:
            return [Answer(press.callback_id, "Уже в избранном")]
        teams = [f for e, f in candidates if e.kind == EntityKind.TEAM]
        drivers = [f for e, f in candidates if e.kind == EntityKind.DRIVER]
        try:
            self.favourites = self.add_favourites(teams, drivers)
        except ConfigError as exc:
            return [
                Answer(press.callback_id, "Не получилось"),
                *_replies(f"Не получилось добавить в избранное: {exc}"),
            ]
        names = ", ".join(f.name for _, f in candidates)
        return [
            Answer(press.callback_id, "Добавлено"),
            *_replies(
                f"Добавил в избранное: {names}. Они появятся в следующем дайджесте.",
                self._story_buttons(story, self._following(story.id)),
            ),
        ]

    def candidates(self, story: Story) -> list[tuple[Entity, Favourite]]:
        """The story's teams, then drivers, that are not favourites yet."""
        known = {
            EntityKind.TEAM: set(self.favourites.team_ids),
            EntityKind.DRIVER: set(self.favourites.driver_ids),
        }
        found: list[tuple[Entity, Favourite]] = []
        entities = self.store.entities(story.id)
        for entity in sorted(entities, key=lambda e: e.kind != EntityKind.TEAM):
            favourite = favourite_for(entity)
            if favourite is not None and favourite.id not in known[entity.kind]:
                found.append((entity, favourite))
        return found

    def _following(self, story_id: int) -> bool:
        subscription = self.store.subscription(story_id)
        return subscription is not None and subscription.active

    def _story_buttons(self, story: Story, following: bool) -> tuple[tuple[Button, ...], ...]:
        if not following:
            return ((Button("Следить", f"{FOLLOW}:{story.id}"),),)
        rows: list[tuple[Button, ...]] = [(Button("Отписаться", f"{UNFOLLOW}:{story.id}"),)]
        candidates = self.candidates(story)
        if candidates:
            names = ", ".join(f.name for _, f in candidates)
            rows.append((Button(f"Следить за {names}", f"{FAVOURITES}:{story.id}"),))
        return tuple(rows)


def _number(text: str) -> int | None:
    return int(text) if text.isdigit() and len(text) <= 9 else None


# --- Telegram ------------------------------------------------------------------------------


@dataclass
class BotApi:
    """The Bot API methods the listener uses."""

    credentials: Credentials
    client: httpx.Client

    def _call(self, method: str, payload: dict[str, Any], timeout: float | None = None) -> Any:
        url = f"{API_URL}/bot{self.credentials.token}/{method}"
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        try:
            response = self.client.post(url, json=payload, **extra)
        except httpx.HTTPError as exc:
            reason = redact(str(exc) or type(exc).__name__, self.credentials.token)
            raise BotError(f"{method} failed: {reason}") from None
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        if response.is_success and isinstance(body, dict) and body.get("ok") is True:
            return body.get("result")
        description = f"HTTP {response.status_code}"
        retry_after = None
        if isinstance(body, dict):
            description = str(body.get("description") or description)
            parameters = body.get("parameters")
            if isinstance(parameters, dict) and isinstance(parameters.get("retry_after"), int):
                retry_after = parameters["retry_after"]
        description = redact(description, self.credentials.token)
        raise BotError(f"{method}: {description}", response.status_code, retry_after)

    def get_updates(self, offset: int | None, timeout: int = POLL_TIMEOUT_S) -> list[Any]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout=timeout + 15)
        return result if isinstance(result, list) else []

    def send(self, action: Action) -> None:
        if isinstance(action, Answer):
            payload = {"callback_query_id": action.callback_id, "text": action.text}
            self._call("answerCallbackQuery", payload)
            return
        payload: dict[str, Any] = {
            "chat_id": self.credentials.chat_id,
            "text": action.text,
            "link_preview_options": {"is_disabled": True},
        }
        if action.buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": b.text, "callback_data": b.data} for b in row]
                    for row in action.buttons
                ]
            }
        self._call("sendMessage", payload)


_OFFSET_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_offset (
    id INTEGER NOT NULL PRIMARY KEY CHECK (id = 1),
    next_update_id INTEGER NOT NULL
)
"""


class OffsetStore:
    """The `getUpdates` offset of the listener, in the state file at `path` (created on first
    use). Telegram forgets an update only once a later poll sends an offset past it, so a
    listener restarted after handling a batch (a crash, launchd restarting it, a reboot
    while it waits to retry a failed poll) would otherwise get the batch again and answer
    it twice. Use as a context manager."""

    def __init__(self, path: str | Path) -> None:
        self.path = path
        try:
            self._db = sqlite3.connect(path, timeout=10.0, isolation_level=None)
            self._db.execute(_OFFSET_SCHEMA)
        except sqlite3.Error as exc:
            raise StateError(f"cannot open {path}: {exc}") from exc

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "OffsetStore":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def load(self) -> int | None:
        try:
            row = self._db.execute("SELECT next_update_id FROM bot_offset").fetchone()
        except sqlite3.Error as exc:
            raise StateError(f"{self.path}: {exc}") from exc
        return row[0] if row else None

    def save(self, offset: int) -> None:
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO bot_offset (id, next_update_id) VALUES (1, ?)", (offset,)
            )
        except sqlite3.Error as exc:
            raise StateError(f"{self.path}: {exc}") from exc


def _warn(text: str) -> None:
    print(f"warning: {text}", file=sys.stderr)


def _update_id(update: Any) -> int | None:
    if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
        return None
    return update["update_id"]


def next_offset(updates: Sequence[Any]) -> int | None:
    """The offset that confirms `updates` (None when none has an id)."""
    ids = [i for i in map(_update_id, updates) if i is not None]
    return max(ids) + 1 if ids else None


def handle_all(
    bot: Bot,
    updates: Sequence[Any],
    now: datetime,
    deliver: Callable[[Action], None],
    warn: Callable[[str], None] = _warn,
) -> int | None:
    """Handles `updates` in order and delivers each action; an update the store fails on and
    a failed delivery are reported and skipped. Returns the offset that confirms them (None
    when there were none)."""
    for update in updates:
        if _update_id(update) is None:
            continue
        try:
            actions = bot.handle(update, now)
        except (StateError, StoryError) as exc:
            warn(f"update not handled: {exc}")
            continue
        for action in actions:
            try:
                deliver(action)
            except BotError as exc:
                warn(f"reply not sent: {exc}")
    return next_offset(updates)


def listen(
    api: BotApi,
    bot: Bot,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    warn: Callable[[str], None] = _warn,
    polls: int | None = None,
    offsets: OffsetStore | None = None,
) -> None:
    """Polls for updates and answers them until stopped (or for `polls` polls). A failed
    poll is retried after a growing pause; a wrong token or a second listener (BotError with
    `fatal`) ends it. With `offsets`, the listener starts from the stored offset and stores
    each batch's offset before handling it: an update is answered at most once, even across
    restarts (one interrupted mid-batch loses the rest of that batch)."""
    offset = offsets.load() if offsets is not None else None
    delay = RETRY_MIN_S
    done = 0
    while polls is None or done < polls:
        done += 1
        try:
            updates = api.get_updates(offset)
        except BotError as exc:
            if exc.fatal:
                raise
            wait = float(exc.retry_after) if exc.retry_after is not None else delay
            warn(f"{exc}; retrying in {wait:.0f} s")
            sleep(wait)
            delay = min(delay * 2, RETRY_MAX_S)
            continue
        delay = RETRY_MIN_S
        batch = next_offset(updates)
        if batch is not None and (offset is None or batch > offset):
            offset = batch
            if offsets is not None:
                try:
                    offsets.save(offset)
                except StateError as exc:
                    warn(f"offset not stored: {exc}")
        handle_all(bot, updates, clock(), api.send, warn)

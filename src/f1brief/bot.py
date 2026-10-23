"""The interactive Telegram bot for hot stories (F23) that explains itself (F40).

A long-polling listener (`getUpdates`, https://core.telegram.org/bots/api#getupdates) meant to
run as its own process. `/start` says what the bot does and when the next digest comes, then
shows the main menu as inline buttons: stories, archive, the next race, favourites, help.
Every other reply carries a "← Меню" button, and a text that is not a command gets the menu.
`/stories` lists each story as a button that opens its timeline, with the buttons "Следить",
"Отписаться", "Без уведомлений на 7 дней" and "Следить за <entities>". The last one, offered
once a story is followed and never acted on by itself, adds the story's drivers and teams that
are not favourites yet to `config.toml` (people without a Jolpica id are not favourites, so
they are left out); the favourites screen removes one with a tap. The typed commands
`/story <id>`, `/follow <id>`, `/unfollow <id>`, `/mute <id> <days>`, `/archive`, `/next` and
`/favourites` keep working. Callback data and command names never depend on the UI language.

Every text comes from `bot_texts`. Only updates from the configured chat are handled; an
update from any other chat gets no answer and is logged to stderr once per chat, so a wrong
`TELEGRAM_CHAT_ID` is visible. Replies are Telegram HTML (F41, `telegram_html`): every text
from `bot_texts`, story titles and summaries included, is escaped, and the story timeline and
the next race are laid out as cards (a bold title, a status chip, the schedule as a `<pre>`
block, a long timeline folded into an expandable quote). The token is part of every Bot API
URL, so it is redacted from every error here.

`/archive` also sends the archive as a static HTML page (F28, `archive_page`) as a file.

`Bot.handle` turns one update into the actions to take (`Reply`, `Answer`, `Document`) and
changes the story store; `BotApi` talks to Telegram; `listen` connects the two. `OffsetStore`
keeps the `getUpdates` offset in the state file, so a restarted listener does not answer an
update twice.
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

from f1brief import archive_page, send_window
from f1brief import bot_texts as texts
from f1brief import telegram_html as th
from f1brief.config import ConfigError, Favourite, Favourites
from f1brief.schedule import Race, ScheduleError, next_race
from f1brief.send_log import StateError
from f1brief.stories import (
    STATUS_RU,
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
# Telegram allows 4096 characters (UTF-16 code units) per message; leave some room.
MAX_LENGTH = 4000
# A timeline longer than this (rendered characters) is folded into an expandable quote.
FOLD_CHARS = 600
MAX_MUTE_DAYS = 365
# The days the "Без уведомлений" button mutes a story for.
MUTE_BUTTON_DAYS = 7
# Open stories not followed that `/stories` lists.
RECENT_STORIES = 10
# Telegram takes at most 100 inline buttons per message: /stories gives at most this many
# stories a button (the rest stay in the list; /archive has them all), leaving room for "← Меню".
MAX_STORY_BUTTONS = 90
# A story's title on its button is cut to this many characters.
BUTTON_TITLE = 48
# Telegram allows at most 64 bytes of callback data.
MAX_CALLBACK_DATA = 64
# Telegram answers these to `getUpdates` when polling cannot work at all: a wrong token (401,
# 404) or another listener or a webhook on the same bot (409).
FATAL_STATUSES = frozenset({401, 404, 409})

HELP = texts.HELP
ARCHIVE_CAPTION = texts.ARCHIVE_CAPTION

# Callback data: "<action>:<story id>[:<days>]", "menu:<screen>", "unfav:<kind>:<id>".
FOLLOW, UNFOLLOW, FAVOURITES = "follow", "unfollow", "favs"
STORY, MUTE, MENU, UNFAVOURITE = "story", "mute", "menu", "unfav"
MAIN, STORIES, ARCHIVE, NEXT, FAVOURITES_SCREEN, HELP_SCREEN = (
    "main",
    "stories",
    "archive",
    "next",
    "favs",
    "help",
)


class BotError(Exception):
    """A Bot API call failed. `status` is the HTTP status, None when no answer came."""

    def __init__(self, message: str, status: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def fatal(self) -> bool:
        return self.status in FATAL_STATUSES


def _warn(text: str) -> None:
    print(f"warning: {text}", file=sys.stderr)


# --- what the bot does ---------------------------------------------------------------------


@dataclass(frozen=True)
class Button:
    text: str
    data: str  # callback data, at most 64 bytes


Keyboard = tuple[tuple[Button, ...], ...]


@dataclass(frozen=True)
class Reply:
    """A message to the chat in Telegram HTML, with inline buttons (rows of them)."""

    text: str
    buttons: Keyboard = ()


@dataclass(frozen=True)
class Answer:
    """The answer to a pressed button (a short notice shown in the client; none when empty)."""

    callback_id: str
    text: str = ""


@dataclass(frozen=True)
class Document:
    """A file sent to the chat (the archive page), with a short caption."""

    filename: str
    content: bytes = field(repr=False)
    mime_type: str
    caption: str


Action = Reply | Answer | Document


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


def status_line(entry: StoryUpdate) -> list[str]:
    """The status the update moved its story to, as a line of its own; none if it did not."""
    return [texts.STATUS_CHANGE.format(status=STATUS_RU[entry.status])] if entry.status else []


def _line(story: Story, numbered: bool = True) -> str:
    template = texts.STORY_LINE_NUMBERED if numbered else texts.STORY_LINE
    return template.format(
        id=story.id,
        title=story.title,
        status=STATUS_RU[story.status],
        date=texts.date(story.last_update_at),
    )


def _short(title: str, limit: int = BUTTON_TITLE) -> str:
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


def status_chip(status: StoryStatus) -> str:
    """ "🟡 слух"."""
    return f"{texts.STATUS_ICONS[status]} {STATUS_RU[status]}"


def source_line(text: str, label: str, url: str) -> th.Line:
    """`text`, then the source as a short linked label."""
    spans = [th.Span(text)] if text else []
    if label:
        spans += [th.Span(" · " if text else ""), th.Span(label, href=url)]
    return th.Line(tuple(spans) or (th.Span(""),))


def folded(lines: Sequence[th.Line], sep: str = "\n") -> bool:
    """Whether lines this long go into an expandable quote."""
    return sum(len(line.render()) for line in lines) + len(sep) * len(lines) > FOLD_CHARS


MENU_ROW = (Button(texts.MENU_BACK, f"{MENU}:{MAIN}"),)

MAIN_MENU: Keyboard = (
    (
        Button(texts.MENU_STORIES, f"{MENU}:{STORIES}"),
        Button(texts.MENU_ARCHIVE, f"{MENU}:{ARCHIVE}"),
    ),
    (Button(texts.MENU_NEXT, f"{MENU}:{NEXT}"),),
    (
        Button(texts.MENU_FAVOURITES, f"{MENU}:{FAVOURITES_SCREEN}"),
        Button(texts.MENU_HELP, f"{MENU}:{HELP_SCREEN}"),
    ),
)


def html_replies(
    blocks: Sequence[th.Block], buttons: Keyboard = (), menu: bool = True
) -> list[Reply]:
    """The blocks in as many HTML messages as they need; the buttons (and, with `menu`, the
    "← Меню" row) go with the last one."""
    if menu:
        buttons = (*buttons, MENU_ROW)
    parts = th.pack(blocks, MAX_LENGTH)
    return [Reply(p, buttons if i == len(parts) - 1 else ()) for i, p in enumerate(parts)]


def _replies(text: str, buttons: Keyboard = (), menu: bool = True) -> list[Action]:
    """Plain text (escaped) as replies, like `html_replies`."""
    return list(html_replies(th.paragraphs(text), buttons, menu))


AddFavourites = Callable[[Sequence[Favourite], Sequence[Favourite]], Favourites]
# Removes a favourite: (kind "teams" or "drivers", id) -> the favourites then in effect.
RemoveFavourite = Callable[[str, str], Favourites]
# When the digest for a round was sent (None: not sent).
DigestSent = Callable[[Race], datetime | None]


def _not_sent(race: Race) -> datetime | None:
    return None


@dataclass
class Bot:
    """Handles the updates of one chat against the story store. `add_favourites` stores new
    favourite teams and drivers (in `config.toml`) and returns the favourites then in effect;
    `remove_favourite` takes one out. `calendar` returns the season's races (it may raise
    `ScheduleError`) and `digest_sent` when a round's digest went out; with them the bot says
    when the next digest comes. `warn` logs updates from other chats."""

    store: StoryStore
    chat_id: str = field(repr=False)
    favourites: Favourites
    add_favourites: AddFavourites
    remove_favourite: RemoveFavourite | None = None
    calendar: Callable[[], Sequence[Race]] | None = None
    digest_sent: DigestSent = _not_sent
    days_before: int = send_window.DEFAULT_DAYS_BEFORE
    warn: Callable[[str], None] = _warn
    ignored_chats: set[str] = field(default_factory=set, repr=False)

    def handle(self, update: Mapping[str, Any], now: datetime) -> list[Action]:
        incoming = parse_update(update)
        if incoming is None:
            return []
        if incoming.chat_id != self.chat_id:
            self._ignore(incoming.chat_id)
            return []
        if isinstance(incoming, Press):
            return self._press(incoming, now)
        return self._command(incoming, now)

    def _ignore(self, chat_id: str) -> None:
        if chat_id in self.ignored_chats:
            return
        self.ignored_chats.add(chat_id)
        self.warn(f"ignored chat {chat_id}; TELEGRAM_CHAT_ID is {self.chat_id}")

    # --- commands ------------------------------------------------------------------------

    def _command(self, command: Command, now: datetime) -> list[Action]:
        name, args = command.name, command.args
        if name in ("", "menu"):
            return _replies(texts.MENU_PROMPT, MAIN_MENU, menu=False)
        if name == "start":
            return self._start(now)
        if name == "help":
            return _replies(texts.HELP)
        if name == "stories":
            return self._stories(now)
        if name == "archive":
            return self._archive(now)
        if name == "next":
            return self._next(now)
        if name in ("favourites", "favorites"):
            return self._favourites_screen()
        if name not in ("story", "follow", "unfollow", "mute"):
            return _replies(f"{texts.UNKNOWN_COMMAND.format(name=name)}\n\n{texts.HELP}")
        story = self._story_arg(args, name)
        if isinstance(story, str):
            return _replies(story)
        if name == "story":
            return self._timeline(story, now)
        if name == "follow":
            return self._follow(story, now)
        if name == "unfollow":
            return self._unfollow(story, now)
        return self._mute(story, args[1:], now)

    def _story_arg(self, args: Sequence[str], command: str) -> Story | str:
        """The story the first argument names, or why there is none."""
        if not args:
            return texts.STORY_NUMBER_MISSING.format(command=command)
        story_id = _number(args[0].removeprefix("#"))
        story = self.store.get(story_id) if story_id is not None else None
        if story is None:
            return texts.STORY_UNKNOWN.format(arg=args[0])
        return story

    def _start(self, now: datetime) -> list[Action]:
        text = texts.start(self.days_before, self._next_digest(now))
        return _replies(text, MAIN_MENU, menu=False)

    def _stories(self, now: datetime) -> list[Action]:
        followed = self.store.followed()
        followed_ids = {s.id for s in followed}
        open_statuses = [s for s in StoryStatus if s != StoryStatus.RESOLVED]
        recent = [s for s in self.store.stories(open_statuses) if s.id not in followed_ids]
        recent = recent[:RECENT_STORIES]
        if not followed and not recent:
            return _replies(texts.NO_STORIES)
        lines: list[str] = []
        buttons: list[tuple[Button, ...]] = []
        if followed:
            lines.append(texts.FOLLOWING)
            for story in followed:
                subscription = self.store.subscription(story.id)
                muted = bool(subscription and subscription.muted_until and subscription.muted(now))
                icon = texts.ICON_MUTED if muted else texts.ICON_FOLLOWED
                note = ""
                if muted and subscription and subscription.muted_until:
                    note = texts.MUTED_UNTIL.format(date=texts.date(subscription.muted_until))
                lines.append(f"{icon} {_line(story, numbered=False)}{note}")
                buttons.append((self._open_button(story, icon),))
        else:
            lines.append(texts.FOLLOWING_NONE)
        if recent:
            lines += ["", texts.RECENT, *(f"• {_line(s, numbered=False)}" for s in recent)]
            buttons += [(self._open_button(s),) for s in recent]
        lines += ["", texts.STORIES_HINT]
        return _replies("\n".join(lines), tuple(buttons[:MAX_STORY_BUTTONS]))

    @staticmethod
    def _open_button(story: Story, icon: str = "") -> Button:
        title = _short(story.title)
        return Button(f"{icon} {title}" if icon else title, f"{STORY}:{story.id}")

    def _archive(self, now: datetime) -> list[Action]:
        every = self.store.stories()
        if not every:
            return _replies(texts.ARCHIVE_EMPTY)
        followed = {s.id for s in self.store.followed()}
        lines = [texts.ARCHIVE_TITLE.format(count=len(every))]
        lines += [_line(s) + (texts.ARCHIVE_FOLLOWED if s.id in followed else "") for s in every]
        lines += ["", texts.ARCHIVE_HINT]
        page = Document(
            archive_page.filename(now),
            archive_page.build(self.store, now).encode("utf-8"),
            "text/html",
            ARCHIVE_CAPTION,
        )
        return [*_replies("\n".join(lines)), page]

    def _timeline(self, story: Story, now: datetime) -> list[Action]:
        following = self._following(story.id)
        title = texts.STORY_TITLE.format(id=story.id, title=story.title)
        head = [
            th.Line((th.Span(f"{texts.ICON_STORY} "), th.Span(title, bold=True))),
            th.plain(
                texts.STORY_STATUS.format(
                    status=status_chip(story.status),
                    following=texts.STORY_FOLLOWED if following else texts.STORY_NOT_FOLLOWED,
                )
            ),
        ]
        if story.summary:
            head.append(th.plain(story.summary))
        entries: list[th.Line] = []
        for entry in self.store.timeline(story.id):
            update = entry.update
            when = texts.date(update.published_at or entry.recorded_at)
            spans = [th.Span(f"{when} — "), th.Span(update.headline, bold=True), th.Span("\n")]
            spans += source_line(update.what_changed, update.source, update.url).spans
            spans += [th.Span(f"\n{line}") for line in status_line(entry)]
            entries.append(th.Line(tuple(spans)))
        blocks = [th.Block(tuple(head))]
        if entries:
            heading = th.Line((th.Span(texts.TIMELINE, bold=True),))
            blocks.append(th.Block(tuple(entries), heading, folded(entries, "\n\n"), "\n\n"))
        return list(html_replies(blocks, self._story_buttons(story, following)))

    def _follow(self, story: Story, now: datetime) -> list[Action]:
        template = texts.FOLLOWED if self.store.follow(story.id, now) else texts.ALREADY_FOLLOWED
        text = template.format(id=story.id, title=story.title)
        return _replies(text, self._story_buttons(story, following=True))

    def _unfollow(self, story: Story, now: datetime) -> list[Action]:
        if self.store.unfollow(story.id, now):
            text = texts.UNFOLLOWED.format(id=story.id, title=story.title)
        else:
            text = texts.NOT_FOLLOWED.format(id=story.id)
        return _replies(text, self._story_buttons(story, following=False))

    def _mute(self, story: Story, args: Sequence[str], now: datetime) -> list[Action]:
        days = _number(args[0]) if args else None
        if days is None or not 1 <= days <= MAX_MUTE_DAYS:
            return _replies(texts.MUTE_DAYS.format(max=MAX_MUTE_DAYS, id=story.id))
        until = now + timedelta(days=days)
        if not self.store.mute(story.id, until):
            return _replies(texts.MUTE_NOT_FOLLOWED.format(id=story.id))
        return _replies(
            texts.MUTED.format(id=story.id, title=story.title, until=texts.date_time(until)),
            self._story_buttons(story, following=True),
        )

    # --- the next race and the digest ----------------------------------------------------

    def _races(self) -> list[Race] | None:
        """The calendar, or None when it cannot be read."""
        if self.calendar is None:
            return None
        try:
            return sorted(self.calendar(), key=lambda r: (r.season, r.round))
        except ScheduleError as exc:
            self.warn(f"calendar: {exc}")
            return None

    def _next_digest(self, now: datetime) -> str:
        """When the next digest comes: the first round whose send window has not closed and
        whose digest has not gone out yet."""
        races = self._races()
        if races is None:
            return texts.CALENDAR_UNAVAILABLE
        try:
            for race in races:
                w = send_window.window(race, self.days_before)
                if now >= w.closes or self.digest_sent(race) is not None:
                    continue
                if now >= w.opens:
                    return texts.NEXT_DIGEST_SOON.format(race=race.name)
                return texts.NEXT_DIGEST_AT.format(race=race.name, when=texts.day_time(w.opens))
        except StateError as exc:
            self.warn(f"send log: {exc}")
            return texts.CALENDAR_UNAVAILABLE
        return texts.NO_DIGEST_PLANNED

    def _next(self, now: datetime) -> list[Action]:
        races = self._races()
        if races is None:
            return _replies(texts.CALENDAR_UNAVAILABLE)
        race = next_race(races, now)
        if race is None:
            return _replies(texts.NO_RACES)
        w = send_window.window(race, self.days_before)
        try:
            sent = self.digest_sent(race)
        except StateError as exc:
            self.warn(f"send log: {exc}")
            sent = None
        if sent is not None:
            digest = texts.DIGEST_SENT.format(when=texts.date_time(sent))
        elif now < w.opens:
            digest = texts.DIGEST_AT.format(when=texts.day_time(w.opens))
        elif now < w.closes:
            digest = texts.DIGEST_SOON
        else:
            digest = texts.DIGEST_NOT_SENT
        return list(html_replies(texts.next_race(race, digest)))

    # --- favourites ----------------------------------------------------------------------

    def _favourites_screen(self, notice: str = "") -> list[Action]:
        teams, drivers = self.favourites.teams, self.favourites.drivers
        text = texts.favourites([t.name for t in teams], [d.name for d in drivers])
        rows: list[tuple[Button, ...]] = []
        if self.remove_favourite is not None:
            for kind, entries in (("teams", teams), ("drivers", drivers)):
                for fav in entries:
                    data = f"{UNFAVOURITE}:{kind}:{fav.id}"
                    if len(data.encode("utf-8")) <= MAX_CALLBACK_DATA:
                        rows.append((Button(texts.REMOVE_BUTTON.format(name=fav.name), data),))
        return _replies(f"{notice}\n\n{text}" if notice else text, tuple(rows))

    def _unfavourite(self, press: Press, kind: str, fav_id: str) -> list[Action]:
        entries = getattr(self.favourites, kind)
        current = next((f for f in entries if f.id == fav_id), None)
        if current is None or self.remove_favourite is None:
            notice = texts.ALREADY_REMOVED.format(name=fav_id)
            return [Answer(press.callback_id), *self._favourites_screen(notice)]
        try:
            self.favourites = self.remove_favourite(kind, fav_id)
        except ConfigError as exc:
            return [
                Answer(press.callback_id, texts.ANSWER_FAILED),
                *_replies(texts.NOT_REMOVED.format(error=exc)),
            ]
        notice = texts.REMOVED.format(name=current.name)
        return [Answer(press.callback_id, texts.ANSWER_REMOVED), *self._favourites_screen(notice)]

    # --- buttons -------------------------------------------------------------------------

    def _press(self, press: Press, now: datetime) -> list[Action]:
        action, _, rest = press.data.partition(":")
        if action == MENU:
            return self._menu(press, rest, now)
        if action == UNFAVOURITE:
            kind, _, fav_id = rest.partition(":")
            if kind in ("teams", "drivers") and fav_id:
                return self._unfavourite(press, kind, fav_id)
            return [Answer(press.callback_id, texts.ANSWER_GONE)]
        raw_id, _, arg = rest.partition(":")
        story_id = _number(raw_id)
        story = self.store.get(story_id) if story_id is not None else None
        if story is None or action not in (FOLLOW, UNFOLLOW, FAVOURITES, STORY, MUTE):
            return [Answer(press.callback_id, texts.ANSWER_GONE)]
        if action == STORY:
            return [Answer(press.callback_id), *self._timeline(story, now)]
        if action == FOLLOW:
            return [Answer(press.callback_id, texts.ANSWER_FOLLOWED), *self._follow(story, now)]
        if action == UNFOLLOW:
            return [Answer(press.callback_id, texts.ANSWER_UNFOLLOWED), *self._unfollow(story, now)]
        if action == MUTE:
            return [Answer(press.callback_id, texts.ANSWER_MUTED), *self._mute(story, [arg], now)]
        return self._add_favourites(press, story)

    def _menu(self, press: Press, screen: str, now: datetime) -> list[Action]:
        if screen == MAIN:
            actions = _replies(texts.MENU_PROMPT, MAIN_MENU, menu=False)
        elif screen == STORIES:
            actions = self._stories(now)
        elif screen == ARCHIVE:
            actions = self._archive(now)
        elif screen == NEXT:
            actions = self._next(now)
        elif screen == FAVOURITES_SCREEN:
            actions = self._favourites_screen()
        elif screen == HELP_SCREEN:
            actions = _replies(texts.HELP)
        else:
            return [Answer(press.callback_id, texts.ANSWER_GONE)]
        return [Answer(press.callback_id), *actions]

    def _add_favourites(self, press: Press, story: Story) -> list[Action]:
        candidates = self.candidates(story)
        if not candidates:
            return [Answer(press.callback_id, texts.ANSWER_ALREADY_FAVOURITE)]
        teams = [f for e, f in candidates if e.kind == EntityKind.TEAM]
        drivers = [f for e, f in candidates if e.kind == EntityKind.DRIVER]
        try:
            self.favourites = self.add_favourites(teams, drivers)
        except ConfigError as exc:
            return [
                Answer(press.callback_id, texts.ANSWER_FAILED),
                *_replies(texts.NOT_ADDED.format(error=exc)),
            ]
        names = ", ".join(f.name for _, f in candidates)
        return [
            Answer(press.callback_id, texts.ANSWER_ADDED),
            *_replies(
                texts.ADDED.format(names=names),
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

    def _story_buttons(self, story: Story, following: bool) -> Keyboard:
        if not following:
            return ((Button(texts.BUTTON_FOLLOW, f"{FOLLOW}:{story.id}"),),)
        rows: list[tuple[Button, ...]] = [
            (Button(texts.BUTTON_UNFOLLOW, f"{UNFOLLOW}:{story.id}"),),
            (Button(texts.button_mute(MUTE_BUTTON_DAYS), f"{MUTE}:{story.id}:{MUTE_BUTTON_DAYS}"),),
        ]
        candidates = self.candidates(story)
        if candidates:
            label = texts.BUTTON_FOLLOW_ENTITIES.format(
                names=", ".join(f.name for _, f in candidates)
            )
            rows.append((Button(label, f"{FAVOURITES}:{story.id}"),))
        return tuple(rows)


def _number(text: str) -> int | None:
    return int(text) if text.isdigit() and len(text) <= 9 else None


# --- Telegram ------------------------------------------------------------------------------


@dataclass
class BotApi:
    """The Bot API methods the listener uses."""

    credentials: Credentials
    client: httpx.Client

    def _call(
        self,
        method: str,
        payload: dict[str, Any],
        timeout: float | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> Any:
        url = f"{API_URL}/bot{self.credentials.token}/{method}"
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        # A file goes as multipart/form-data, everything else as JSON.
        extra.update({"data": payload, "files": files} if files else {"json": payload})
        try:
            response = self.client.post(url, **extra)
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

    def set_commands(self, commands: Sequence[tuple[str, str]]) -> None:
        """The command menu under the "/" button: (command, description) pairs."""
        menu = [{"command": c, "description": d} for c, d in commands]
        self._call("setMyCommands", {"commands": menu})

    def get_me(self) -> dict[str, Any]:
        result = self._call("getMe", {})
        return result if isinstance(result, dict) else {}

    def webhook_info(self) -> dict[str, Any]:
        result = self._call("getWebhookInfo", {})
        return result if isinstance(result, dict) else {}

    def send(self, action: Action) -> None:
        if isinstance(action, Answer):
            answer = {"callback_query_id": action.callback_id}
            if action.text:
                answer["text"] = action.text
            self._call("answerCallbackQuery", answer)
            return
        if isinstance(action, Document):
            fields = {"chat_id": self.credentials.chat_id, "caption": action.caption}
            files = {"document": (action.filename, action.content, action.mime_type)}
            self._call("sendDocument", fields, files=files)
            return
        payload: dict[str, Any] = {
            "chat_id": self.credentials.chat_id,
            "text": action.text,
            "parse_mode": th.PARSE_MODE,
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


def register_commands(api: BotApi, warn: Callable[[str], None] = _warn) -> bool:
    """Registers the command menu (`setMyCommands`) when the listener starts. A failure is
    reported and does not stop the listener: the commands still work when typed."""
    try:
        api.set_commands(texts.COMMANDS)
    except BotError as exc:
        warn(f"command menu not registered: {exc}")
        return False
    return True


def check(api: BotApi, chat_id: str) -> list[str]:
    """What `f1brief bot --check` prints: the bot's username, whether a webhook is set (which
    blocks polling) and the configured chat id. Raises BotError when Telegram cannot be asked."""
    me = api.get_me()
    webhook = api.webhook_info()
    username = me.get("username")
    lines = [f"bot: @{username}" if username else "bot: (no username)"]
    if webhook.get("url"):
        pending = webhook.get("pending_update_count", 0)
        lines.append(
            f"webhook: set ({pending} pending update(s)); it blocks polling, remove it with "
            "the Bot API method deleteWebhook"
        )
    else:
        lines.append("webhook: none (polling works)")
    lines.append(f"TELEGRAM_CHAT_ID: {chat_id}")
    return lines


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

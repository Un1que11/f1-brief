"""Alerts on followed stories (F24): a short message when a followed story gets an update.

An update is alerted on when its story is followed and it was recorded after the story was
followed. One alert covers all of a story's waiting updates, each with its source, and
carries the "Хронология" and "Отписаться" buttons the bot (F23) answers. It reads like a
card (F41): `🔔 <b>title</b>`, the story's status chip, then one line per update with what
changed and the source as a short linked label. At most `MAX_PER_DAY` alerts go out
per calendar day, and none in the quiet hours (23:00-08:00 by default): those wait for the
first run after them, the oldest first. The day and the quiet hours are the account's local
time (`QuietHours`, Warsaw by default; F32 makes the zone, the hours and the cap settings).
Updates of a muted story are skipped, not saved for later, and so is an update that has
waited longer than `STALE_AFTER` (the digest's stories section covers both). Alerts are in
the bot's language (F30, `bot_texts`).

An alert is recorded as sent before it is sent, so it goes out at most once; one Telegram
refuses outright is made to wait again. `plan` decides, `deliver` sends and records; the clock
is always passed in.
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo

from f1brief import telegram_html as th
from f1brief.bot import STORY, UNFOLLOW, BotError, Button, Reply, html_replies, source_line
from f1brief.bot_texts import ICON_ALERT, texts_for
from f1brief.messages import DEFAULT_LANGUAGE
from f1brief.schedule import WARSAW
from f1brief.stories import Story, StoryStore, StoryUpdate

MAX_PER_DAY = 3
# Local wall-clock time: from QUIET_START until QUIET_END the next morning.
QUIET_START = time(23)
QUIET_END = time(8)
STALE_AFTER = timedelta(days=3)


@dataclass(frozen=True)
class QuietHours:
    """No alerts from `start` to `end` (wall-clock time in `tz`); a window that crosses
    midnight (23:00-08:00) ends the next morning, and equal times mean no quiet hours."""

    start: time = QUIET_START
    end: time = QUIET_END
    tz: tzinfo = WARSAW

    def contains(self, now: datetime) -> bool:
        local = now.astimezone(self.tz).time()
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= local < self.end
        return local >= self.start or local < self.end


DEFAULT_QUIET = QuietHours()


def is_quiet(now: datetime, hours: QuietHours = DEFAULT_QUIET) -> bool:
    return hours.contains(now)


def day_bounds(now: datetime, tz: tzinfo = WARSAW) -> tuple[datetime, datetime]:
    """The calendar day in `tz` that `now` falls in, from midnight to the next midnight (23
    or 25 hours long on the days the clocks change)."""
    day = now.astimezone(tz).date()
    start = datetime.combine(day, time(), tz)
    return start, datetime.combine(day + timedelta(days=1), time(), tz)


@dataclass(frozen=True)
class Alert:
    story: Story
    updates: tuple[StoryUpdate, ...]


@dataclass(frozen=True)
class Plan:
    send: tuple[Alert, ...]
    # Left for a later run: in the quiet hours, or over the day's limit.
    waiting: tuple[Alert, ...]
    # Never to be alerted on: the story is muted, or the update waited too long.
    skip: tuple[StoryUpdate, ...]
    quiet: bool

    def describe(self) -> str:
        parts = [f"{len(self.send)} to send"]
        if self.waiting:
            why = "quiet hours" if self.quiet else f"at most {MAX_PER_DAY} a day"
            parts.append(f"{len(self.waiting)} waiting ({why})")
        if self.skip:
            parts.append(f"{len(self.skip)} update(s) skipped (muted or too old)")
        return "story alerts: " + ", ".join(parts)


def plan(
    store: StoryStore,
    now: datetime,
    max_per_day: int = MAX_PER_DAY,
    hours: QuietHours = DEFAULT_QUIET,
) -> Plan:
    """What to send now to the store's account, what waits and what is skipped. Alerts are
    ordered by their oldest update; the daily cap counts the account's local day."""
    skip: list[StoryUpdate] = []
    by_story: dict[int, list[StoryUpdate]] = {}
    for update in store.unalerted():
        subscription = store.subscription(update.story_id)
        muted = subscription is not None and subscription.muted(now)
        if muted or now - update.recorded_at > STALE_AFTER:
            skip.append(update)
        else:
            by_story.setdefault(update.story_id, []).append(update)
    alerts: list[Alert] = []
    for story_id, updates in by_story.items():
        story = store.get(story_id)
        if story is not None:
            alerts.append(Alert(story, tuple(updates)))
    quiet = is_quiet(now, hours)
    room = 0 if quiet else max(0, max_per_day - store.alerts_sent(*day_bounds(now, hours.tz)))
    return Plan(tuple(alerts[:room]), tuple(alerts[room:]), tuple(skip), quiet)


def blocks(alert: Alert, language: str = DEFAULT_LANGUAGE) -> list[th.Block]:
    texts = texts_for(language)
    story = alert.story
    chip = texts.status_chip(story.status)
    if any(entry.status for entry in alert.updates):
        chip = texts.alert_status_changed.format(chip=chip)
    head = th.Block(
        (
            th.Line((th.Span(f"{ICON_ALERT} "), th.Span(story.title, bold=True))),
            th.plain(chip),
        )
    )
    lines = tuple(
        source_line(f"• {e.update.what_changed}", e.update.source, e.update.url)
        for e in alert.updates
    )
    return [head, th.Block(lines)]


def text(alert: Alert, language: str = DEFAULT_LANGUAGE) -> str:
    """The alert's first message, as Telegram HTML."""
    return replies(alert, language)[0].text


def replies(alert: Alert, language: str = DEFAULT_LANGUAGE) -> list[Reply]:
    """The alert's messages (one unless it is long); the last carries the buttons."""
    texts = texts_for(language)
    story_id = alert.story.id
    buttons = (
        (
            Button(texts.button_timeline, f"{STORY}:{story_id}"),
            Button(texts.button_unfollow, f"{UNFOLLOW}:{story_id}"),
        ),
    )
    return html_replies(blocks(alert, language), buttons)


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def deliver(
    store: StoryStore,
    planned: Plan,
    now: datetime,
    send: Callable[[Reply], None],
    warn: Callable[[str], None] = _warn,
    language: str = DEFAULT_LANGUAGE,
) -> bool:
    """Records the skipped updates, then sends the planned alerts in order. When Telegram
    refuses one, the rest wait for the next run; the refused one waits too unless part of
    it was delivered. False when an alert failed."""
    if planned.skip:
        store.record_alert(planned.skip, sent=False, now=now)
    for alert in planned.send:
        store.record_alert(alert.updates, sent=True, now=now)
        delivered = 0
        try:
            for reply in replies(alert, language):
                send(reply)
                delivered += 1
        except BotError as exc:
            if delivered == 0:
                store.forget_alert(alert.updates)
            warn(f"alert on story #{alert.story.id} not sent: {exc}")
            return False
    return True

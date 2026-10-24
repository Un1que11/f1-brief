"""Alerts on followed stories (F24): a short message when a followed story gets an update.

An update is alerted on when its story is followed and it was recorded after the story was
followed. One alert covers all of a story's waiting updates, each with its source, and
carries the "Отписаться" button the bot (F23) answers. At most `MAX_PER_DAY` alerts go out
per calendar day, and none in the quiet hours (23:00-08:00 by default): those wait for the
first run after them, the oldest first. The day and the quiet hours are the account's local
time (`QuietHours`, Warsaw by default; F32 makes the zone, the hours and the cap settings).
Updates of a muted story are skipped, not saved for later, and so is an update that has
waited longer than `STALE_AFTER` (the digest's "Твои сюжеты" covers both).

An alert is recorded as sent before it is sent, so it goes out at most once; one Telegram
refuses outright is made to wait again. `plan` decides, `deliver` sends and records; the clock
is always passed in.
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo

from f1brief.bot import STATUS_RU, UNFOLLOW, BotError, Button, Reply, chunks, status_line
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


def text(alert: Alert) -> str:
    story = alert.story
    lines = [f"Новое в сюжете #{story.id} «{story.title}» ({STATUS_RU[story.status]})"]
    for entry in alert.updates:
        update = entry.update
        lines += [
            "",
            update.what_changed,
            *status_line(entry),
            f"{update.headline} ({update.source})",
            update.url,
        ]
    lines += ["", f"Хронология: /story {story.id}"]
    return "\n".join(lines)


def replies(alert: Alert) -> list[Reply]:
    """The alert's messages (one unless it is long); the last carries "Отписаться"."""
    buttons = ((Button("Отписаться", f"{UNFOLLOW}:{alert.story.id}"),),)
    parts = chunks(text(alert))
    return [Reply(p, buttons if i == len(parts) - 1 else ()) for i, p in enumerate(parts)]


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def deliver(
    store: StoryStore,
    planned: Plan,
    now: datetime,
    send: Callable[[Reply], None],
    warn: Callable[[str], None] = _warn,
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
            for reply in replies(alert):
                send(reply)
                delivered += 1
        except BotError as exc:
            if delivered == 0:
                store.forget_alert(alert.updates)
            warn(f"alert on story #{alert.story.id} not sent: {exc}")
            return False
    return True

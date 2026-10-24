"""Story lifecycle (F26): one offer to unfollow a followed story that has gone quiet.

A story's status moves only with a sourced update and only forward (`stories.can_move`); this
module covers the other half of "Lifecycle" in CLAUDE.md. A followed story with no update for
`QUIET_AFTER` gets one message offering to unfollow it, with the "Отписаться" button the bot
(F23) answers. The quiet stretch counts from the story's latest update, or from when it was
followed if that is later, so a story followed after a long silence is not offered at once.
Each stretch is offered once: a later update starts a new one.

Offers wait while the story is muted and in the account's quiet hours (`alerts.QuietHours`),
like alerts,
but do not count towards the alerts' daily limit: there is at most one per story per stretch.
An offer is recorded before it is sent, so it goes out at most once; one Telegram refuses is
made due again. `plan` decides, `deliver` sends and records; the clock is always passed in.
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from f1brief.alerts import DEFAULT_QUIET, QuietHours, is_quiet
from f1brief.bot import STATUS_RU, UNFOLLOW, BotError, Button, Reply
from f1brief.bot_texts import days_ru
from f1brief.schedule import WARSAW
from f1brief.stories import Story, StoryStore

QUIET_AFTER = timedelta(days=60)


@dataclass(frozen=True)
class Offer:
    story: Story
    quiet_since: datetime


@dataclass(frozen=True)
class Plan:
    send: tuple[Offer, ...]
    # Due, but left for the first run after the quiet hours.
    waiting: tuple[Offer, ...]

    def describe(self) -> str:
        parts = [f"{len(self.send)} to send"]
        if self.waiting:
            parts.append(f"{len(self.waiting)} waiting (quiet hours)")
        return "offers to unfollow quiet stories: " + ", ".join(parts)


def plan(
    store: StoryStore,
    now: datetime,
    quiet_after: timedelta = QUIET_AFTER,
    hours: QuietHours = DEFAULT_QUIET,
) -> Plan:
    """The offers due at `now` to the store's account, the longest-quiet story first."""
    due: list[Offer] = []
    for story in store.followed():
        subscription = store.subscription(story.id)
        if subscription is None or subscription.muted(now):
            continue
        quiet_since = max(story.last_update_at, subscription.followed_at)
        if now - quiet_since < quiet_after or store.stale_offered(story.id, quiet_since):
            continue
        due.append(Offer(story, quiet_since))
    due.sort(key=lambda o: (o.quiet_since, o.story.id))
    if is_quiet(now, hours):
        return Plan((), tuple(due))
    return Plan(tuple(due), ())


def text(offer: Offer, now: datetime, tz: tzinfo = WARSAW) -> str:
    story = offer.story
    last = story.last_update_at.astimezone(tz).strftime("%d.%m.%Y")
    return "\n".join(
        [
            f"Сюжет #{story.id} «{story.title}» ({STATUS_RU[story.status]}): "
            f"нет новостей уже {days_ru((now - offer.quiet_since).days)}, "
            f"последнее обновление — {last}.",
            "Отписаться? Если хочешь следить дальше, ничего не нажимай: новости по нему "
            "придут как обычно.",
            "",
            f"Хронология: /story {story.id}",
        ]
    )


def reply(offer: Offer, now: datetime, tz: tzinfo = WARSAW) -> Reply:
    return Reply(text(offer, now, tz), ((Button("Отписаться", f"{UNFOLLOW}:{offer.story.id}"),),))


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def deliver(
    store: StoryStore,
    planned: Plan,
    now: datetime,
    send: Callable[[Reply], None],
    warn: Callable[[str], None] = _warn,
    tz: tzinfo = WARSAW,
) -> bool:
    """Sends the planned offers in order. When Telegram refuses one, it and the rest wait
    for the next run. False when an offer failed."""
    for offer in planned.send:
        store.record_stale_offer(offer.story.id, offer.quiet_since, now)
        try:
            send(reply(offer, now, tz))
        except BotError as exc:
            store.forget_stale_offer(offer.story.id, offer.quiet_since)
            warn(f"offer to unfollow story #{offer.story.id} not sent: {exc}")
            return False
    return True

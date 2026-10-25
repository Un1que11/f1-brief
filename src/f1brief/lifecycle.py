"""Story lifecycle (F26): one offer to unfollow a followed story that has gone quiet.

A story's status moves only with a sourced update and only forward (`stories.can_move`); this
module covers the other half of "Lifecycle" in CLAUDE.md. A followed story with no update for
`QUIET_AFTER` gets one message offering to unfollow it, with the unfollow button the bot
(F23) answers, in the bot's language (F30, `bot_texts`). The quiet stretch counts from the
story's latest update, or from when it was followed if that is later, so a story followed
after a long silence is not offered at once.
Each stretch is offered once: a later update starts a new one.

Offers wait while the story is muted and in the quiet hours (23:00-08:00 Warsaw), like alerts,
but do not count towards the alerts' daily limit: there is at most one per story per stretch.
An offer is recorded before it is sent, so it goes out at most once; one Telegram refuses is
made due again. `plan` decides, `deliver` sends and records; the clock is always passed in.
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from f1brief.alerts import is_quiet
from f1brief.bot import UNFOLLOW, BotError, Button, Reply
from f1brief.bot_texts import texts_for
from f1brief.messages import DEFAULT_LANGUAGE
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


def plan(store: StoryStore, now: datetime, quiet_after: timedelta = QUIET_AFTER) -> Plan:
    """The offers due at `now`, the longest-quiet story first."""
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
    if is_quiet(now):
        return Plan((), tuple(due))
    return Plan(tuple(due), ())


def text(offer: Offer, now: datetime, language: str = DEFAULT_LANGUAGE) -> str:
    texts = texts_for(language)
    story = offer.story
    offer_text = texts.stale_offer.format(
        id=story.id,
        title=story.title,
        status=texts.status(story.status),
        days=texts.days((now - offer.quiet_since).days),
        date=texts.date(story.last_update_at),
    )
    return "\n".join([offer_text, "", texts.timeline_link.format(id=story.id)])


def reply(offer: Offer, now: datetime, language: str = DEFAULT_LANGUAGE) -> Reply:
    button = Button(texts_for(language).button_unfollow, f"{UNFOLLOW}:{offer.story.id}")
    return Reply(text(offer, now, language), ((button,),))


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
    """Sends the planned offers in order. When Telegram refuses one, it and the rest wait
    for the next run. False when an offer failed."""
    for offer in planned.send:
        store.record_stale_offer(offer.story.id, offer.quiet_since, now)
        try:
            send(reply(offer, now, language))
        except BotError as exc:
            store.forget_stale_offer(offer.story.id, offer.quiet_since)
            warn(f"offer to unfollow story #{offer.story.id} not sent: {exc}")
            return False
    return True

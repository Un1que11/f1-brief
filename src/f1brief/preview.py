"""Sample messages (F41) to check how each kind looks in Telegram: `f1brief preview`.

The digest, the message after qualifying and the off-season summary come from saved sample
cards (`samples/<kind>.json`: synthetic text with example links, marked as a sample), rendered
the way `run` renders a generated card; the story alert is built from a sample story the way
`run` builds a real one. Nothing here calls claude, Jolpica or any other source.
"""

import json
from datetime import UTC, datetime
from importlib import resources

from f1brief import alerts, cards
from f1brief.bot import Reply
from f1brief.stories import Story, StoryStatus, StoryUpdate, Update

KINDS = ("digest", "quali", "alert", "summary")
SAMPLE_TIME = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)


def sample_card(kind: str) -> cards.Card:
    path = resources.files("f1brief").joinpath("samples", f"{kind}.json")
    return cards.from_dict(json.loads(path.read_text(encoding="utf-8")))


def sample_alert() -> alerts.Alert:
    story = Story(
        id=1,
        slug="sample-seat",
        title="Образец: пилот Haas может перейти в другую команду в 2028",
        summary="Образец сюжета для проверки вида сообщений.",
        status=StoryStatus.DEVELOPING,
        created_at=SAMPLE_TIME,
        last_update_at=SAMPLE_TIME,
        language="ru",
    )
    update = Update(
        headline="Sample headline <with> & special characters",
        url="https://example.com/sample/story-update?a=1&b=2",
        source="Motorsport.com",
        what_changed="Образец: команда подтвердила переговоры, решение ждут после сезона.",
        published_at=SAMPLE_TIME,
        language="ru",
    )
    entry = StoryUpdate(1, story.id, update, SAMPLE_TIME, StoryStatus.DEVELOPING)
    return alerts.Alert(story, (entry,))


def replies(kind: str) -> list[Reply]:
    """The sample messages of `kind`, as `run` would send them."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if kind == "alert":
        return alerts.replies(sample_alert())
    return [Reply(text) for text in cards.render(sample_card(kind))]

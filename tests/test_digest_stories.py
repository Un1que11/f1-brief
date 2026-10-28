"""The digest's "Твои сюжеты" section (F25): the followed stories' updates since the last
digest, read from synthetic stories in a temporary state file with an injected clock."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import facts_transport
import pytest

from f1brief import cli, facts
from f1brief.cli import main
from f1brief.config import DEFAULT_FAVOURITES
from f1brief.send_log import SendLog, StateError
from f1brief.stories import Entity, EntityKind, Story, StoryStatus, StoryStore, Update

NOW = datetime(2026, 10, 21, 8, tzinfo=UTC)  # Wed 10:00 in Warsaw
LAST_DIGEST = datetime(2026, 10, 1, 8, tzinfo=UTC)
FACTS_BLOCK = "<facts>\n"


def update(n: int, published: datetime | None = None) -> Update:
    return Update(
        headline=f"Synthetic headline {n}",
        url=f"https://example.com/story-{n}",
        source=f"Outlet {n % 2}",
        what_changed=f"Синтетика: изменение {n}.",
        language="ru",
        published_at=published,
    )


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def store(state: Path) -> Iterator[StoryStore]:
    with StoryStore(state) as opened:
        yield opened


def story(store: StoryStore, n: int, entities: list[Entity], recorded: datetime) -> Story:
    return store.create(
        f"story-{n}",
        f"Сюжет {n}",
        "Синтетика: описание.",
        StoryStatus.RUMOR,
        entities,
        update(100 * n, recorded - timedelta(hours=2)),
        recorded,
        language="ru",
    )


BEFORE = LAST_DIGEST - timedelta(days=5)
AFTER = LAST_DIGEST + timedelta(days=3)


@pytest.fixture
def stories(store: StoryStore) -> dict[str, Story]:
    """A followed story with an update before and two after the last digest, a followed one
    with nothing new, a muted one with news, an unfollowed and a never-followed one with
    news."""
    seat = story(
        store,
        1,
        [
            Entity(EntityKind.DRIVER, "bearman"),
            Entity(EntityKind.TEAM, "haas"),
            Entity(EntityKind.TEAM, "ferrari"),
            Entity(EntityKind.PERSON, "fred_vasseur"),
        ],
        BEFORE,
    )
    store.follow(seat.id, BEFORE)
    store.add_update(seat.id, update(11, AFTER - timedelta(hours=1)), AFTER)
    # Undated, recorded later: listed by when it was recorded.
    store.add_update(seat.id, update(12), AFTER + timedelta(days=1), StoryStatus.DEVELOPING)

    quiet = story(store, 2, [Entity(EntityKind.DRIVER, "russell")], BEFORE)
    store.follow(quiet.id, BEFORE)

    muted = story(store, 3, [Entity(EntityKind.DRIVER, "ocon")], BEFORE)
    store.follow(muted.id, BEFORE)
    store.mute(muted.id, NOW + timedelta(days=7))
    store.add_update(muted.id, update(31, AFTER), AFTER)

    dropped = story(store, 4, [Entity(EntityKind.TEAM, "mclaren")], BEFORE)
    store.follow(dropped.id, BEFORE)
    store.unfollow(dropped.id, BEFORE + timedelta(days=1))
    store.add_update(dropped.id, update(41, AFTER), AFTER)

    never = story(store, 5, [Entity(EntityKind.DRIVER, "hamilton")], AFTER)
    return {"seat": seat, "quiet": quiet, "muted": muted, "dropped": dropped, "never": never}


def section(store: StoryStore) -> dict[str, Any]:
    return facts.stories_facts(store, LAST_DIGEST, True, DEFAULT_FAVOURITES)


def by_id(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {row["id"]: row for row in rows}


# --- the section ------------------------------------------------------------------------


def test_every_followed_story_and_only_those(store: StoryStore, stories: dict[str, Story]) -> None:
    listed = by_id(section(store)["followed"])

    assert set(listed) == {stories[k].id for k in ("seat", "quiet", "muted")}


def test_only_updates_since_the_last_digest_with_their_sources(
    store: StoryStore, stories: dict[str, Story]
) -> None:
    data = section(store)
    seat = by_id(data["followed"])[stories["seat"].id]

    assert data["since"] == "2026-10-01T10:00:00+02:00"
    assert data["since_last_digest"] is True
    assert seat["title"] == "Сюжет 1"
    assert seat["summary"] == "Синтетика: описание."
    assert seat["status"] == "developing"
    assert seat["followed_since"] == "2026-09-26T10:00:00+02:00"
    # The first update (before the last digest) is left out; the rest are oldest first.
    assert seat["updates_since"] == [
        {
            "published": "2026-10-04T09:00:00+02:00",
            "recorded": "2026-10-04T10:00:00+02:00",
            "headline": "Synthetic headline 11",
            "what_changed": "Синтетика: изменение 11.",
            "language": "ru",
            "source": "Outlet 1",
            "link": "https://example.com/story-11",
        },
        {
            "published": None,
            "recorded": "2026-10-05T10:00:00+02:00",
            "headline": "Synthetic headline 12",
            "what_changed": "Синтетика: изменение 12.",
            "language": "ru",
            "source": "Outlet 0",
            "link": "https://example.com/story-12",
        },
    ]
    # Every update in the section is a source of it, once; nothing else is.
    assert data["sources"] == [
        {"name": "Outlet 1", "url": "https://example.com/story-11"},
        {"name": "Outlet 0", "url": "https://example.com/story-12"},
        {"name": "Outlet 1", "url": "https://example.com/story-31"},
    ]


def test_a_story_with_nothing_new_is_still_listed(
    store: StoryStore, stories: dict[str, Story]
) -> None:
    quiet = by_id(section(store)["followed"])[stories["quiet"].id]

    assert quiet["updates_since"] == []
    assert quiet["status"] == "rumor"


def test_a_muted_story_is_covered(store: StoryStore, stories: dict[str, Story]) -> None:
    # Muting silences the alerts; the digest still covers the story.
    muted = by_id(section(store)["followed"])[stories["muted"].id]

    assert [u["link"] for u in muted["updates_since"]] == ["https://example.com/story-31"]


def test_entities_that_are_favourites_are_marked(
    store: StoryStore, stories: dict[str, Story]
) -> None:
    seat = by_id(section(store)["followed"])[stories["seat"].id]

    assert seat["entities"] == [
        {"kind": "driver", "ref": "bearman", "favourite": True},
        {"kind": "person", "ref": "fred_vasseur", "favourite": False},
        {"kind": "team", "ref": "ferrari", "favourite": False},
        {"kind": "team", "ref": "haas", "favourite": True},
    ]


def test_favourites_come_from_the_config(store: StoryStore, stories: dict[str, Story]) -> None:
    ferrari_fan = DEFAULT_FAVOURITES.only(["ferrari"], [])
    data = facts.stories_facts(store, LAST_DIGEST, True, ferrari_fan)
    seat = by_id(data["followed"])[stories["seat"].id]

    assert {e["ref"]: e["favourite"] for e in seat["entities"]} == {
        "bearman": False,
        "fred_vasseur": False,
        "ferrari": True,
        "haas": False,
    }


def test_nothing_followed(store: StoryStore) -> None:
    data = section(store)

    assert data["followed"] == [] and data["sources"] == []
    assert json.loads(json.dumps(data)) == data


@pytest.mark.parametrize(
    ("last_digest", "expected"),
    [
        (LAST_DIGEST, LAST_DIGEST),
        (None, NOW - timedelta(days=facts.STORIES_DAYS)),
    ],
)
def test_since_the_last_digest_or_a_fixed_window(
    last_digest: datetime | None, expected: datetime
) -> None:
    assert facts.stories_since(last_digest, NOW) == expected


# --- the last digest --------------------------------------------------------------------


def test_last_digest_is_the_latest_round_sent_before_now(state: Path) -> None:
    with SendLog(state) as log:
        assert log.last_digest_at(NOW) is None
        log.claim(
            2026,
            16,
            LAST_DIGEST - timedelta(days=14),
        )
        log.mark_sent(2026, 16, LAST_DIGEST - timedelta(days=14), 2)
        # Partly sent: some of it may be in the chat, so it counts.
        log.claim(2026, 17, LAST_DIGEST)
        log.mark_partial(2026, 17, LAST_DIGEST, 1)
        # The message after qualifying and the summary are not digests.
        log.claim_qualifying(2026, 17, AFTER)
        log.claim_summary("2026-10", AFTER)

        assert log.last_digest_at(NOW) == LAST_DIGEST
        assert log.last_digest_at(LAST_DIGEST) == LAST_DIGEST - timedelta(days=14)


# --- in the facts document --------------------------------------------------------------


def collect(stories: Any) -> dict[str, Any]:
    with facts_transport.client([]) as client:
        return facts.collect(NOW, DEFAULT_FAVOURITES, client, sleep=lambda _: None, stories=stories)


def test_an_unreadable_state_file_only_removes_the_section() -> None:
    def broken() -> dict[str, Any]:
        raise StateError("cannot open state.db: disk I/O error")

    document = collect(broken)

    assert document["sections"]["stories"] is None
    assert document["missing"] == [
        {"section": "stories", "reason": "cannot open state.db: disk I/O error"}
    ]
    assert document["sections"]["race"] is not None


def test_without_a_reader_the_section_is_missing() -> None:
    document = collect(None)

    assert document["sections"]["stories"] is None
    assert document["missing"] == [
        {"section": "stories", "reason": "the followed stories were not read"}
    ]


# --- from the command line ----------------------------------------------------------------


def test_cli_reads_the_stories_since_the_last_digest(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: Path,
    stories: dict[str, Story],
) -> None:
    with SendLog(state) as log:
        log.claim(2026, 17, LAST_DIGEST)
        log.mark_sent(2026, 17, LAST_DIGEST, 2)

    def fake_collect(
        now: datetime, favourites: object, stories: Any = None, tz: Any = None
    ) -> dict[str, Any]:
        return {"sections": {"race": {"round": 18}, "stories": stories()}, "missing": []}

    monkeypatch.setattr(cli, "_now", lambda: NOW)
    monkeypatch.setattr(facts, "collect", fake_collect)

    assert main(["digest", "--prompt-only", "--state", str(state)]) == 0
    prompt = capsys.readouterr().out
    document = json.loads(prompt.split(FACTS_BLOCK, 1)[1].split("\n</facts>", 1)[0])
    data = document["sections"]["stories"]

    assert data["since"] == "2026-10-01T10:00:00+02:00"
    assert data["since_last_digest"] is True
    seat = by_id(data["followed"])[stories["seat"].id]
    assert [u["link"] for u in seat["updates_since"]] == [
        "https://example.com/story-11",
        "https://example.com/story-12",
    ]

    assert main(["facts", "--state", str(state)]) == 0
    assert json.loads(capsys.readouterr().out)["sections"]["stories"] == data


def test_cli_without_a_sent_digest_looks_back_a_fixed_window(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: Path,
    stories: dict[str, Story],
) -> None:
    def fake_collect(
        now: datetime, favourites: object, stories: Any = None, tz: Any = None
    ) -> dict[str, Any]:
        return {"sections": {"race": {"round": 18}, "stories": stories()}, "missing": []}

    monkeypatch.setattr(cli, "_now", lambda: NOW)
    monkeypatch.setattr(facts, "collect", fake_collect)

    assert main(["facts", "--state", str(state)]) == 0
    data = json.loads(capsys.readouterr().out)["sections"]["stories"]

    assert data["since"] == "2026-10-07T10:00:00+02:00"
    assert data["since_last_digest"] is False
    seat = by_id(data["followed"])[stories["seat"].id]
    assert seat["updates_since"] == []

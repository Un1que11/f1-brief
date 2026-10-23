"""Seed stories (F27): the checked-in seeds file, and `f1brief seed-stories` on synthetic seeds
(example.com, no real outlet or text) with a stubbed `claude -p`."""

import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from test_digest import facts_of
from test_story_detection import Model, attach, new

from f1brief import cli, digest, news, story_seeds
from f1brief import story_detection as sd
from f1brief.cli import main
from f1brief.config import DEFAULT_FAVOURITES
from f1brief.stories import Entity, EntityKind, StoryError, StoryStatus, StoryStore, Update
from f1brief.story_seeds import Seed, SeedError

NOW = datetime(2026, 10, 19, 9, tzinfo=UTC)
REPO_SEEDS = Path(__file__).resolve().parent.parent / "story_seeds.toml"

FIRST = "https://example.com/bearman-ferrari"
SECOND = "https://example.com/hamilton-revenue"
SEEDS_TOML = f"""
[[seed]]
url = "{FIRST}"
title = "Synthetic: Bearman linked to a Ferrari seat for 2028"
source = "Example News"
published = 2026-09-01T08:56:00+02:00
summary = "Synthetic: Haas driver Bearman may replace Hamilton."

[[seed]]
url = "{SECOND}"
title = "Synthetic: Ferrari would lose revenue without Hamilton"
source = "Example Daily"
published = 2026-08-16T12:00:00Z
"""

BEARMAN_FERRARI = new(
    FIRST,
    slug="bearman-ferrari-2028",
    title="Бирман в Ferrari в 2028 вместо Хэмилтона?",
    entities=[
        {"kind": "driver", "ref": "bearman"},
        {"kind": "team", "ref": "haas"},
        {"kind": "team", "ref": "ferrari"},
        {"kind": "driver", "ref": "hamilton"},
    ],
)


# --- the seeds file -----------------------------------------------------------------------


def test_the_checked_in_seeds_are_the_sources_from_the_backlog() -> None:
    seeds = story_seeds.load(REPO_SEEDS)
    assert [s.url for s in seeds] == [
        # The backlog's link redirects here (checked 2026-10-19).
        "https://www.gpfans.com/en/f1-news/1089724/"
        "f1-lewis-hamilton-new-contract-ferrari-force-ollie-bearman-team-change/",
        "https://www.f1oversteer.com/news/"
        "ferrari-will-have-a-77-million-problem-if-they-replace-lewis-hamilton-with-oliver-bearman/",
    ]
    assert all(s.follow and s.title and s.source and s.summary for s in seeds)
    assert [s.published for s in seeds] == [
        datetime(2026, 9, 1, 6, 56, tzinfo=UTC),
        datetime(2026, 8, 16, 12, tzinfo=UTC),
    ]


def test_the_checked_in_seeds_say_nothing_about_the_story() -> None:
    """The story comes from the model: the file carries only what a feed would."""
    text = REPO_SEEDS.read_text(encoding="utf-8")
    for key in ("slug", "status", "entities", "what_changed"):
        assert f"{key} =" not in text


def test_a_seed_parses_with_its_defaults() -> None:
    seeds = story_seeds.parse(
        {
            "seed": [
                {"url": FIRST, "title": " T ", "source": "S"},
                {
                    "url": SECOND,
                    "title": "U",
                    "source": "S",
                    "follow": False,
                    "summary": " x ",
                    "published": datetime(2026, 8, 16, 14, tzinfo=timezone(timedelta(hours=2))),
                },
            ]
        }
    )
    assert seeds == (
        Seed(FIRST, "T", "S", None),
        Seed(SECOND, "U", "S", datetime(2026, 8, 16, 12, tzinfo=UTC), "x", follow=False),
    )


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"seeds": []}, "unknown key(s) seeds"),
        ({"seed": {}}, "must be a list"),
        ({"seed": [{"url": FIRST, "title": "T"}]}, "seed[1].source: required"),
        ({"seed": [{"url": "example.com/x", "title": "T", "source": "S"}]}, "not a web link"),
        ({"seed": [{"url": FIRST, "title": "T", "source": "S", "status": "rumor"}]}, "status"),
        ({"seed": [{"url": FIRST, "title": "T", "source": "S"}] * 2}, "listed twice"),
        (
            {"seed": [{"url": FIRST, "title": "T", "source": "S", "published": "2026-09-01"}]},
            "with an offset",
        ),
        (
            {
                "seed": [
                    {"url": FIRST, "title": "T", "source": "S", "published": datetime(2026, 9, 1)}
                ]
            },
            "with an offset",
        ),
        ({"seed": [{"url": FIRST, "title": "T", "source": "S", "follow": "yes"}]}, "true or"),
    ],
)
def test_an_invalid_seed_is_an_error(data: dict[str, Any], error: str) -> None:
    with pytest.raises(SeedError, match=re.escape(error)):
        story_seeds.parse(data)


def test_a_missing_or_malformed_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SeedError, match="no such file"):
        story_seeds.load(tmp_path / "absent.toml")
    (tmp_path / "bad.toml").write_text("[[seed]\n", encoding="utf-8")
    with pytest.raises(SeedError, match="not valid TOML"):
        story_seeds.load(tmp_path / "bad.toml")


def test_seeds_become_news_items_classified_like_a_feed_s() -> None:
    seeds = story_seeds.parse(
        {
            "seed": [
                {
                    "url": FIRST,
                    "title": "Synthetic: Bearman contract talks",
                    "source": "Example News",
                    "summary": "s",
                }
            ]
        }
    )
    [item] = story_seeds.items(seeds, DEFAULT_FAVOURITES, NOW)
    assert (item.link, item.title, item.summary, item.sources) == (
        FIRST,
        "Synthetic: Bearman contract talks",
        "s",
        ("Example News",),
    )
    assert item.published is None
    assert item.favourites == {"Oliver Bearman"}
    assert "contracts" in item.topics


# --- the store ----------------------------------------------------------------------------


def test_without_updates_lists_the_urls_no_story_has(tmp_path: Path) -> None:
    with StoryStore(tmp_path / "state.db") as store:
        assert store.without_updates([]) == []
        store.create(
            "s",
            "Сюжет",
            "Синтетика.",
            StoryStatus.RUMOR,
            [],
            Update("H", FIRST, "Example News", "Синтетика.", None, "ru"),
            NOW,
            language="ru",
        )
        assert store.without_updates([SECOND, FIRST]) == [SECOND]


# --- the command --------------------------------------------------------------------------


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """`f1brief seed-stories` with synthetic seeds and a stubbed `claude -p`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    (tmp_path / "story_seeds.toml").write_text(SEEDS_TOML, encoding="utf-8")

    def no_feeds(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("seeding reads no news feed")

    monkeypatch.setattr(news, "fetch_news", no_feeds)
    model = Model(BEARMAN_FERRARI, attach(SECOND))

    class FakeCli:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["system_prompt"] == sd.DETECTION_SYSTEM_PROMPT

        def __call__(self, prompt: str) -> str:
            return model(prompt)

    monkeypatch.setattr(digest, "ClaudeCli", FakeCli)

    def invoke(*args: str) -> int:
        return main(["seed-stories", *args])

    invoke.model = model  # type: ignore[attr-defined]
    return invoke


def test_a_seed_starts_a_story_through_detection_and_follows_it(run: Any, capsys: Any) -> None:
    assert run() == 0

    out = capsys.readouterr().out
    assert "1 new stories, 1 updates" in out
    assert "following #1 bearman-ferrari-2028" in out
    # The detection prompt, with the seeds as its items.
    [prompt] = run.model.prompts
    assert "Decide, for every item" in prompt
    sent = facts_of(prompt)
    assert sent["stories"] == []
    assert [(i["url"], i["sources"]) for i in sent["items"]] == [
        (FIRST, ["Example News"]),
        (SECOND, ["Example Daily"]),
    ]
    with StoryStore("state.db") as store:
        story = store.get_by_slug("bearman-ferrari-2028")
        assert story is not None
        assert story.status is StoryStatus.RUMOR
        assert set(store.entities(story.id)) == {
            Entity(EntityKind.DRIVER, "bearman"),
            Entity(EntityKind.TEAM, "haas"),
            Entity(EntityKind.TEAM, "ferrari"),
            Entity(EntityKind.DRIVER, "hamilton"),
        }
        # Every update keeps its source; the timeline is in publication order.
        assert [(u.update.url, u.update.source) for u in store.timeline(story.id)] == [
            (SECOND, "Example Daily"),
            (FIRST, "Example News"),
        ]
        assert [s.slug for s in store.followed()] == ["bearman-ferrari-2028"]
        assert store.subscription(story.id).followed_at == NOW
        assert store.unchecked([FIRST, SECOND]) == []


def test_a_second_run_asks_nothing(run: Any, capsys: Any) -> None:
    assert run() == 0
    capsys.readouterr()
    assert run() == 0
    assert "no seed article left" in capsys.readouterr().out
    assert len(run.model.prompts) == 1


def test_an_unfollowed_seeded_story_is_not_followed_again(run: Any, capsys: Any) -> None:
    assert run() == 0
    with StoryStore("state.db") as store:
        store.unfollow(store.get_by_slug("bearman-ferrari-2028").id, NOW)
    assert run() == 0
    with StoryStore("state.db") as store:
        assert store.followed() == []


def test_a_seed_the_model_ignored_is_asked_about_again(run: Any, capsys: Any) -> None:
    run.model.text = Model(BEARMAN_FERRARI, {"url": SECOND, "action": "ignore"}).text
    assert run() == 0
    assert f"no story from {SECOND}" in capsys.readouterr().out

    run.model.text = Model(attach(SECOND)).text
    assert run() == 0

    sent = facts_of(run.model.prompts[-1])
    assert [i["url"] for i in sent["items"]] == [SECOND]
    assert [s["slug"] for s in sent["stories"]] == ["bearman-ferrari-2028"]
    with StoryStore("state.db") as store:
        story = store.get_by_slug("bearman-ferrari-2028")
        assert [u.update.url for u in store.timeline(story.id)] == [SECOND, FIRST]
        assert [s.slug for s in store.followed()] == ["bearman-ferrari-2028"]


def test_a_seed_that_joins_a_stored_story_follows_it(run: Any, capsys: Any) -> None:
    with StoryStore("state.db") as store:
        store.create(
            "bearman-ferrari-2028",
            "Бирман в Ferrari?",
            "Синтетика.",
            StoryStatus.RUMOR,
            [Entity(EntityKind.DRIVER, "bearman")],
            Update("H", "https://example.com/earlier", "Example News", "Синтетика.", None, "ru"),
            NOW - timedelta(days=30),
            language="ru",
        )
    run.model.text = Model(attach(FIRST), attach(SECOND)).text

    assert run() == 0

    with StoryStore("state.db") as store:
        assert [s.slug for s in store.followed()] == ["bearman-ferrari-2028"]
        assert len(store.stories()) == 1


def test_a_seed_with_follow_false_starts_the_story_unfollowed(
    run: Any, tmp_path: Path, capsys: Any
) -> None:
    (tmp_path / "story_seeds.toml").write_text(
        SEEDS_TOML.replace('summary = "Synthetic', 'follow = false\nsummary = "Synthetic'),
        encoding="utf-8",
    )
    run.model.text = Model(BEARMAN_FERRARI, {"url": SECOND, "action": "ignore"}).text

    assert run() == 0

    assert "following" not in capsys.readouterr().out
    with StoryStore("state.db") as store:
        assert store.get_by_slug("bearman-ferrari-2028") is not None
        assert store.followed() == []


def test_the_model_s_story_gets_the_checks_of_any_detection(run: Any, capsys: Any) -> None:
    """A story may not start resolved: the seed is rejected and nothing is followed."""
    run.model.text = Model(
        BEARMAN_FERRARI | {"status": "resolved"}, {"url": SECOND, "action": "ignore"}
    ).text

    assert run() == 0

    out = capsys.readouterr().out
    assert f"rejected {FIRST}: a new story cannot start as resolved" in out
    assert "following" not in out
    with StoryStore("state.db") as store:
        assert store.stories() == []
        assert store.without_updates([FIRST]) == [FIRST]


def test_a_dry_run_stores_and_follows_nothing(run: Any, capsys: Any) -> None:
    assert run("--dry-run") == 0

    out = capsys.readouterr().out
    assert "dry run: nothing stored" in out
    assert "would follow bearman-ferrari-2028" in out
    with StoryStore("state.db") as store:
        assert store.stories() == []
        assert store.unchecked([FIRST, SECOND]) == [FIRST, SECOND]


def test_prompt_only_prints_the_prompt_without_asking(run: Any, capsys: Any) -> None:
    assert run("--prompt-only") == 0
    sent = facts_of(capsys.readouterr().out)
    assert [i["url"] for i in sent["items"]] == [FIRST, SECOND]
    assert run.model.prompts == []


def test_a_failed_follow_stores_nothing(
    run: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    def fail(self: StoryStore, story_id: int, now: datetime) -> bool:
        raise StoryError("synthetic failure")

    monkeypatch.setattr(StoryStore, "follow", fail)

    assert run() == 1

    assert "synthetic failure" in capsys.readouterr().err
    with StoryStore("state.db") as store:
        assert store.stories() == []
        assert store.unchecked([FIRST, SECOND]) == [FIRST, SECOND]


def test_a_missing_seeds_file_is_an_error(run: Any, capsys: Any) -> None:
    assert run("--seeds", "absent.toml") == 2
    assert "error: seeds: absent.toml: no such file" in capsys.readouterr().err
    assert run.model.prompts == []

"""Story detection (F22) on synthetic news items (example.com, no real outlet or text) with a
stubbed `claude -p`."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FIXTURES
from test_digest import facts_of

from f1brief import cli, digest, news
from f1brief import story_detection as sd
from f1brief.cli import main
from f1brief.news import NewsItem, NewsReport
from f1brief.send_log import StateError
from f1brief.stories import (
    _MIGRATIONS,
    SCHEMA_VERSION,
    Entity,
    EntityKind,
    StoryStatus,
    StoryStore,
    Update,
)

NOW = datetime(2026, 10, 16, 9, tzinfo=UTC)
# The real `fetch_news`, kept before the command fixture stubs it.
REAL_FETCH_NEWS = news.fetch_news
WATCH = ["McLaren", "Russell", "Ocon"]


def item(
    slug: str,
    title: str,
    topics: frozenset[str] = frozenset({"contracts"}),
    summary: str = "",
    published: datetime | None = datetime(2026, 10, 15, 12, tzinfo=UTC),
) -> NewsItem:
    return NewsItem(
        title=title,
        link=f"https://example.com/{slug}",
        published=published,
        summary=summary,
        sources=("Example News", "Example Daily"),
        topics=topics,
    )


OCON_SEAT = item("ocon-seat", "Synthetic: Ocon linked to a 2027 seat elsewhere")
OCON_TALKS = item("ocon-talks", "Synthetic: Ocon confirms talks about 2027")
RUSSELL_PENALTY = item(
    "russell-penalty", "Synthetic: Russell handed a grid drop", frozenset({"penalties"})
)
MCLAREN_UPGRADE = item(
    "mclaren-upgrade", "Synthetic: McLaren bring a new floor", frozenset({"upgrades"})
)
OTHER_CONTRACT = item("other-contract", "Synthetic: an unrelated driver signs a new deal")
MCLAREN_BOSS = item(
    "mclaren-boss", "Synthetic: McLaren name a new team principal", frozenset(), "Synthetic."
)

FIRST = Update(
    headline="Synthetic: Bearman linked to a 2028 Ferrari seat",
    url="https://example.com/bearman-ferrari",
    source="Example News",
    what_changed_ru="Синтетика: первый слух.",
    published_at=datetime(2026, 9, 30, 18, tzinfo=UTC),
)


@pytest.fixture
def store(tmp_path: Path):
    with StoryStore(tmp_path / "state.db") as opened:
        yield opened


def seed(store: StoryStore, slug: str = "bearman-ferrari-2028", **changes: Any):
    args = {
        "slug": slug,
        "title_ru": "Бирман в Ferrari в 2028?",
        "summary_ru": "Синтетическое описание.",
        "status": StoryStatus.RUMOR,
        "entities": [Entity(EntityKind.DRIVER, "bearman"), Entity(EntityKind.TEAM, "haas")],
        "first_update": FIRST,
        "now": NOW,
    } | changes
    return store.create(**args)


def new(url: str, slug: str = "ocon-2027", **changes: Any) -> dict[str, Any]:
    return {
        "url": url,
        "action": "new",
        "slug": slug,
        "title_ru": "Окон уйдёт после 2026?",
        "summary_ru": "Синтетика: по слухам, место Окона под вопросом.",
        "status": "rumor",
        "entities": [{"kind": "driver", "ref": "ocon"}, {"kind": "team", "ref": "haas"}],
        "what_changed_ru": "Синтетика: первый слух.",
    } | changes


def attach(url: str, story: str = "bearman-ferrari-2028", **changes: Any) -> dict[str, Any]:
    return {
        "url": url,
        "action": "attach",
        "story": story,
        "what_changed_ru": "Синтетика: новые подробности.",
    } | changes


class Model:
    """A stubbed `claude -p`: records the prompts, answers with the given decisions."""

    def __init__(self, *decisions: Any, text: str | None = None) -> None:
        self.text = text if text is not None else json.dumps({"decisions": list(decisions)})
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.text


# --- hot items ----------------------------------------------------------------------------


def test_hot_items_are_about_a_watched_name_on_a_hot_topic() -> None:
    items = [OCON_SEAT, RUSSELL_PENALTY, MCLAREN_UPGRADE, OTHER_CONTRACT, MCLAREN_BOSS]
    assert sd.hot_items(items, WATCH) == [OCON_SEAT, RUSSELL_PENALTY, MCLAREN_BOSS]


def test_a_name_matches_as_a_whole_word_only() -> None:
    oconnor = item("oconnor", "Synthetic: O'Connor's Oconville contract")
    assert sd.hot_items([oconnor], ["Ocon"]) == []


def test_nothing_is_hot_without_names_to_watch() -> None:
    assert sd.hot_items([OCON_SEAT], []) == []
    assert sd.hot_items([OCON_SEAT], ["  "]) == []


def test_entity_terms_name_teams_whole_and_people_by_the_last_word() -> None:
    entities = [
        Entity(EntityKind.DRIVER, "max_verstappen"),
        Entity(EntityKind.TEAM, "red_bull"),
        Entity(EntityKind.PERSON, "fred_vasseur"),
        Entity(EntityKind.DRIVER, "bearman"),
        Entity(EntityKind.DRIVER, "bearman"),
    ]
    assert sd.entity_terms(entities) == ["verstappen", "red bull", "vasseur", "bearman"]


def test_followed_stories_add_their_entities_to_the_watch(store: StoryStore) -> None:
    followed = seed(store)
    seed(store, "other", entities=[Entity(EntityKind.DRIVER, "hamilton")])
    store.follow(followed.id, NOW)
    assert sd.watch_terms(store, WATCH) == [*WATCH, "bearman", "haas"]
    bearman = item("bearman", "Synthetic: Bearman to sign for 2028")
    hamilton = item("hamilton", "Synthetic: Hamilton to leave")
    assert sd.pending(store, [bearman, hamilton], sd.watch_terms(store, WATCH)) == [bearman]


def test_pending_leaves_out_items_already_checked(store: StoryStore) -> None:
    store.mark_checked([OCON_SEAT.link], NOW)
    assert sd.pending(store, [OCON_SEAT, RUSSELL_PENALTY, OCON_SEAT], WATCH) == [RUSSELL_PENALTY]


# --- the prompt ---------------------------------------------------------------------------


def test_the_prompt_carries_only_the_new_items_and_the_open_stories(store: StoryStore) -> None:
    open_story = seed(store)
    seed(store, "closed", status=StoryStatus.RESOLVED)
    sent = facts_of(sd.build_prompt(store, [OCON_SEAT]))
    assert [s["slug"] for s in sent["stories"]] == [open_story.slug]
    assert sent["stories"][0]["entities"] == [
        {"kind": "driver", "ref": "bearman"},
        {"kind": "team", "ref": "haas"},
    ]
    assert sent["stories"][0]["recent_updates"] == [
        {
            "headline": FIRST.headline,
            "published_at": "2026-09-30T18:00:00+00:00",
            "what_changed_ru": FIRST.what_changed_ru,
        }
    ]
    assert sent["items"] == [
        {
            "url": OCON_SEAT.link,
            "title": OCON_SEAT.title,
            "summary": "",
            "published": "2026-10-15T12:00:00+00:00",
            "sources": ["Example News", "Example Daily"],
            "topics": ["contracts"],
        }
    ]


def test_the_template_has_no_facts_of_its_own() -> None:
    text = digest.template(sd.DETECTION_TEMPLATE, None)
    assert text.count(digest.FACTS_MARKER) == 1
    for name in ("Ocon", "Russell", "Bearman", "McLaren", "Haas", "Ferrari", "example.com"):
        assert name not in text


# --- reading the answer -------------------------------------------------------------------


def test_an_answer_in_a_code_fence_is_read() -> None:
    text = '```json\n{"decisions": [{"url": "u", "action": "ignore"}]}\n```'
    assert sd.parse_answer(text) == [{"url": "u", "action": "ignore"}]


@pytest.mark.parametrize("text", ["", "Sorry, I cannot.", "[]", '{"decision": []}', "{"])
def test_an_answer_that_is_not_the_json_asked_for_is_refused(text: str) -> None:
    with pytest.raises(sd.DetectionError):
        sd.parse_answer(text)


# --- checking the decisions ---------------------------------------------------------------


def test_new_attach_and_ignore_decisions_are_planned(store: StoryStore) -> None:
    seed(store)
    items = [OCON_SEAT, RUSSELL_PENALTY, MCLAREN_BOSS]
    result = sd.plan(
        [
            new(OCON_SEAT.link),
            attach(MCLAREN_BOSS.link),
            {"url": RUSSELL_PENALTY.link, "action": "ignore"},
        ],
        items,
        store.stories(),
    )
    assert result.rejected == ()
    [story] = result.new_stories
    assert story.slug == "ocon-2027"
    assert story.status == StoryStatus.RUMOR
    # The source comes from the item, not from the model.
    assert story.first_update == Update(
        headline=OCON_SEAT.title,
        url=OCON_SEAT.link,
        source="Example News",
        what_changed_ru="Синтетика: первый слух.",
        published_at=OCON_SEAT.published,
    )
    [attachment] = result.attachments
    assert (attachment.story_slug, attachment.update.url, attachment.status) == (
        "bearman-ferrari-2028",
        MCLAREN_BOSS.link,
        None,
    )
    assert result.ignored == (RUSSELL_PENALTY.link,)
    assert result.checked == tuple(i.link for i in items)
    assert result.unanswered == ()


@pytest.mark.parametrize(
    "decision",
    [
        {"action": "new"},
        {"url": "", "action": "ignore"},
        {"url": None, "action": "ignore"},
        "ignore everything",
    ],
)
def test_a_decision_without_a_source_url_is_rejected(store: StoryStore, decision: Any) -> None:
    result = sd.plan([decision], [OCON_SEAT], store.stories())
    [rejection] = result.rejected
    assert rejection.url is None
    assert result.new_stories == () and result.checked == ()
    assert result.unanswered == (OCON_SEAT.link,)


def test_a_url_that_was_not_sent_is_rejected(store: StoryStore) -> None:
    result = sd.plan([new("https://example.com/made-up")], [OCON_SEAT], store.stories())
    assert [r.reason for r in result.rejected] == ["the url is not one of the items sent"]
    assert result.new_stories == () and result.checked == ()


def test_an_item_whose_link_is_not_http_is_rejected(store: StoryStore) -> None:
    odd = NewsItem(
        "Synthetic: Ocon contract",
        "ftp://example.com/x",
        None,
        "",
        ("Example",),
        topics=frozenset({"contracts"}),
    )
    result = sd.plan([new(odd.link)], [odd], store.stories())
    assert "http(s) URL" in result.rejected[0].reason
    assert result.checked == (odd.link,)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"slug": "Bad Slug"}, "slug"),
        ({"slug": "bearman-ferrari-2028"}, "taken"),
        ({"title_ru": " "}, "title_ru"),
        ({"what_changed_ru": None}, "what_changed_ru"),
        ({"status": "gossip"}, "status"),
        ({"status": None}, "status"),
        ({"entities": []}, "entity"),
        ({"entities": [{"kind": "car", "ref": "x"}]}, "kind"),
        ({"entities": [{"kind": "driver", "ref": ""}]}, "ref"),
        ({"action": "merge"}, "unknown action"),
    ],
)
def test_an_invalid_new_story_is_rejected(
    store: StoryStore, changes: dict[str, Any], reason: str
) -> None:
    seed(store)
    result = sd.plan([new(OCON_SEAT.link, **changes)], [OCON_SEAT], store.stories())
    [rejection] = result.rejected
    assert reason in rejection.reason
    assert result.new_stories == ()
    # Answered, so it is not asked about again.
    assert result.checked == (OCON_SEAT.link,)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"story": "no-such-story"}, "no open story"),
        ({"story": "closed"}, "no open story"),
        ({"status": "gossip"}, "status"),
        ({"what_changed_ru": ""}, "what_changed_ru"),
    ],
)
def test_an_invalid_attachment_is_rejected(
    store: StoryStore, changes: dict[str, Any], reason: str
) -> None:
    seed(store)
    seed(store, "closed", status=StoryStatus.RESOLVED)
    result = sd.plan([attach(OCON_SEAT.link, **changes)], [OCON_SEAT], store.stories())
    [rejection] = result.rejected
    assert reason in rejection.reason
    assert result.attachments == ()


def test_an_item_may_join_a_story_started_in_the_same_answer(store: StoryStore) -> None:
    decisions = [attach(OCON_TALKS.link, "ocon-2027", status="developing"), new(OCON_SEAT.link)]
    result = sd.plan(decisions, [OCON_SEAT, OCON_TALKS], store.stories())
    assert result.rejected == ()
    assert [a.story_slug for a in result.attachments] == ["ocon-2027"]


def test_two_new_stories_may_not_share_a_slug(store: StoryStore) -> None:
    result = sd.plan([new(OCON_SEAT.link), new(OCON_TALKS.link)], [OCON_SEAT, OCON_TALKS], [])
    assert len(result.new_stories) == 1
    assert "taken" in result.rejected[0].reason


def test_a_second_decision_for_an_item_is_rejected(store: StoryStore) -> None:
    decisions = [{"url": OCON_SEAT.link, "action": "ignore"}, new(OCON_SEAT.link)]
    result = sd.plan(decisions, [OCON_SEAT], store.stories())
    assert result.ignored == (OCON_SEAT.link,)
    assert result.new_stories == ()
    assert [r.reason for r in result.rejected] == ["a second decision for the same item"]


def test_items_left_out_of_the_answer_stay_unchecked(store: StoryStore) -> None:
    result = sd.plan([new(OCON_SEAT.link)], [OCON_SEAT, OCON_TALKS], store.stories())
    assert result.checked == (OCON_SEAT.link,)
    assert result.unanswered == (OCON_TALKS.link,)


# --- storing ------------------------------------------------------------------------------


def test_detect_then_apply_stores_stories_updates_and_checked_items(store: StoryStore) -> None:
    seed(store)
    model = Model(
        new(OCON_SEAT.link),
        attach(OCON_TALKS.link, "ocon-2027", status="developing"),
        attach(MCLAREN_BOSS.link),
        {"url": RUSSELL_PENALTY.link, "action": "ignore"},
    )
    items = sd.pending(store, [OCON_SEAT, OCON_TALKS, MCLAREN_BOSS, RUSSELL_PENALTY], WATCH)
    result = sd.detect(store, items, model)
    assert store.get_by_slug("ocon-2027") is None  # detect alone stores nothing
    sd.apply(store, result, NOW)

    ocon = store.get_by_slug("ocon-2027")
    assert ocon is not None and ocon.status == StoryStatus.DEVELOPING
    assert store.entities(ocon.id) == [
        Entity(EntityKind.DRIVER, "ocon"),
        Entity(EntityKind.TEAM, "haas"),
    ]
    assert [u.update.url for u in store.timeline(ocon.id)] == [OCON_SEAT.link, OCON_TALKS.link]
    bearman = store.get_by_slug("bearman-ferrari-2028")
    assert bearman is not None
    assert [u.update.url for u in store.timeline(bearman.id)] == [FIRST.url, MCLAREN_BOSS.link]
    # A new story is not followed until Misha follows it.
    assert store.subscription(ocon.id) is None

    # The next run sends nothing: every item was answered.
    assert sd.pending(store, items, WATCH) == []


def test_every_stored_update_has_a_source_url(store: StoryStore) -> None:
    model = Model(
        new(OCON_SEAT.link), {"action": "attach", "story": "ocon-2027", "what_changed_ru": "x"}
    )
    sd.apply(store, sd.detect(store, [OCON_SEAT], model), NOW)
    rows = store._db.execute("SELECT url FROM story_updates").fetchall()
    assert rows == [(OCON_SEAT.link,)]


def test_a_failure_while_storing_stores_nothing(
    store: StoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(store)
    result = sd.plan(
        [new(OCON_SEAT.link), attach(OCON_TALKS.link)], [OCON_SEAT, OCON_TALKS], store.stories()
    )

    def fail(*args: object) -> bool:
        raise StateError("disk full")

    monkeypatch.setattr(store, "add_update", fail)
    with pytest.raises(StateError, match="disk full"):
        sd.apply(store, result, NOW)
    assert store.get_by_slug("ocon-2027") is None
    assert store.unchecked([OCON_SEAT.link, OCON_TALKS.link]) == [OCON_SEAT.link, OCON_TALKS.link]
    # The store is usable afterwards.
    sd.apply(store, sd.plan([new(OCON_SEAT.link)], [OCON_SEAT], store.stories()), NOW)
    assert store.get_by_slug("ocon-2027") is not None


def test_a_model_failure_checks_nothing(store: StoryStore) -> None:
    def broken(prompt: str) -> str:
        raise digest.GenerationError("claude exited with 1")

    with pytest.raises(digest.GenerationError):
        sd.detect(store, [OCON_SEAT], broken)
    assert store.unchecked([OCON_SEAT.link]) == [OCON_SEAT.link]


def test_a_state_file_from_f21_gains_the_checked_news_table(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE schema_versions (component TEXT NOT NULL PRIMARY KEY, "
            "version INTEGER NOT NULL)"
        )
        for statement in _MIGRATIONS[0]:
            db.execute(statement)
        db.execute("INSERT INTO schema_versions VALUES ('stories', 1)")
        db.execute(
            "INSERT INTO stories (slug, title_ru, summary_ru, status, created_at, "
            "last_update_at) VALUES ('kept', 't', 's', 'rumor', '2026-10-01', '2026-10-01')"
        )
    with StoryStore(path) as store:
        assert [s.slug for s in store.stories()] == ["kept"]
        store.mark_checked([OCON_SEAT.link], NOW)
        assert store.unchecked([OCON_SEAT.link, OCON_TALKS.link]) == [OCON_TALKS.link]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_versions").fetchall() == [(SCHEMA_VERSION,)]
    assert SCHEMA_VERSION >= 2


# --- the command --------------------------------------------------------------------------


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """`f1brief detect-stories` with stubbed news and a stubbed `claude -p`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    # Only Ocon is a favourite: the McLaren upgrade is not about anyone watched.
    (tmp_path / "config.toml").write_text(
        '[favourites]\nteams = []\n[[favourites.drivers]]\nid = "ocon"\nname = "Esteban Ocon"\n'
        'aliases = ["Ocon"]\n',
        encoding="utf-8",
    )
    feed = [OCON_SEAT, MCLAREN_UPGRADE]
    monkeypatch.setattr(
        news, "fetch_news", lambda since, *a, **k: NewsReport(since, tuple(feed), ())
    )
    model = Model(new(OCON_SEAT.link))

    class FakeCli:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["system_prompt"] == sd.DETECTION_SYSTEM_PROMPT

        def __call__(self, prompt: str) -> str:
            return model(prompt)

    monkeypatch.setattr(digest, "ClaudeCli", FakeCli)

    def invoke(*args: str) -> int:
        return main(["detect-stories", *args])

    invoke.model = model  # type: ignore[attr-defined]
    return invoke


def test_the_command_stores_what_the_model_decided(run: Any, capsys: Any) -> None:
    assert run() == 0
    assert "1 new stories" in capsys.readouterr().out
    assert len(run.model.prompts) == 1
    with StoryStore("state.db") as store:
        assert store.get_by_slug("ocon-2027") is not None
    # Nothing new on the next run: claude is not asked again.
    assert run() == 0
    assert "no new hot news items" in capsys.readouterr().out
    assert len(run.model.prompts) == 1


def test_a_dry_run_stores_nothing(run: Any, capsys: Any) -> None:
    assert run("--dry-run") == 0
    assert "dry run: nothing stored" in capsys.readouterr().out
    with StoryStore("state.db") as store:
        assert store.stories() == []
        assert store.unchecked([OCON_SEAT.link]) == [OCON_SEAT.link]


def test_prompt_only_prints_the_prompt_without_asking(run: Any, capsys: Any) -> None:
    assert run("--prompt-only") == 0
    sent = facts_of(capsys.readouterr().out)
    assert [i["url"] for i in sent["items"]] == [OCON_SEAT.link]
    assert run.model.prompts == []


def test_an_unreadable_answer_is_an_error_and_stores_nothing(run: Any, capsys: Any) -> None:
    run.model.text = "not json"
    assert run() == 1
    assert "not JSON" in capsys.readouterr().err
    with StoryStore("state.db") as store:
        assert store.unchecked([OCON_SEAT.link]) == [OCON_SEAT.link]


def test_the_command_sends_hot_items_about_a_followed_non_favourite_entity(
    run: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A followed story about Ferrari: an item naming Ferrari and a team principal is hot
    (`EXTRA_TERMS`) although it names no favourite and matches no news topic, so the news
    filter of the digest must not drop it before detection sees it."""
    feed = (FIXTURES / "rss" / "followed_entity_like.xml").read_bytes()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=feed))
    monkeypatch.setattr(
        news,
        "fetch_news",
        lambda since, favourites, **kwargs: REAL_FETCH_NEWS(
            since,
            favourites,
            feeds=(news.Feed("Example News", "https://example.test/feed"),),
            client=httpx.Client(transport=transport),
            **kwargs,
        ),
    )
    with StoryStore("state.db") as store:
        story = seed(store, "ferrari-boss", entities=[Entity(EntityKind.TEAM, "ferrari")])
        store.follow(story.id, NOW)

    assert run("--prompt-only") == 0

    sent = facts_of(capsys.readouterr().out)
    assert [i["url"] for i in sent["items"]] == ["https://example.test/feed/ferrari-boss"]

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from f1brief.news_archive import NewsArchive
from f1brief.send_log import SendLog, StateError
from f1brief.stories import (
    SCHEMA_VERSION,
    Entity,
    EntityKind,
    StoryError,
    StoryStatus,
    StoryStore,
    Update,
)

NOW = datetime(2026, 10, 1, 9, tzinfo=UTC)
ENTITIES = [
    Entity(EntityKind.DRIVER, "bearman"),
    Entity(EntityKind.TEAM, "haas"),
    Entity(EntityKind.TEAM, "ferrari"),
    Entity(EntityKind.DRIVER, "hamilton"),
]
# Synthetic articles: example.com, no real outlet or text.
FIRST = Update(
    headline="Synthetic: Bearman linked to a 2028 Ferrari seat",
    url="https://example.com/bearman-ferrari",
    source="Example News",
    what_changed_ru="Синтетика: первый слух.",
    published_at=datetime(2026, 9, 30, 18, tzinfo=UTC),
)
SECOND = Update(
    headline="Synthetic: Ferrari comments on its 2028 line-up",
    url="https://example.com/ferrari-comments",
    source="Example Daily",
    what_changed_ru="Синтетика: команда прокомментировала.",
    published_at=datetime(2026, 10, 5, 12, tzinfo=UTC),
)


@pytest.fixture
def store(tmp_path: Path):
    with StoryStore(tmp_path / "state.db") as opened:
        yield opened


def _create(store: StoryStore, slug: str = "bearman-ferrari-2028", **changes):
    args = {
        "slug": slug,
        "title_ru": "Бирман в Ferrari в 2028?",
        "summary_ru": "Синтетическое описание.",
        "status": StoryStatus.RUMOR,
        "entities": ENTITIES,
        "first_update": FIRST,
        "now": NOW,
    } | changes
    return store.create(**args)


# --- stories and updates ------------------------------------------------------------------


def test_a_story_comes_back_with_its_entities_and_first_update(store: StoryStore) -> None:
    story = _create(store)
    assert story.slug == "bearman-ferrari-2028"
    assert story.status is StoryStatus.RUMOR
    assert story.created_at == NOW
    assert story.last_update_at == FIRST.published_at
    assert store.get(story.id) == story
    assert store.get_by_slug("bearman-ferrari-2028") == story
    assert set(store.entities(story.id)) == set(ENTITIES)
    [first] = store.timeline(story.id)
    assert first.update == FIRST
    assert first.story_id == story.id
    assert first.recorded_at == NOW


def test_a_taken_slug_is_refused_and_nothing_is_stored(store: StoryStore) -> None:
    _create(store)
    with pytest.raises(StoryError, match="slug"):
        _create(store, first_update=SECOND)
    assert len(store.stories()) == 1
    assert store.updates_since(NOW - timedelta(days=1)) == store.timeline(1)


@pytest.mark.parametrize(
    "url", ["", "example.com/no-scheme", "ftp://example.com/x", "https://", "javascript:x"]
)
def test_a_story_without_a_source_url_is_refused(store: StoryStore, url: str) -> None:
    with pytest.raises(ValueError, match="URL"):
        _create(store, first_update=replace(FIRST, url=url))
    assert store.stories() == []


@pytest.mark.parametrize("field", ["headline", "source", "what_changed_ru"])
def test_an_update_with_an_empty_field_is_refused(store: StoryStore, field: str) -> None:
    story = _create(store)
    with pytest.raises(ValueError, match=field):
        store.add_update(story.id, replace(SECOND, **{field: "  "}), NOW)
    assert len(store.timeline(story.id)) == 1


@pytest.mark.parametrize("slug", ["", "Bearman", "bearman ferrari", "-bearman", "a--b"])
def test_a_bad_slug_is_refused(store: StoryStore, slug: str) -> None:
    with pytest.raises(ValueError, match="slug"):
        _create(store, slug=slug)


def test_the_database_itself_refuses_an_update_without_a_url(store: StoryStore) -> None:
    story = _create(store)
    with pytest.raises(sqlite3.IntegrityError):
        store._db.execute(
            "INSERT INTO story_updates (story_id, headline, url, source, what_changed_ru, "
            "recorded_at) VALUES (?, 'h', '', 's', 'w', ?)",
            (story.id, NOW.isoformat()),
        )


def test_an_update_moves_last_update_and_may_change_the_status(store: StoryStore) -> None:
    story = _create(store)
    later = NOW + timedelta(days=5)
    assert store.add_update(story.id, SECOND, later, status=StoryStatus.DEVELOPING)
    changed = store.get(story.id)
    assert changed is not None
    assert changed.status is StoryStatus.DEVELOPING
    assert changed.last_update_at == SECOND.published_at
    assert [u.update for u in store.timeline(story.id)] == [FIRST, SECOND]


def test_an_update_without_a_status_keeps_the_status(store: StoryStore) -> None:
    story = _create(store)
    store.add_update(story.id, SECOND, NOW)
    assert store.get(story.id).status is StoryStatus.RUMOR


def test_an_older_article_does_not_move_last_update_back(store: StoryStore) -> None:
    story = _create(store)
    older = replace(SECOND, published_at=datetime(2026, 9, 1, tzinfo=UTC))
    store.add_update(story.id, older, NOW)
    assert store.get(story.id).last_update_at == FIRST.published_at
    assert [u.update for u in store.timeline(story.id)] == [older, FIRST]


def test_an_undated_article_counts_from_when_it_was_recorded(store: StoryStore) -> None:
    story = _create(store)
    recorded = NOW + timedelta(days=10)
    store.add_update(story.id, replace(SECOND, published_at=None), recorded)
    assert store.get(story.id).last_update_at == recorded
    assert store.timeline(story.id)[-1].update.published_at is None


def test_the_same_article_is_attached_once(store: StoryStore) -> None:
    story = _create(store)
    assert store.add_update(story.id, SECOND, NOW)
    again = replace(SECOND, headline="Changed", published_at=datetime(2026, 12, 1, tzinfo=UTC))
    assert not store.add_update(story.id, again, NOW, status=StoryStatus.CONFIRMED)
    unchanged = store.get(story.id)
    assert unchanged is not None
    assert unchanged.status is StoryStatus.RUMOR
    assert unchanged.last_update_at == SECOND.published_at
    assert len(store.timeline(story.id)) == 2


def test_an_update_for_an_unknown_story_is_refused(store: StoryStore) -> None:
    with pytest.raises(StoryError, match="no story 7"):
        store.add_update(7, SECOND, NOW)


def test_stories_are_listed_most_recently_updated_first(store: StoryStore) -> None:
    old = _create(store, slug="old-story")
    new = _create(store, slug="new-story", first_update=SECOND)
    assert [s.id for s in store.stories()] == [new.id, old.id]
    store.add_update(
        old.id,
        replace(SECOND, url="https://example.com/latest", published_at=NOW + timedelta(days=9)),
        NOW,
        status=StoryStatus.CONFIRMED,
    )
    assert [s.id for s in store.stories()] == [old.id, new.id]
    assert [s.id for s in store.stories([StoryStatus.RUMOR])] == [new.id]
    assert store.stories([StoryStatus.RESOLVED]) == []


def test_updates_since_a_moment_by_when_they_were_recorded(store: StoryStore) -> None:
    followed = _create(store, slug="followed")
    other = _create(store, slug="other")
    later = NOW + timedelta(days=3)
    store.add_update(followed.id, SECOND, later)
    store.add_update(other.id, SECOND, later)
    store.follow(followed.id, NOW)
    assert [(u.story_id, u.update) for u in store.updates_since(later)] == [
        (followed.id, SECOND),
        (other.id, SECOND),
    ]
    assert [u.story_id for u in store.updates_since(later, followed_only=True)] == [followed.id]
    assert len(store.updates_since(NOW)) == 4


def test_naive_timestamps_are_refused(store: StoryStore) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _create(store, now=datetime(2026, 10, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        _create(store, first_update=replace(FIRST, published_at=datetime(2026, 10, 1)))


# --- subscriptions ------------------------------------------------------------------------


def test_follow_and_unfollow_keep_the_story(store: StoryStore) -> None:
    story = _create(store)
    assert store.subscription(story.id) is None
    assert store.follow(story.id, NOW)
    assert not store.follow(story.id, NOW + timedelta(hours=1))
    assert store.followed() == [story]
    sub = store.subscription(story.id)
    assert sub is not None
    assert (sub.active, sub.followed_at, sub.unfollowed_at) == (True, NOW, None)

    later = NOW + timedelta(days=2)
    assert store.unfollow(story.id, later)
    assert not store.unfollow(story.id, later)
    assert store.followed() == []
    assert store.get(story.id) == story
    assert len(store.timeline(story.id)) == 1
    sub = store.subscription(story.id)
    assert sub is not None
    assert (sub.active, sub.unfollowed_at) == (False, later)


def test_following_again_starts_afresh(store: StoryStore) -> None:
    story = _create(store)
    store.follow(story.id, NOW)
    store.mute(story.id, NOW + timedelta(days=30))
    store.unfollow(story.id, NOW + timedelta(days=1))
    again = NOW + timedelta(days=2)
    assert store.follow(story.id, again)
    sub = store.subscription(story.id)
    assert sub is not None
    assert (sub.active, sub.followed_at, sub.unfollowed_at, sub.muted_until) == (
        True,
        again,
        None,
        None,
    )


def test_following_an_unknown_story_is_refused(store: StoryStore) -> None:
    with pytest.raises(StoryError, match="no story 3"):
        store.follow(3, NOW)
    assert not store.unfollow(3, NOW)
    assert not store.mute(3, NOW)


def test_mute_applies_to_a_followed_story_until_its_end(store: StoryStore) -> None:
    story = _create(store)
    until = NOW + timedelta(days=7)
    assert not store.mute(story.id, until)  # not followed
    store.follow(story.id, NOW)
    assert store.mute(story.id, until)
    sub = store.subscription(story.id)
    assert sub is not None
    assert sub.muted(NOW)
    assert not sub.muted(until)
    assert store.followed() == [story]  # muted, still followed
    store.unfollow(story.id, NOW)
    assert store.subscription(story.id).muted_until is None


# --- the state file -----------------------------------------------------------------------


def test_stories_survive_reopening(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with StoryStore(path) as store:
        story = _create(store)
        store.follow(story.id, NOW)
    with StoryStore(path) as store:
        assert store.followed() == [story]
        assert [u.update for u in store.timeline(story.id)] == [FIRST]


def test_shares_the_state_file_with_the_send_log_and_the_news_archive(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with SendLog(path) as log, NewsArchive(path), StoryStore(path) as store:
        assert log.claim(2026, 18, NOW)
        story = _create(store)
    with SendLog(path) as log, StoryStore(path) as store:
        assert log.get(2026, 18) is not None
        assert store.get(story.id) == story


def test_the_schema_version_is_recorded_once(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    StoryStore(path).close()
    StoryStore(path).close()
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT component, version FROM schema_versions").fetchall()
    assert rows == [("stories", SCHEMA_VERSION)]


def test_a_file_from_a_newer_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    StoryStore(path).close()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE schema_versions SET version = ?", (SCHEMA_VERSION + 1,))
    with pytest.raises(StateError, match="newer"):
        StoryStore(path)


def test_an_unreadable_file_is_a_state_error(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"not a database" * 100)
    with pytest.raises(StateError):
        StoryStore(path)


def test_deleting_a_story_removes_its_rows(store: StoryStore) -> None:
    story = _create(store)
    store.follow(story.id, NOW)
    store._db.execute("DELETE FROM stories WHERE id = ?", (story.id,))
    for table in ("story_entities", "story_updates", "subscriptions"):
        assert store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_a_story_that_fails_halfway_leaves_nothing(
    store: StoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object) -> bool:
        raise StateError("disk full")

    monkeypatch.setattr(store, "_insert_update", fail)
    with pytest.raises(StateError, match="disk full"):
        _create(store)
    assert store.stories() == []
    assert store._db.execute("SELECT COUNT(*) FROM story_entities").fetchone() == (0,)

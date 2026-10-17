import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from f1brief.news import NewsItem
from f1brief.news_archive import NewsArchive
from f1brief.send_log import SendLog, StateError

SEEN = datetime(2026, 1, 10, 8, tzinfo=UTC)
ITEM = NewsItem(
    title="Ocon signs a new contract",
    link="https://example.com/ocon-contract",
    published=datetime(2026, 1, 9, 18, tzinfo=UTC),
    summary="Synthetic.",
    sources=("BBC Sport", "Autosport"),
    favourites=frozenset({"Ocon"}),
    topics=frozenset({"contracts"}),
    future=True,
)


@pytest.fixture
def archive(tmp_path: Path):
    with NewsArchive(tmp_path / "state.db") as opened:
        yield opened


def test_items_come_back_as_stored(archive: NewsArchive) -> None:
    assert archive.add([ITEM], SEEN) == 1
    assert archive.since(datetime(2026, 1, 1, tzinfo=UTC)) == [ITEM]
    assert archive.since(datetime(2026, 1, 9, 19, tzinfo=UTC)) == []


def test_an_item_is_stored_once(archive: NewsArchive) -> None:
    archive.add([ITEM], SEEN)
    assert archive.add([ITEM, replace(ITEM, title="Changed")], SEEN) == 0
    assert archive.since(datetime(2026, 1, 1, tzinfo=UTC)) == [ITEM]


def test_an_undated_item_counts_from_when_it_was_first_seen(archive: NewsArchive) -> None:
    undated = replace(ITEM, link="https://example.com/undated", published=None)
    archive.add([undated], SEEN)
    assert archive.since(datetime(2026, 1, 10, 7, tzinfo=UTC)) == [undated]
    assert archive.since(datetime(2026, 1, 10, 9, tzinfo=UTC)) == []


def test_shares_the_state_file_with_the_send_log(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with SendLog(path) as log, NewsArchive(path) as archive:
        archive.add([ITEM], SEEN)
        assert log.claim(2026, 1, SEEN)
    with NewsArchive(path) as archive:
        assert archive.since(datetime(2026, 1, 1, tzinfo=UTC)) == [ITEM]


def test_naive_timestamps_are_rejected(archive: NewsArchive) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        archive.add([ITEM], datetime(2026, 1, 10))


def test_an_unopenable_file_is_a_state_error(tmp_path: Path) -> None:
    with pytest.raises(StateError, match="cannot open"):
        NewsArchive(tmp_path / "no-such-dir" / "state.db")


def test_flags_are_stored_as_a_list_of_names(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    plain = replace(ITEM, link="https://example.com/plain", future=False)
    with NewsArchive(path) as archive:
        archive.add([ITEM, plain], SEEN)
    with sqlite3.connect(path) as db:
        stored = dict(db.execute("SELECT link, flags FROM news_items"))
    assert {link: json.loads(flags) for link, flags in stored.items()} == {
        ITEM.link: ["future"],
        plain.link: [],
    }


def test_the_flag_name_from_before_f20_reads_as_future(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with NewsArchive(path) as archive:
        archive.add([ITEM], SEEN)
    with sqlite3.connect(path) as db:
        db.execute("""UPDATE news_items SET flags = '["ocon_future"]'""")
    db.close()
    with NewsArchive(path) as archive:
        assert archive.since(datetime(2026, 1, 1, tzinfo=UTC)) == [ITEM]


def test_unknown_flags_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with NewsArchive(path) as archive:
        archive.add([ITEM], SEEN)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE news_items SET flags = \'["future", "from_a_later_version"]\'')
    with NewsArchive(path) as archive:
        assert archive.since(datetime(2026, 1, 1, tzinfo=UTC)) == [ITEM]


def test_a_first_version_archive_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE news_items (link TEXT NOT NULL PRIMARY KEY, title TEXT NOT NULL, "
            "published TEXT, summary TEXT NOT NULL, sources TEXT NOT NULL, "
            "favourites TEXT NOT NULL, topics TEXT NOT NULL, ocon_future INTEGER NOT NULL, "
            "first_seen TEXT NOT NULL)"
        )
        db.executemany(
            "INSERT INTO news_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    link,
                    ITEM.title,
                    "2026-01-09T18:00:00+00:00",
                    ITEM.summary,
                    json.dumps(list(ITEM.sources)),
                    '["Ocon"]',
                    '["contracts"]',
                    flag,
                    SEEN.isoformat(),
                )
                for link, flag in ((ITEM.link, 1), ("https://example.com/plain", 0))
            ],
        )
    db.close()
    with NewsArchive(path) as archive:
        assert sorted(archive.since(datetime(2026, 1, 1, tzinfo=UTC)), key=lambda i: i.link) == [
            ITEM,
            replace(ITEM, link="https://example.com/plain", future=False),
        ]
        later = replace(ITEM, link="https://example.com/later")
        assert archive.add([later], SEEN) == 1
    with NewsArchive(path) as archive:
        assert later in archive.since(datetime(2026, 1, 1, tzinfo=UTC))

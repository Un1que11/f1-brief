# The page's texts are Russian: Cyrillic letters that look like Latin ones are intended here.
# ruff: noqa: RUF001
"""The static archive page (F28) on a synthetic story store: no real stories or news."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from f1brief import archive_page
from f1brief.cli import main
from f1brief.stories import Entity, EntityKind, StoryStatus, StoryStore, Update

NOW = datetime(2026, 10, 19, 9, tzinfo=UTC)


def update(n: int, published: datetime | None = datetime(2026, 9, 30, 18, tzinfo=UTC)) -> Update:
    return Update(
        headline=f"Synthetic headline {n}",
        url=f"https://example.com/story-{n}",
        source="Example News",
        what_changed_ru=f"Синтетика: изменение {n}.",
        published_at=published,
    )


@pytest.fixture
def store(tmp_path: Path):
    with StoryStore(tmp_path / "state.db") as opened:
        yield opened


def seed(
    store: StoryStore,
    slug: str,
    title: str,
    status: StoryStatus = StoryStatus.RUMOR,
    entities: list[Entity] | None = None,
    n: int = 1,
    published: datetime | None = datetime(2026, 9, 30, 18, tzinfo=UTC),
):
    return store.create(
        slug,
        title,
        "Синтетика: краткое описание.",
        status,
        entities or [],
        update(n, published),
        NOW - timedelta(days=10),
    )


def articles(page: str) -> dict[str, str]:
    """Each story's <article> markup by its element id."""
    return dict(re.findall(r'<article class="[^"]*" id="(story-\d+)">(.*?)</article>', page, re.S))


def article_classes(page: str) -> dict[str, str]:
    return {i: c for c, i in re.findall(r'<article class="([^"]*)" id="(story-\d+)">', page)}


def test_an_empty_store_shows_all_with_an_empty_note_for_each_filter(store: StoryStore) -> None:
    page = archive_page.build(store, NOW)
    assert '<input class="filters" type="radio" name="filter" id="f-all" checked>' in page
    assert '<label for="f-followed">Слежу (0)</label>' in page
    assert '<label for="f-all">Все (0)</label>' in page
    assert '<label for="f-resolved">Завершённые (0)</label>' in page
    for key in ("followed", "all", "resolved"):
        assert f'class="empty empty-{key}"' in page
    assert "<article" not in page


def test_counts_classes_and_the_default_filter(store: StoryStore) -> None:
    # Three stories: one followed rumour, one resolved (not followed), one open not followed.
    followed = seed(store, "followed-one", "Слежу за этим", n=1)
    resolved = seed(store, "resolved-one", "Старый сюжет", StoryStatus.RESOLVED, n=2)
    other = seed(store, "other-one", "Другой сюжет", StoryStatus.DEVELOPING, n=3)
    store.follow(followed.id, NOW)

    page = archive_page.build(store, NOW)

    # Worked by hand: followed = 1, all = 3, resolved = 1.
    assert '<label for="f-followed">Слежу (1)</label>' in page
    assert '<label for="f-all">Все (3)</label>' in page
    assert '<label for="f-resolved">Завершённые (1)</label>' in page
    assert 'id="f-followed" checked>' in page  # something is followed: open on it
    assert 'id="f-all">' in page
    assert "empty-" not in page.split("<main>")[1]  # every filter shows something
    classes = article_classes(page)
    assert classes == {
        f"story-{followed.id}": "story followed",
        f"story-{resolved.id}": "story resolved",
        f"story-{other.id}": "story",
    }
    # The CSS hides what the checked filter does not show.
    assert "#f-followed:checked ~ main .story:not(.followed)" in page
    assert "#f-resolved:checked ~ main .story:not(.resolved)" in page


def test_an_unfollowed_story_is_no_longer_in_followed(store: StoryStore) -> None:
    story = seed(store, "was-followed", "Был в подписках")
    store.follow(story.id, NOW - timedelta(days=2))
    store.unfollow(story.id, NOW - timedelta(days=1))

    page = archive_page.build(store, NOW)

    assert '<label for="f-followed">Слежу (0)</label>' in page
    assert 'id="f-all" checked>' in page
    assert article_classes(page) == {f"story-{story.id}": "story"}
    assert '<p class="empty empty-followed">' in page


def test_the_timeline_lists_every_update_with_its_source_oldest_first(store: StoryStore) -> None:
    story = seed(
        store,
        "bearman-ferrari-2028",
        "Бирман в Ferrari в 2028?",
        entities=[Entity(EntityKind.DRIVER, "bearman"), Entity(EntityKind.TEAM, "haas")],
        n=1,
    )
    # Published 22:30 UTC on 14.10 is 00:30 on 15.10 in Warsaw (CEST, UTC+2).
    store.add_update(
        story.id,
        update(2, datetime(2026, 10, 14, 22, 30, tzinfo=UTC)),
        NOW,
        status=StoryStatus.DEVELOPING,
    )
    store.add_update(story.id, update(3, None), NOW)  # undated: when it was recorded

    page = archive_page.build(store, NOW)
    article = articles(page)[f"story-{story.id}"]

    assert f"<h2>#{story.id} Бирман в Ferrari в 2028?</h2>" in article
    assert '<span class="badge status-developing">развивается</span>' in article
    assert "обновлён 19.10.2026" in article  # the undated update, recorded at NOW
    assert "Кто: bearman (пилот), haas (команда)" in article
    assert "<summary>Хронология (3)</summary>" in article
    dates = re.findall(r"<time>([\d.]+)</time>", article)
    assert dates == ["30.09.2026", "15.10.2026", "19.10.2026"]
    links = re.findall(r'<a href="([^"]+)" target="_blank" rel="noopener noreferrer">', article)
    assert links == [f"https://example.com/story-{n}" for n in (1, 2, 3)]
    # The first update set the status, the second moved it, the third changed none.
    assert article.count('class="moved"') == 2
    assert "Статус → слух" in article
    assert "Статус → развивается" in article


def test_a_muted_followed_story_says_until_when(store: StoryStore) -> None:
    story = seed(store, "muted-one", "Без уведомлений")
    store.follow(story.id, NOW - timedelta(days=1))
    store.mute(story.id, datetime(2026, 10, 25, 23, tzinfo=UTC))  # 26.10 01:00 in Warsaw

    article = articles(archive_page.build(store, NOW))[f"story-{story.id}"]

    assert '<span class="badge follow">следишь</span>' in article
    assert "без уведомлений до 26.10.2026" in article
    # Once the mute is over, the badge goes.
    later = archive_page.build(store, datetime(2026, 10, 27, tzinfo=UTC))
    assert "без уведомлений" not in articles(later)[f"story-{story.id}"]


def test_every_stored_text_is_escaped(store: StoryStore) -> None:
    story = store.create(
        "escape-me",
        '<script>alert("t")</script>',
        "A & B <b>",
        StoryStatus.RUMOR,
        [Entity(EntityKind.PERSON, "o'brien<x>")],
        Update(
            headline='"><img src=x onerror=alert(1)>',
            url='https://example.com/a?b=1&c="2"',
            source="<i>Source</i>",
            what_changed_ru="1 < 2",
            published_at=None,
        ),
        NOW,
    )

    page = archive_page.build(store, NOW)

    assert "<script" not in page
    assert "<img" not in page
    assert "<b>" not in page and "<i>" not in page and "<x>" not in page
    article = articles(page)[f"story-{story.id}"]
    assert "&lt;script&gt;alert(&quot;t&quot;)&lt;/script&gt;" in article
    assert "A &amp; B &lt;b&gt;" in article
    assert 'href="https://example.com/a?b=1&amp;c=&quot;2&quot;"' in article
    assert "o&#x27;brien&lt;x&gt; (персона)" in article
    assert "1 &lt; 2" in article


def test_the_page_is_self_contained_and_allows_no_script(store: StoryStore) -> None:
    seed(store, "any", "Сюжет")
    page = archive_page.build(store, NOW)
    assert page.startswith("<!doctype html>")
    assert "default-src 'none'; style-src 'unsafe-inline'" in page
    assert '<meta name="referrer" content="no-referrer">' in page
    assert "<script" not in page
    # Nothing is loaded from elsewhere: the only URLs are the sources' links.
    assert re.findall(r'(?:src|href)="([^"]+)"', page) == ["https://example.com/story-1"]
    # The radios precede nav and main as siblings, which the `~` selectors need.
    body = page.split("<body>")[1]
    assert body.index('id="f-resolved"') < body.index("<nav>") < body.index("<main>")
    assert "Сформирован 19.10.2026 11:00 (Варшава) · сюжетов: 1" in page


def test_the_newest_story_comes_first(store: StoryStore) -> None:
    old = seed(store, "old", "Старый", published=datetime(2026, 8, 1, tzinfo=UTC), n=1)
    new = seed(store, "new", "Новый", published=datetime(2026, 10, 1, tzinfo=UTC), n=2)
    page = archive_page.build(store, NOW)
    assert list(articles(page)) == [f"story-{new.id}", f"story-{old.id}"]


def test_filename_uses_the_warsaw_date() -> None:
    # 22:30 UTC on 19.10 is already 20.10 in Warsaw.
    assert archive_page.filename(datetime(2026, 10, 19, 22, 30, tzinfo=UTC)) == (
        "f1-archive-2026-10-20.html"
    )


# --- the command ---------------------------------------------------------------------------


def test_the_command_writes_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    with StoryStore(tmp_path / "state.db") as opened:
        seed(opened, "any", "Сюжет для страницы")

    assert main(["archive-page", "--output", "out/archive.html"]) == 0

    page = (tmp_path / "out" / "archive.html").read_text(encoding="utf-8")
    assert "Сюжет для страницы" in page
    assert "wrote " in capsys.readouterr().out


def test_the_command_prints_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    with StoryStore(tmp_path / "state.db") as opened:
        seed(opened, "any", "Сюжет для страницы")
    assert main(["archive-page", "--output", "-"]) == 0
    assert capsys.readouterr().out.startswith("<!doctype html>")


def test_the_command_without_a_state_file_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["archive-page"]) == 1
    assert "no state file" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []

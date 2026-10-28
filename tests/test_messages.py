# Russian and Polish texts, and no-break spaces in numbers, are intended here.
# ruff: noqa: RUF001
"""The message catalog (F30): bot, alert and archive-page texts in ru / en / pl, the same keys
and template fields in every language, and per-locale dates, times, numbers and plurals."""

import json
import re
import string
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from conftest import load_fixture
from test_bot import CHAT, MockTelegram, api, message, press, seed, text_of

from f1brief import alerts, archive_page, bot, config, lifecycle, messages, schedule
from f1brief.bot import Bot, Reply
from f1brief.bot_texts import Texts, texts_for
from f1brief.messages import PLURAL_CATEGORIES, Catalog, CatalogError
from f1brief.schedule import SessionKind
from f1brief.stories import EntityKind, StoryStatus, StoryStore

LANGUAGES = config.LANGUAGES
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
# Monday 19.10.2026 11:00 in Warsaw (CEST): the next round is the United States GP.
NOW = datetime(2026, 10, 19, 9, tzinfo=UTC)


def raw(language: str) -> dict[str, Any]:
    return messages.load(language)


def leaves(table: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Every entry of a parsed catalog by its dotted key; plural tables are left whole (their
    forms differ by language)."""
    found: dict[str, Any] = {}
    for key, value in table.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and not path.startswith("plural."):
            found |= leaves(value, f"{path}.")
        else:
            found[path] = value
    return found


def fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


@pytest.fixture
def store(tmp_path: Path):
    with StoryStore(tmp_path / "state.db") as opened:
        yield opened


@pytest.fixture
def races() -> list[schedule.Race]:
    return schedule.parse_schedule(load_fixture("jolpica/schedule_2026.json"))


def handler(store: StoryStore, language: str, races: list[schedule.Race] | None = None) -> Bot:
    return Bot(
        store,
        CHAT,
        config.DEFAULT_FAVOURITES,
        lambda teams, drivers: config.DEFAULT_FAVOURITES,
        remove_favourite=lambda kind, fav_id: config.DEFAULT_FAVOURITES,
        calendar=(lambda: races) if races is not None else None,
        language=language,
    )


# --- the catalog files ----------------------------------------------------------------------


def test_there_is_a_catalog_for_every_language_and_no_other() -> None:
    folder = resources.files("f1brief").joinpath("messages")
    on_disk = {p.name.removesuffix(".toml") for p in folder.iterdir() if p.name.endswith(".toml")}
    assert on_disk == set(LANGUAGES)
    with pytest.raises(CatalogError):
        messages.load("de")


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_key_exists_in_every_language(language: str) -> None:
    assert leaves(raw(language)).keys() == leaves(raw("ru")).keys()


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_template_has_the_same_fields_in_every_language(language: str) -> None:
    reference = leaves(raw("ru"))
    for key, value in leaves(raw(language)).items():
        # Date formats may use different fields (a month name or number); texts may not.
        if isinstance(value, str) and not key.startswith("format."):
            assert fields(value) == fields(reference[key]), key


@pytest.mark.parametrize("language", LANGUAGES)
def test_plural_forms_cover_the_languages_categories(language: str) -> None:
    for key, forms in raw(language)["plural"].items():
        assert tuple(forms) == PLURAL_CATEGORIES[language], key
        assert all(fields(f) == {"n"} for f in forms.values()), key


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_text_is_empty_and_none_is_hidden_by_a_method(language: str) -> None:
    texts = texts_for(language)
    for key, value in raw(language)["text"].items():
        assert value.strip(), key
        assert not hasattr(Texts, key), f"{key} is hidden by Texts.{key}"
        assert getattr(texts, key) == value


@pytest.mark.parametrize("language", LANGUAGES)
def test_tables_cover_every_status_session_and_entity_kind(language: str) -> None:
    texts = texts_for(language)
    assert all(texts.status(s) for s in StoryStatus)
    assert all(texts.session(k) for k in SessionKind)
    assert all(texts.entity_kind(k) for k in EntityKind)
    assert len(raw(language)["status"]) == len(StoryStatus)
    assert len(raw(language)["sessions"]) == len(SessionKind)
    assert len(raw(language)["entity_kinds"]) == len(EntityKind)


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_command_menu_has_the_same_commands_in_every_language(language: str) -> None:
    commands = texts_for(language).commands()
    assert [c for c, _ in commands] == ["start", "stories", "archive", "next", "favourites", "help"]
    assert all(1 <= len(d) <= 256 for _, d in commands)


@pytest.mark.parametrize("language", ["en", "pl"])
def test_english_and_polish_texts_have_no_russian(language: str) -> None:
    for key, value in leaves(raw(language)).items():
        values = value.values() if isinstance(value, dict) else [value]
        for text in values if not isinstance(value, list) else value:
            assert not CYRILLIC.search(str(text)), key


def test_an_unknown_text_is_an_attribute_error() -> None:
    with pytest.raises(AttributeError, match="no text 'nope' in the en catalog"):
        _ = texts_for("en").nope


# --- plurals --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "n", "text"),
    [
        ("ru", 1, "1 день"),
        ("ru", 2, "2 дня"),
        ("ru", 5, "5 дней"),
        ("ru", 11, "11 дней"),
        ("ru", 12, "12 дней"),
        ("ru", 21, "21 день"),
        ("ru", 22, "22 дня"),
        ("ru", 0, "0 дней"),
        ("pl", 1, "1 dzień"),
        ("pl", 2, "2 dni"),
        ("pl", 5, "5 dni"),
        ("pl", 21, "21 dni"),  # pl: only 1 is "one"
        ("pl", 22, "22 dni"),
        ("en", 1, "1 day"),
        ("en", 0, "0 days"),
        ("en", 21, "21 days"),
    ],
)
def test_days_by_plural_rules(language: str, n: int, text: str) -> None:
    assert texts_for(language).days(n) == text


@pytest.mark.parametrize(
    ("language", "n", "category"),
    [("ru", 111, "many"), ("ru", 101, "one"), ("ru", 104, "few"), ("pl", 112, "many"),
     ("pl", 104, "few"), ("pl", 101, "many"), ("en", 2, "other")],
)  # fmt: skip
def test_plural_categories(language: str, n: int, category: str) -> None:
    assert messages.PLURAL_RULES[language](n) == category


# --- dates, times and numbers ---------------------------------------------------------------

# 17:30 UTC on Friday 23.10.2026 is 19:30 in Warsaw (CEST); on Friday 30.10.2026 it is 18:30
# (CET, after the clocks went back on 25.10).
SUMMER = datetime(2026, 10, 23, 17, 30, tzinfo=UTC)
WINTER = datetime(2026, 10, 30, 17, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("language", "date_", "date_time", "day_time", "winter"),
    [
        ("ru", "23.10.2026", "23.10.2026 19:30", "Пт 23.10 19:30", "Пт 30.10 18:30"),
        ("pl", "23.10.2026", "23.10.2026 19:30", "pt. 23.10 19:30", "pt. 30.10 18:30"),
        ("en", "23 Oct 2026", "23 Oct 2026 19:30", "Fri 23 Oct 19:30", "Fri 30 Oct 18:30"),
    ],
)
def test_dates_and_24_hour_times_in_warsaw(
    language: str, date_: str, date_time: str, day_time: str, winter: str
) -> None:
    texts = texts_for(language)
    assert texts.date(SUMMER) == date_
    assert texts.date_time(SUMMER) == date_time
    assert texts.day_time(SUMMER) == day_time
    assert texts.day_time(WINTER) == winter


def test_times_are_24_hour_and_a_late_utc_time_is_the_next_warsaw_day() -> None:
    late = datetime(2026, 7, 4, 22, 5, tzinfo=UTC)  # 00:05 on Sunday 5 July in Warsaw
    assert texts_for("en").date_time(late) == "5 Jul 2026 00:05"
    assert texts_for("ru").date_time(datetime(2026, 7, 4, 21, 5, tzinfo=UTC)) == "04.07.2026 23:05"
    assert texts_for("pl").day(date(2026, 7, 5)) == "niedz. 05.07"


@pytest.mark.parametrize(
    ("language", "value", "digits", "text"),
    [
        ("ru", 1234.5, 1, "1 234,5"),
        ("pl", 1234.5, 1, "1 234,5"),
        ("en", 1234.5, 1, "1,234.5"),
        ("ru", 0.125, 2, "0,13"),  # half up, not to even
        ("en", Decimal("2.25"), 1, "2.3"),
        ("pl", -3.75, 1, "-3,8"),
        ("en", 1234567, 0, "1,234,567"),
        ("ru", 7, 0, "7"),
    ],
)
def test_numbers_with_the_locales_separators(
    language: str, value: float, digits: int, text: str
) -> None:
    assert texts_for(language).number(value, digits) == text


def test_catalogs_are_loaded_once() -> None:
    assert texts_for("pl") is texts_for("pl")
    assert messages.catalog("en") is messages.catalog("en")
    assert isinstance(texts_for("en"), Catalog)


# --- the bot, alerts, offers and the archive page in each language ----------------------------


@pytest.mark.parametrize(
    ("language", "first_line", "next_digest"),
    [
        (
            "en",
            "Hi! I send an F1 digest 2 days before the first practice",
            "The next digest comes Wed 21 Oct 00:00 Warsaw time, for the «United States Grand "
            "Prix» round.",
        ),
        (
            "pl",
            "Cześć! Wysyłam przegląd F1 na 2 dni przed pierwszym treningiem",
            "Następny przegląd przyjdzie śr. 21.10 00:00 czasu warszawskiego — na rundę «United "
            "States Grand Prix».",
        ),
    ],
)
def test_start_in_english_and_polish(
    store: StoryStore,
    races: list[schedule.Race],
    language: str,
    first_line: str,
    next_digest: str,
) -> None:
    [reply] = handler(store, language, races).handle(message("/start"), NOW)
    lines = reply.text.splitlines()
    assert len(lines) == 4
    assert lines[0].startswith(first_line)
    assert lines[3] == next_digest
    assert reply.buttons == bot.main_menu(texts_for(language))
    assert not CYRILLIC.search(reply.text)


def test_the_menu_is_translated_but_its_callbacks_are_not() -> None:
    menus = {lang: bot.main_menu(texts_for(lang)) for lang in LANGUAGES}
    labels = {lang: [b.text for row in m for b in row] for lang, m in menus.items()}
    data = {lang: [b.data for row in m for b in row] for lang, m in menus.items()}
    assert labels["en"] == ["📰 Stories", "🗂 Archive", "🏁 Next race", "⭐ Favourites", "❓ Help"]
    assert labels["pl"] == [
        "📰 Wątki",
        "🗂 Archiwum",
        "🏁 Następny wyścig",
        "⭐ Ulubione",
        "❓ Pomoc",
    ]
    assert data["en"] == data["pl"] == data["ru"]


def test_next_in_polish_names_the_sessions_and_the_back_button(
    store: StoryStore, races: list[schedule.Race]
) -> None:
    [reply] = handler(store, "pl", races).handle(message("/next"), NOW)
    assert reply.text.splitlines()[0] == "🏁 Runda 18: United States Grand Prix (2026)"
    assert "Harmonogram (czas warszawski):" in reply.text
    assert "pt. 23.10 19:30 — Trening 1" in reply.text
    assert "niedz. 25.10 21:00 — Wyścig" in reply.text  # after the clocks went back
    assert reply.buttons[-1] == (bot.Button("← Menu", "menu:main"),)


def test_a_story_timeline_and_its_buttons_in_english(store: StoryStore) -> None:
    story = seed(store)
    actions = handler(store, "en", []).handle(message(f"/follow {story.id}"), NOW)
    assert text_of(actions).startswith(f"You now follow story #{story.id} «")
    actions = handler(store, "en", []).handle(press(f"story:{story.id}"), NOW)
    text = text_of(actions)
    assert "Status: rumour · you follow it" in text
    assert "Timeline:" in text
    labels = [b.text for r in actions if isinstance(r, Reply) for row in r.buttons for b in row]
    assert labels[:2] == ["Unfollow", "No notifications for 7 days"]


def test_an_unknown_command_in_english(store: StoryStore) -> None:
    text = text_of(handler(store, "en").handle(message("/nope"), NOW))
    assert text.startswith("I don't know the command /nope.\n\nCommands:")


def test_the_command_menu_is_registered_in_the_configured_language() -> None:
    mock = MockTelegram()
    assert bot.register_commands(api(mock), language="pl") is True
    [sent] = mock.sent("setMyCommands")
    assert sent["commands"][0] == {"command": "start", "description": "Co potrafi bot i menu"}


def test_an_alert_in_english(store: StoryStore) -> None:
    story = seed(store)
    store.follow(story.id, NOW - timedelta(days=1))
    store.add_update(story.id, _update(2), NOW, None)
    [alert] = alerts.plan(store, NOW).send
    [reply] = alerts.replies(alert, "en")
    assert reply.text.startswith(f"New in story #{story.id} «")
    assert reply.text.endswith(f"Timeline: /story {story.id}")
    assert [[b.text for b in row] for row in reply.buttons] == [["Unfollow"]]


def test_the_offer_to_unfollow_in_polish(store: StoryStore) -> None:
    story = seed(store)
    store.follow(story.id, NOW)
    later = NOW + timedelta(days=60)
    [offer] = lifecycle.plan(store, later).send
    reply = lifecycle.reply(offer, later, "pl")
    assert reply.text.startswith(f"Wątek #{story.id} «")
    assert "(plotka): brak wiadomości od 60 dni" in reply.text
    assert reply.text.endswith(f"Historia: /story {story.id}")
    assert reply.buttons[0][0].text == "Przestań obserwować"


def test_the_archive_page_in_english(store: StoryStore) -> None:
    story = seed(store)
    store.follow(story.id, NOW)
    page = archive_page.build(store, NOW, "en")
    assert '<html lang="en">' in page
    assert "<title>F1 Brief — story archive</title>" in page
    assert "Generated 19 Oct 2026 11:00 (Warsaw) · stories: 1" in page
    assert '<label for="f-followed">Following (1)</label>' in page
    assert "(driver)" in page
    assert "Rumours are marked with the status «rumour»." in page
    for russian in ("Архив", "Сформирован", "следишь", "Кто:", "Хронология", "слух", "пилот"):
        assert russian not in page


def test_the_bot_sends_the_archive_page_in_its_language(store: StoryStore) -> None:
    seed(store)
    actions = handler(store, "pl").handle(message("/archive"), NOW)
    [page] = [a for a in actions if isinstance(a, bot.Document)]
    assert b'<html lang="pl">' in page.content
    assert page.caption.startswith("Archiwum jako strona")


def test_the_cli_bot_answers_in_the_configured_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text('[digest]\nlanguage = "en"\n', encoding="utf-8")
    updates = tmp_path / "updates.json"
    update = {
        "update_id": 1,
        "message": {"message_id": 1, "chat": {"id": int(CHAT)}, "date": 0, "text": "/help"},
    }
    updates.write_text(json.dumps([update]), encoding="utf-8")
    from f1brief.cli import main

    assert main(["bot", "--dry-run", "--updates", str(updates), "--chat-id", CHAT]) == 0
    out = capsys.readouterr().out
    assert "Commands:" in out
    assert "/story <number> — a story's timeline with sources" in out


def _update(n: int):
    from f1brief.stories import Update

    return Update(
        headline=f"Synthetic headline {n}",
        url=f"https://example.com/story-{n}",
        source="Example News",
        what_changed=f"Synthetic change {n}.",
        language="en",
        published_at=NOW - timedelta(hours=1),
    )

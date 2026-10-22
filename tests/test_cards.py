"""Designed cards (F41): the model's structured answer checked against the facts, the header
and schedule from the facts, rendering snapshots for each kind and the `preview` command."""

# Russian and Polish texts: Cyrillic letters that look like Latin ones are intended here.
# ruff: noqa: RUF001
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import facts_transport
import httpx
import pytest
from test_telegram_html import assert_valid
from test_telegram_send import CHAT_ID, TOKEN

from f1brief import cards, cli, facts, preview
from f1brief import telegram_send as ts
from f1brief.cli import main
from f1brief.config import DEFAULT_FAVOURITES

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
SNAPSHOTS = Path(__file__).parent / "snapshots"
# Set to rewrite the snapshot files from the current rendering (then review the diff).
UPDATE = os.environ.get("F1BRIEF_UPDATE_SNAPSHOTS") == "1"


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    with facts_transport.client([]) as client:
        return facts.collect(NOW, DEFAULT_FAVOURITES, client, sleep=lambda _: None)


def standings_source(document: dict[str, Any]) -> dict[str, str]:
    return dict(document["sections"]["standings"]["sources"][0])


def news_source(document: dict[str, Any]) -> dict[str, str]:
    item = document["sections"]["news"]["items"][0]
    return {"name": item["sources"][0], "url": item["link"]}


def answer(*sections: dict[str, Any], title: str = "Гран-при США") -> str:
    return json.dumps({"title": title, "sections": list(sections)}, ensure_ascii=False)


def section(section_id: str, heading: str, *items: dict[str, Any]) -> dict[str, Any]:
    return {"id": section_id, "heading": heading, "items": list(items)}


# --- the model's answer against the facts ---------------------------------------------------


def test_only_sources_from_the_facts_survive(document: dict[str, Any]) -> None:
    warnings: list[str] = []
    standings, news = standings_source(document), news_source(document)
    made_up = {"name": "Jolpica-F1", "url": "https://api.jolpi.ca/ergast/f1/made-up.json"}
    raw = answer(
        section(
            "standings",
            "Зачёт",
            {"text": "Рассел — **P4**.", "sources": [standings, standings]},
            {"text": "Новость.", "sources": [news, made_up]},
            {"text": "Выдумка.", "sources": [made_up]},
            {"text": "Без источника."},
            {"text": "Пит-стопы неизвестны.", "unknown": True},
        ),
        section("track", "Трасса", {"text": "Выдумка.", "sources": [made_up]}),
    )

    card = cards.from_model(cards.DIGEST, raw, document, "ru", warnings.append)

    [kept] = card.sections
    assert [i.text for i in kept.items] == ["Рассел — **P4**.", "Новость.", "Пит-стопы неизвестны."]
    assert kept.items[0].sources == (cards.Source(**standings),)  # the duplicate is merged
    assert kept.items[1].sources == (cards.Source(**news),)
    assert kept.items[2] == cards.Item("Пит-стопы неизвестны.", (), unknown=True)
    assert sum("not in the facts" in w for w in warnings) == 3
    assert any("item 4: dropped an item without a source" in w for w in warnings)
    assert any("section track: no items left; dropped" in w for w in warnings)


def test_every_item_of_a_card_cites_the_facts(document: dict[str, Any]) -> None:
    """Whatever the answer, each item left is either sourced from the facts or says that
    something is unknown."""
    known = cards.facts_strings(document)
    raw = answer(
        section("standings", "Зачёт", {"text": "a", "sources": [standings_source(document)]}),
        section("drivers", "Гонщики", {"text": "b", "sources": [news_source(document)]}),
        section("weather", "Погода", {"text": "c", "sources": [{"name": "X", "url": "https://x"}]}),
        section("expect", "Чего ждать", {"text": "d", "unknown": True}),
    )
    card = cards.from_model(cards.DIGEST, raw, document, "ru", lambda _: None)
    items = [i for s in card.sections for i in s.items]
    assert len(items) == 3
    for item in items:
        assert item.unknown or item.sources
        for source in item.sources:
            assert source.name in known and source.url in known


def test_a_fenced_answer_is_accepted(document: dict[str, Any]) -> None:
    raw = "```json\n" + answer(section("expect", "Чего ждать", {"text": "x", "unknown": True}))
    card = cards.from_model(cards.DIGEST, raw + "\n```", document, "ru", lambda _: None)
    assert card.title == "Гран-при США"


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ("1. Раунд\nОстин.", "holds no JSON object"),
        ('{"title": "x", "sections": [}', "not valid JSON"),
        ('["x"]', "holds no JSON object"),
        (answer(title=""), "has no title"),
        (answer(section("expect", "x", {"text": "y"})), "no section with a sourced item"),
    ],
)
def test_an_unusable_answer_is_an_error(document: dict[str, Any], raw: str, error: str) -> None:
    with pytest.raises(cards.CardError, match=error):
        cards.from_model(cards.DIGEST, raw, document, "ru", lambda _: None)


def test_an_unknown_section_id_is_kept_with_a_plain_marker(document: dict[str, Any]) -> None:
    warnings: list[str] = []
    raw = answer(section("gossip", "Слухи", {"text": "x", "unknown": True}))
    card = cards.from_model(cards.DIGEST, raw, document, "ru", warnings.append)
    assert card.sections[0].id == "gossip"
    assert warnings == ["section gossip: not a digest section id; shown with a plain marker"]
    assert "<b>📌 Слухи</b>" in cards.render(card)[0]


# --- what code adds: dates and the schedule -------------------------------------------------


def test_the_digest_header_has_the_weekend_dates_and_the_schedule(
    document: dict[str, Any],
) -> None:
    raw = answer(section("expect", "Чего ждать", {"text": "x", "unknown": True}))
    card = cards.from_model(cards.DIGEST, raw, document, "ru", lambda _: None)
    assert card.subtitle == "23–25.10.2026"
    # Warsaw leaves summer time on 25.10: the race is still at 21:00 Warsaw time.
    assert card.schedule == (
        ("Пт 23.10 19:30", "Практика 1"),
        ("Пт 23.10 23:00", "Практика 2"),
        ("Сб 24.10 19:30", "Практика 3"),
        ("Сб 24.10 23:00", "Квалификация"),
        ("Вс 25.10 21:00", "Гонка"),
    )
    first = cards.render(card)[0]
    assert first.startswith(
        "🏁 <b>Гран-при США</b>\n23–25.10.2026\n\nРасписание, время варшавское\n"
        "<pre>Пт 23.10 19:30  Практика 1\nПт 23.10 23:00  Практика 2\n"
    )


@pytest.mark.parametrize(
    ("language", "row", "label"),
    [
        ("ru", ("Пт 23.10 --:--", "Практика 1"), "--:-- — время уточняется"),
        ("en", ("Fri 23.10 --:--", "Practice 1"), "--:-- — time to be confirmed"),
        ("pl", ("Pt 23.10 --:--", "Trening 1"), "--:-- — godzina do potwierdzenia"),
    ],
)
def test_a_session_without_a_time_is_marked_in_each_language(
    language: str, row: tuple[str, str], label: str
) -> None:
    race = {"sessions": [{"session": "FP1", "date": "2026-10-23", "start_warsaw": None}]}
    rows = cards.schedule_rows(race, language)
    assert rows == (row,)
    lines = [line.render() for line in cards.schedule_block(rows, language).lines]
    assert lines[-1] == label


def test_the_quali_and_summary_headers() -> None:
    race = {
        "sessions": [
            {"session": "Race", "date": "2026-10-25", "start_warsaw": "2026-10-25T21:00:00+01:00"}
        ]
    }
    quali = {"sections": {"race": race}}
    assert cards.header(cards.QUALI, quali, "ru") == ("Гонка: Вс 25.10 21:00 по Варшаве", ())
    assert cards.header(cards.QUALI, quali, "en") == ("Race: Sun 25.10 21:00, Warsaw time", ())
    summary = {"period": {"from": "2026-12-01", "to": "2027-01-01"}, "sections": {}}
    assert cards.header(cards.SUMMARY, summary, "ru") == ("01.12.2026–01.01.2027", ())
    assert cards.header(cards.DIGEST, {"sections": {"race": None}}, "ru") == ("", ())


@pytest.mark.parametrize(
    ("first", "last", "text"),
    [
        (date(2026, 10, 23), date(2026, 10, 25), "23–25.10.2026"),
        (date(2026, 10, 30), date(2026, 11, 1), "30.10–01.11.2026"),
        (date(2026, 12, 28), date(2027, 1, 3), "28.12.2026–03.01.2027"),
        (date(2026, 10, 25), date(2026, 10, 25), "25.10.2026"),
    ],
)
def test_date_ranges(first: date, last: date, text: str) -> None:
    assert cards.date_range(first, last) == text


# --- rendering ------------------------------------------------------------------------------


def test_team_sections_get_their_colour_and_sources_are_short_labels() -> None:
    url = "https://example.com/a"
    item = cards.Item(
        "x",
        (
            cards.Source("Jolpica-F1", url),
            cards.Source("Jolpica-F1", url + "2"),
            cards.Source("BBC <Sport>", url + "&b"),
        ),
    )
    card = cards.Card(
        cards.DIGEST,
        "ru",
        "T",
        sections=(
            cards.Section("team:mclaren", "McLaren", (item,)),
            cards.Section("team:haas", "Haas", (item,)),
            cards.Section("team:newcomer", "Newcomer", (item,)),
        ),
    )
    text = cards.render(card)[0]
    assert "<b>🧡 McLaren</b>\n• x · " in text
    assert "<b>🤍 Haas</b>" in text and "<b>🏎 Newcomer</b>" in text
    # One label per source name, linked to its first URL; never a raw URL.
    assert (
        '• x · <a href="https://example.com/a">Jolpica-F1</a>, '
        '<a href="https://example.com/a&amp;b">BBC &lt;Sport&gt;</a>'
    ) in text


def test_a_long_section_is_folded_and_a_short_one_is_not() -> None:
    short = cards.Section("track", "Трасса", (cards.Item("коротко", unknown=True),))
    long = cards.Section(
        "standings",
        "Зачёт",
        tuple(cards.Item("новость " * 20, unknown=True) for _ in range(5)),
    )
    text = cards.render(cards.Card(cards.DIGEST, "ru", "T", sections=(short, long)))[0]
    assert "<b>🛣 Трасса</b>\n• коротко" in text
    assert "<b>📊 Зачёт</b>\n<blockquote expandable>• новость" in text


def test_a_card_survives_saving() -> None:
    card = preview.sample_card("digest")
    assert cards.from_dict(json.loads(json.dumps(card.to_dict()))) == card
    with pytest.raises(cards.CardError, match="not a saved card"):
        cards.from_dict({"kind": "letter"})


def test_a_long_card_splits_with_headings_kept_with_their_items() -> None:
    card = preview.sample_card("digest")
    headings = {f"<b>{cards.section_emoji(card.kind, s.id)} {s.heading}</b>" for s in card.sections}
    messages = cards.render(card, limit=600)
    assert len(messages) > 4
    for message in messages:
        assert_valid(message)
        assert len(message.encode("utf-16-le")) // 2 <= 600
        assert message.splitlines()[-1] not in headings


# --- snapshots ------------------------------------------------------------------------------


def rendered(kind: str) -> str:
    replies = preview.replies(kind)
    parts = []
    for n, reply in enumerate(replies, 1):
        buttons = " ".join(f"[{b.text} -> {b.data}]" for row in reply.buttons for b in row)
        parts.append(f"===== message {n}/{len(replies)} =====\n{reply.text}\n")
        if buttons:
            parts.append(f"===== buttons =====\n{buttons}\n")
    return "".join(parts)


@pytest.mark.parametrize("kind", preview.KINDS)
def test_rendering_snapshot(kind: str) -> None:
    path = SNAPSHOTS / f"{kind}.html"
    text = rendered(kind)
    if UPDATE:
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")
    for reply in preview.replies(kind):
        assert_valid(reply.text)


# --- f1brief preview ------------------------------------------------------------------------


def test_preview_prints_a_sample_without_settings_or_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    def no_client() -> httpx.Client:
        raise AssertionError("printing a preview must not create an HTTP client")

    monkeypatch.setattr(cli, "_telegram_client", no_client)
    assert main(["preview", "--kind", "alert"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("🔔 <b>Образец:")
    assert "[Хронология] [Отписаться]" in captured.out
    assert "--send sends them to the admin chat" in captured.err


def test_preview_send_goes_to_the_admin_chat_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ts.TOKEN_VAR, TOKEN)
    monkeypatch.setenv(ts.CHAT_ID_VAR, CHAT_ID)
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(cli, "_telegram_client", lambda: httpx.Client(transport=transport))

    assert main(["preview", "--kind", "digest", "--send"]) == 0
    assert [m["chat_id"] for m in sent] == [CHAT_ID] * len(preview.replies("digest"))
    assert {m["parse_mode"] for m in sent} == {"HTML"}
    assert sent[0]["text"].startswith("🏁 <b>Гран-при США (образец)</b>")
    assert "sent 1 sample message(s) to the admin chat" in capsys.readouterr().err


def test_preview_send_without_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ts.TOKEN_VAR, raising=False)
    monkeypatch.delenv(ts.CHAT_ID_VAR, raising=False)
    assert main(["preview", "--kind", "quali", "--send"]) == 1
    assert "not set" in capsys.readouterr().err

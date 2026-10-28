"""The daily job in the off-season (F18): the monthly summary. Telegram, the news feeds and
`claude -p` are all stubs; the calendar is the recorded 2026 one."""

# The summary is Russian: Cyrillic letters that look like Latin ones are intended here.
# ruff: noqa: RUF001
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_digest import facts_of as digest_facts
from test_telegram_send import CHAT_ID, PARSE_ERROR, TOKEN, Recorder

from f1brief import cli, digest, news, schedule
from f1brief import telegram_send as ts
from f1brief.cli import main
from f1brief.news import NewsItem, NewsReport
from f1brief.news_archive import NewsArchive
from f1brief.send_log import SendLog, Status

DUE = datetime(2026, 1, 3, 8, tzinfo=UTC)  # Sat 3 Jan: off-season, early in the month
NOT_DUE = datetime(2025, 12, 20, 8, tzinfo=UTC)  # mid-month: news is only archived
SUMMARY = json.dumps(
    {
        "title": "Межсезонье",
        "sections": [
            {
                "id": "moves",
                "heading": "Переходы",
                "items": [{"text": "Ничего нового.", "unknown": True}],
            }
        ],
    },
    ensure_ascii=False,
)


def news_item(title: str, link: str, published: datetime) -> NewsItem:
    return NewsItem(
        title=title,
        link=link,
        published=published,
        summary="",
        sources=("BBC Sport",),
        favourites=frozenset({"Ocon"}),
        topics=frozenset({"contracts"}),
        future=True,
    )


EARLY = news_item(
    "Ocon talks continue", "https://example.com/early", datetime(2025, 12, 19, tzinfo=UTC)
)
LATE = news_item(
    "Ocon signs a new deal", "https://example.com/late", datetime(2026, 1, 2, tzinfo=UTC)
)


class Stubs:
    """Today's news, the generated text, and what the summary was generated from."""

    def __init__(self) -> None:
        self.news: list[NewsItem] = []
        self.fetches = 0
        self.documents: list[dict[str, Any]] = []
        self.languages: list[str] = []

    def fetch_news(self, since: datetime, *args: Any, **kwargs: Any) -> NewsReport:
        self.fetches += 1
        return NewsReport(since, tuple(i for i in self.news if i.published >= since), ())

    def generate(self, document: dict[str, Any], model: Any, language: str) -> str:
        self.documents.append(document)
        self.languages.append(language)
        return SUMMARY


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, schedule_2026: dict[str, Any]):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ts.TOKEN_VAR, TOKEN)
    monkeypatch.setenv(ts.CHAT_ID_VAR, CHAT_ID)
    races = schedule.parse_schedule(schedule_2026)
    calendars = {"current": races, 2026: races}

    def fetch_schedule(season: int | str = "current", client: Any = None) -> list:
        return calendars.get(season, [])

    monkeypatch.setattr(schedule, "fetch_schedule", fetch_schedule)
    monkeypatch.setattr(cli, "_now", lambda: DUE)
    stubs = Stubs()
    monkeypatch.setattr(news, "fetch_news", stubs.fetch_news)
    monkeypatch.setattr(digest, "generate_summary", stubs.generate)
    recorder = Recorder()
    monkeypatch.setattr(cli, "_telegram_client", recorder.client)
    return stubs, recorder


def summary(month: str = "2026-01"):
    with SendLog("state.db") as log:
        return log.get_summary(month)


def test_sends_the_summary_once_a_month(setup, capsys: pytest.CaptureFixture[str]) -> None:
    stubs, recorder = setup
    stubs.news = [LATE]

    assert main(["run"]) == 0
    assert recorder.texts() == [
        "🗓 <b>Межсезонье</b>\n01.12.2025–03.01.2026\n\n<b>🔄 Переходы</b>\n• Ничего нового."
    ]
    saved = summary()
    assert saved is not None and (saved.status, saved.messages) == (Status.SENT, 1)
    err = capsys.readouterr().err
    assert "Off-season: monthly summary for 2026-01\nDue: the 2026-01 summary." in err
    assert err.endswith("sent 1 message(s) for the 2026-01 summary\n")

    (document,) = stubs.documents
    assert document["period"] == {"from": "2025-12-01", "to": "2026-01-03"}
    assert document["sections"]["season_start"]["name"] == "Australian Grand Prix"
    assert [i["link"] for i in document["sections"]["news"]["items"]] == [LATE.link]

    assert main(["run"]) == 0
    assert len(recorder.requests) == 1
    assert "The 2026-01 summary was already sent at Sat 03 Jan 09:00." in capsys.readouterr().err


def test_news_archived_earlier_in_the_month_reaches_the_summary(
    setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    stubs, recorder = setup
    # 20 December: off-season, not due. The feeds carry EARLY, which is archived.
    monkeypatch.setattr(cli, "_now", lambda: NOT_DUE)
    stubs.news = [EARLY]
    assert main(["run"]) == 0
    assert (recorder.requests, stubs.documents) == ([], [])

    # 3 January: the feeds have moved on to LATE, but the summary still has both.
    monkeypatch.setattr(cli, "_now", lambda: DUE)
    stubs.news = [LATE]
    assert main(["run"]) == 0
    (document,) = stubs.documents
    links = [i["link"] for i in document["sections"]["news"]["items"]]
    assert links == [LATE.link, EARLY.link]


def test_dry_run_prints_and_records_nothing(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stubs, recorder = setup
    monkeypatch.delenv(ts.TOKEN_VAR)
    stubs.news = [LATE]

    assert main(["run", "--dry-run"]) == 0
    assert recorder.requests == []
    assert summary() is None
    with NewsArchive("state.db") as archive:
        assert archive.since(datetime(2025, 1, 1, tzinfo=UTC)) == []
    captured = capsys.readouterr()
    assert "Переходы" in captured.out
    assert "dry run: 1 message(s) not sent, nothing recorded" in captured.err

    # A dry run on a day that is not due does not even fetch the news.
    monkeypatch.setattr(cli, "_now", lambda: NOT_DUE)
    fetches = stubs.fetches
    assert main(["run", "--dry-run"]) == 0
    assert stubs.fetches == fetches


def test_missing_settings_fail_before_claude_runs(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stubs, _ = setup
    monkeypatch.delenv(ts.TOKEN_VAR)

    assert main(["run", "--env-file", "missing.env"]) == 1
    assert stubs.documents == []
    assert "error: TELEGRAM_BOT_TOKEN not set" in capsys.readouterr().err


def test_a_refused_summary_is_retried(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, recorder = setup
    refused = Recorder(httpx.Response(400, json=PARSE_ERROR))
    monkeypatch.setattr(cli, "_telegram_client", refused.client)
    assert main(["run"]) == 1
    assert summary() is None
    assert "nothing was delivered; the next run will try again" in capsys.readouterr().err

    monkeypatch.setattr(cli, "_telegram_client", recorder.client)
    assert main(["run"]) == 0
    assert len(recorder.requests) == 1


def test_a_partial_summary_names_its_forget_command(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def timeout() -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(cli, "_telegram_client", Recorder(timeout).client)
    assert main(["run"]) == 1
    saved = summary()
    assert saved is not None and saved.status is Status.PARTIAL
    assert "`f1brief forget --month 2026-01`" in capsys.readouterr().err

    assert main(["forget", "--month", "2026-01"]) == 0
    assert capsys.readouterr().out == "forgot the 2026-01 summary\n"
    assert summary() is None
    assert main(["forget", "--month", "2026-01"]) == 0
    assert capsys.readouterr().out == "the 2026-01 summary was not in state.db\n"


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--season", "2026"],
        ["--month", "2026-01", "--season", "2026", "--round", "1"],
        ["--month", "2026-13"],
        ["--month", "January"],
    ],
)
def test_forget_needs_a_round_or_a_month(setup, args: list[str]) -> None:
    assert main(["forget", *args]) == 2


def test_due_shows_the_off_season_decision(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stubs, recorder = setup
    monkeypatch.setattr(cli, "_now", lambda: NOT_DUE)

    assert main(["due"]) == 0
    # The current calendar (2026) is not this year's (2025), which has none yet.
    assert capsys.readouterr().out == (
        "Round 1: Australian Grand Prix (2026)\n"
        "Send window: Wed 04 Mar 00:00 - Fri 06 Mar 02:30 (Europe/Warsaw)\n"
        "Too early: the window opens Wed 04 Mar 00:00.\n"
        "Off-season: monthly summary for 2025-12\n"
        "The monthly summary goes out in the first 7 days of a month.\n"
    )
    assert (stubs.fetches, recorder.requests) == (0, [])


def test_summary_writes_the_prompt_on_any_day(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stubs, recorder = setup
    # Mid-season: the next race is round 18, and the period is the previous month on.
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 10, 16, 8, tzinfo=UTC))
    stubs.news = [
        news_item("Ocon news", "https://example.com/oct", datetime(2026, 10, 15, tzinfo=UTC))
    ]

    assert main(["summary", "--prompt-only"]) == 0
    prompt = capsys.readouterr().out
    document = digest_facts(prompt)
    assert document["period"] == {"from": "2026-09-01", "to": "2026-10-16"}
    assert document["sections"]["season_start"]["round"] == 18
    assert [i["link"] for i in document["sections"]["news"]["items"]] == ["https://example.com/oct"]
    assert prompt.startswith("<facts>") and "Write the summary in Russian." in prompt
    # Nothing is sent or recorded.
    assert recorder.requests == []
    with NewsArchive("state.db") as archive:
        assert archive.since(datetime(2026, 1, 1, tzinfo=UTC)) == []


def test_summary_writes_the_text(setup, tmp_path: Path) -> None:
    stubs, recorder = setup
    assert main(["summary", "--output", "summary.txt"]) == 0
    card = json.loads((tmp_path / "summary.txt").read_text(encoding="utf-8"))
    assert (card["kind"], card["title"]) == ("summary", "Межсезонье")
    [section] = card["sections"]
    assert section["items"] == [{"text": "Ничего нового.", "sources": [], "unknown": True}]
    assert len(stubs.documents) == 1
    assert recorder.requests == []
    assert stubs.languages == ["ru"]


def test_the_summary_is_written_in_the_configured_language(setup, tmp_path: Path) -> None:
    stubs, recorder = setup
    stubs.news = [LATE]
    (tmp_path / "config.toml").write_text('[digest]\nlanguage = "en"\n', encoding="utf-8")

    assert main(["run"]) == 0
    assert main(["summary", "--output", "summary.txt"]) == 0
    assert stubs.languages == ["en", "en"]
    assert len(recorder.requests) == 1

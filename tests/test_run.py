"""The daily job (`f1brief run`), `due` and `forget`: every Telegram request goes to a mock."""

# The digest is Russian: Cyrillic letters that look like Latin ones are intended here.
# ruff: noqa: RUF001
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_telegram_send import CHAT_ID, OK, PARSE_ERROR, TOKEN, Recorder

from f1brief import cli, digest, facts, schedule
from f1brief import telegram_send as ts
from f1brief.cli import main
from f1brief.send_log import SendLog, Status

IN_WINDOW = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)  # Wed, round 18's window is open
TOO_EARLY = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)
TWO_PARTS = "1. Раунд\nОстин.\n\n7. Чего ожидать\nДождь."


def facts_about(season: int, round_: int) -> dict[str, Any]:
    return {"sections": {"race": {"season": season, "round": round_}}, "missing": []}


class Pipeline:
    """Stubs for everything `run` calls before Telegram, counting the calls."""

    def __init__(self, document: dict[str, Any], text: str) -> None:
        self.document, self.text = document, text
        self.collected = self.generated = 0
        self.stories: list[dict[str, Any]] = []

    def collect(self, now: datetime, favourites: object, stories: Any = None) -> dict[str, Any]:
        self.collected += 1
        if stories is not None:
            self.stories.append(stories())
        return self.document

    def generate(self, document: dict[str, Any], model: digest.ClaudeCli) -> str:
        self.generated += 1
        return self.text


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, schedule_2026: dict[str, Any]):
    """The 2026 calendar, the clock inside round 18's window, synthetic Telegram settings and
    a working directory of its own (so `state.db` and `.env` are fresh)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ts.TOKEN_VAR, TOKEN)
    monkeypatch.setenv(ts.CHAT_ID_VAR, CHAT_ID)
    races = schedule.parse_schedule(schedule_2026)
    monkeypatch.setattr(schedule, "fetch_schedule", lambda: races)
    monkeypatch.setattr(cli, "_now", lambda: IN_WINDOW)
    pipeline = Pipeline(facts_about(2026, 18), TWO_PARTS)
    monkeypatch.setattr(facts, "collect", pipeline.collect)
    monkeypatch.setattr(digest, "generate", pipeline.generate)
    recorder = Recorder()
    monkeypatch.setattr(cli, "_telegram_client", recorder.client)
    return pipeline, recorder


def record(round_: int = 18, path: str = "state.db"):
    with SendLog(path) as log:
        return log.get(2026, round_)


def test_sends_once_when_due(setup, capsys: pytest.CaptureFixture[str]) -> None:
    pipeline, recorder = setup

    assert main(["run"]) == 0
    assert len(recorder.requests) == 1
    assert recorder.texts()[0].startswith("*1\\. Раунд*")
    saved = record()
    assert saved is not None and (saved.status, saved.messages) == (Status.SENT, 1)
    err = capsys.readouterr().err
    assert "Due: FP1 starts Fri 23 Oct 19:30." in err
    assert err.endswith("sent 1 message(s) for round 18 of 2026\n")

    # The next day's run sends nothing and does not even collect facts.
    assert main(["run"]) == 0
    assert len(recorder.requests) == 1
    assert pipeline.collected == 1
    assert "Already sent at Wed 21 Oct 10:00 (1 message(s))." in capsys.readouterr().err


def test_does_nothing_before_the_window(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pipeline, recorder = setup
    monkeypatch.setattr(cli, "_now", lambda: TOO_EARLY)

    assert main(["run"]) == 0
    assert (pipeline.collected, len(recorder.requests)) == (0, 0)
    assert record() is None
    assert "Too early: the window opens Wed 21 Oct 00:00." in capsys.readouterr().err


def test_days_before_widens_the_window(setup, monkeypatch: pytest.MonkeyPatch) -> None:
    _, recorder = setup
    monkeypatch.setattr(cli, "_now", lambda: TOO_EARLY)

    assert main(["run", "--days-before", "7"]) == 0
    assert len(recorder.requests) == 1
    assert main(["run", "--days-before", "-1"]) == 2


def test_dry_run_prints_without_settings_and_records_nothing(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, recorder = setup
    monkeypatch.delenv(ts.TOKEN_VAR)
    monkeypatch.delenv(ts.CHAT_ID_VAR)

    assert main(["run", "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("--- message 1/1, ")
    assert captured.err.endswith("dry run: 1 message(s) not sent, nothing recorded\n")
    assert recorder.requests == []
    assert record() is None


def test_missing_settings_fail_before_claude_runs(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pipeline, recorder = setup
    monkeypatch.delenv(ts.TOKEN_VAR)

    assert main(["run"]) == 1
    assert (pipeline.collected, pipeline.generated, len(recorder.requests)) == (0, 0, 0)
    assert record() is None
    assert capsys.readouterr().err.endswith(
        "error: TELEGRAM_BOT_TOKEN not set (in the environment or .env)\n"
    )


def test_facts_about_another_round_are_not_sent(setup, capsys: pytest.CaptureFixture[str]) -> None:
    pipeline, recorder = setup
    pipeline.document = facts_about(2026, 17)

    assert main(["run"]) == 1
    assert (pipeline.generated, len(recorder.requests)) == (0, 0)
    assert record() is None
    assert "error: the facts are not about round 18 of 2026" in capsys.readouterr().err


def test_a_generation_failure_records_nothing(setup, monkeypatch: pytest.MonkeyPatch) -> None:
    _, recorder = setup

    def fail(document: dict[str, Any], model: digest.ClaudeCli) -> str:
        raise digest.GenerationError("claude timed out after 600s")

    monkeypatch.setattr(digest, "generate", fail)
    assert main(["run"]) == 1
    assert recorder.requests == []
    assert record() is None


def test_a_refused_first_message_can_be_retried(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    refused = Recorder(httpx.Response(400, json=PARSE_ERROR))
    monkeypatch.setattr(cli, "_telegram_client", refused.client)

    assert main(["run"]) == 1
    assert record() is None
    err = capsys.readouterr().err
    assert "nothing was delivered; the next run will try again" in err
    assert TOKEN not in err

    _, recorder = setup
    monkeypatch.setattr(cli, "_telegram_client", recorder.client)
    assert main(["run"]) == 0
    assert len(recorder.requests) == 1


def test_a_failure_after_the_first_message_is_never_resent(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pipeline, _ = setup
    # Two messages: the first one goes through, the second one is refused.
    pipeline.text = "1. Раунд\n" + "Остин. " * 700
    recorder = Recorder(httpx.Response(200, json=OK), httpx.Response(400, json=PARSE_ERROR))
    monkeypatch.setattr(cli, "_telegram_client", recorder.client)
    monkeypatch.setattr(
        ts,
        "send",
        lambda messages, credentials, client: ts.Sender(
            credentials, client, sleep=lambda _: None
        ).send(messages),
    )

    assert main(["run"]) == 1
    assert len(recorder.requests) == 2
    saved = record()
    assert saved is not None and (saved.status, saved.messages) == (Status.PARTIAL, 1)
    err = capsys.readouterr().err
    assert "error: message 2 of 2 not sent (1 sent before it)" in err
    assert "`f1brief forget --season 2026 --round 18`" in err

    assert main(["run"]) == 0
    assert len(recorder.requests) == 2
    assert "Only partly sent at Wed 21 Oct 10:00" in capsys.readouterr().err


def test_a_timeout_may_have_delivered_so_it_is_not_resent(
    setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout() -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    recorder = Recorder(timeout)
    monkeypatch.setattr(cli, "_telegram_client", recorder.client)

    assert main(["run"]) == 1
    saved = record()
    assert saved is not None and (saved.status, saved.messages) == (Status.PARTIAL, 0)


def test_a_server_error_may_have_delivered_so_it_is_not_resent(
    setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(httpx.Response(502, text="<html>Bad Gateway</html>"))
    monkeypatch.setattr(cli, "_telegram_client", recorder.client)

    assert main(["run"]) == 1
    saved = record()
    assert saved is not None and (saved.status, saved.messages) == (Status.PARTIAL, 0)

    assert main(["run"]) == 0
    assert len(recorder.requests) == 1


def test_a_round_claimed_by_another_run_is_not_sent(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, recorder = setup

    def generate_while_another_run_sends(document: dict[str, Any], model: Any) -> str:
        with SendLog("state.db") as other:
            assert other.claim(2026, 18, IN_WINDOW)
        return TWO_PARTS

    monkeypatch.setattr(digest, "generate", generate_while_another_run_sends)

    assert main(["run"]) == 0
    assert recorder.requests == []
    assert capsys.readouterr().err.endswith("round 18 of 2026 is already sent or being sent\n")
    saved = record()
    assert saved is not None and saved.status is Status.SENDING


def test_a_custom_state_file(setup, tmp_path: Path) -> None:
    path = tmp_path / "elsewhere.db"
    assert main(["run", "--state", str(path)]) == 0
    assert record(path=str(path)) is not None
    assert not (tmp_path / "state.db").exists()


def test_the_digest_covers_the_stories_in_the_state_file(setup, tmp_path: Path) -> None:
    pipeline, _ = setup
    path = tmp_path / "elsewhere.db"
    with SendLog(path) as log:
        log.claim(2026, 17, datetime(2026, 10, 7, 8, tzinfo=UTC))
        log.mark_sent(2026, 17, datetime(2026, 10, 7, 8, tzinfo=UTC), 2)

    assert main(["run", "--state", str(path)]) == 0
    (stories,) = pipeline.stories
    assert stories["since"] == "2026-10-07T10:00:00+02:00"
    assert stories["since_last_digest"] is True
    assert stories["followed"] == []


def test_an_unusable_state_file_is_an_error(
    setup, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, recorder = setup
    assert main(["run", "--state", str(tmp_path / "missing" / "state.db")]) == 1
    assert recorder.requests == []
    assert "error: cannot open" in capsys.readouterr().err


def test_a_calendar_failure_is_an_error(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> list:
        raise schedule.ScheduleError("network down")

    monkeypatch.setattr(schedule, "fetch_schedule", boom)
    assert main(["run"]) == 1
    assert main(["due"]) == 1
    assert capsys.readouterr().err == "error: network down\nerror: network down\n"


def test_due_shows_the_decision(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pipeline, recorder = setup
    monkeypatch.setattr(cli, "_now", lambda: TOO_EARLY)

    assert main(["due"]) == 0
    assert capsys.readouterr().out == (
        "Round 18: United States Grand Prix (2026)\n"
        "Send window: Wed 21 Oct 00:00 - Fri 23 Oct 19:30 (Europe/Warsaw)\n"
        "Too early: the window opens Wed 21 Oct 00:00.\n"
    )
    assert main(["due", "--days-before", "7"]) == 0
    assert capsys.readouterr().out.endswith("Due: FP1 starts Fri 23 Oct 19:30.\n")
    assert (pipeline.collected, len(recorder.requests)) == (0, 0)
    assert main(["due", "--days-before", "-1"]) == 2


def test_forget_allows_another_send(setup, capsys: pytest.CaptureFixture[str]) -> None:
    _, recorder = setup
    assert main(["run"]) == 0

    assert main(["forget", "--season", "2026", "--round", "18"]) == 0
    assert capsys.readouterr().out.endswith("forgot round 18 of 2026\n")
    assert main(["forget", "--season", "2026", "--round", "18"]) == 0
    assert capsys.readouterr().out == "round 18 of 2026 was not in state.db\n"

    assert main(["run"]) == 0
    assert len(recorder.requests) == 2

"""The daily job after qualifying (F19), the `quali` command and `forget --qualifying`.
Telegram, Jolpica and `claude -p` are all stubs; the calendar is the recorded 2026 one."""

# The message is Russian: Cyrillic letters that look like Latin ones are intended here.
# ruff: noqa: RUF001
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import load_fixture
from test_digest import facts_of
from test_telegram_send import CHAT_ID, PARSE_ERROR, TOKEN, Recorder

from f1brief import after_qualifying, cli, digest, form, schedule
from f1brief import telegram_send as ts
from f1brief.cli import main
from f1brief.send_log import SendLog, Status

AFTER_QUALI = datetime(2026, 10, 25, 7, 0, tzinfo=UTC)  # Sun morning in Austin's window
DURING_FP = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
MESSAGE = "1. Квалификация\nРассел — P6.\n\n2. Что это значит для гонки\nСтарт в первой пятёрке."


class Stubs:
    """The classification, the facts document and the text, counting the calls."""

    def __init__(self, results: list[form.QualifyingResult]) -> None:
        self.results = results
        self.error: Exception | None = None
        self.collected: list[tuple[Any, ...]] = []
        self.generated: list[dict[str, Any]] = []
        self.text = MESSAGE

    def fetch_classification(self, race: schedule.Race, *args: Any) -> list:
        if self.error is not None:
            raise self.error
        return self.results

    def collect(self, race, results, now, favourite_drivers, favourite_teams) -> dict[str, Any]:
        self.collected.append((race.round, len(results), favourite_drivers, favourite_teams))
        return {"kind": "after_qualifying", "sections": {}, "missing": []}

    def generate(self, document: dict[str, Any], model: digest.ClaudeCli) -> str:
        assert model.system_prompt == digest.QUALIFYING_SYSTEM_PROMPT
        self.generated.append(document)
        return self.text


@pytest.fixture
def results(schedule_2026: dict[str, Any]) -> list[form.QualifyingResult]:
    """Recorded Singapore qualifying (2026 round 17), standing in for any round's."""
    singapore = next(r for r in schedule.parse_schedule(schedule_2026) if r.round == 17)
    return after_qualifying.parse_classification(
        load_fixture("jolpica/qualifying_2026_r17.json"), singapore
    )


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, schedule_2026, results):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ts.TOKEN_VAR, TOKEN)
    monkeypatch.setenv(ts.CHAT_ID_VAR, CHAT_ID)
    races = schedule.parse_schedule(schedule_2026)
    monkeypatch.setattr(schedule, "fetch_schedule", lambda season="current": races)
    monkeypatch.setattr(cli, "_now", lambda: AFTER_QUALI)
    stubs = Stubs(results)
    monkeypatch.setattr(after_qualifying, "fetch_classification", stubs.fetch_classification)
    monkeypatch.setattr(after_qualifying, "collect", stubs.collect)
    monkeypatch.setattr(digest, "generate_after_qualifying", stubs.generate)
    recorder = Recorder()
    monkeypatch.setattr(cli, "_telegram_client", recorder.client)
    return stubs, recorder


def record():
    with SendLog("state.db") as log:
        return log.get_qualifying(2026, 18)


def test_sends_once_after_qualifying(setup, capsys: pytest.CaptureFixture[str]) -> None:
    stubs, recorder = setup

    assert main(["run"]) == 0
    assert len(recorder.requests) == 1
    assert recorder.texts()[0].startswith("*1\\. Квалификация*")
    saved = record()
    assert saved is not None and (saved.status, saved.messages) == (Status.SENT, 1)
    err = capsys.readouterr().err
    assert "After qualifying (round 18 of 2026): due: the race starts Sun 25 Oct 21:00." in err
    assert err.endswith("sent 1 message(s) for the message after qualifying for round 18 of 2026\n")
    # The favourites come from the collectors' lists, passed as arguments.
    assert stubs.collected == [(18, 22, ("russell", "ocon"), ("mclaren",))]
    # The round's digest is not touched.
    with SendLog("state.db") as log:
        assert log.get(2026, 18) is None

    assert main(["run"]) == 0
    assert len(recorder.requests) == 1
    assert len(stubs.collected) == 1
    assert "After qualifying (round 18 of 2026): already sent at" in capsys.readouterr().err


def test_waits_while_the_classification_is_not_published(
    setup, capsys: pytest.CaptureFixture[str]
) -> None:
    stubs, recorder = setup
    stubs.results = []

    assert main(["run"]) == 0
    assert (len(stubs.collected), len(recorder.requests)) == (0, 0)
    assert record() is None
    assert capsys.readouterr().err.endswith(
        "the qualifying classification for United States Grand Prix is not published yet\n"
    )


def test_a_jolpica_failure_is_an_error_and_claims_nothing(
    setup, capsys: pytest.CaptureFixture[str]
) -> None:
    stubs, recorder = setup
    stubs.error = form.FormError("failed to fetch: 503")

    assert main(["run"]) == 1
    assert (len(stubs.collected), len(recorder.requests)) == (0, 0)
    assert record() is None
    assert capsys.readouterr().err.endswith("error: qualifying: failed to fetch: 503\n")


def test_nothing_during_practice(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stubs, recorder = setup
    monkeypatch.setattr(cli, "_now", lambda: DURING_FP)

    assert main(["run"]) == 0
    assert (len(stubs.collected), len(recorder.requests)) == (0, 0)
    assert "After qualifying (round 18 of 2026): not yet: due from Sun 25 Oct 01:00." in (
        capsys.readouterr().err
    )


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


def test_missing_settings_fail_before_anything_is_fetched(
    setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    stubs, recorder = setup
    monkeypatch.delenv(ts.TOKEN_VAR)
    stubs.error = AssertionError("must not fetch")

    assert main(["run"]) == 1
    assert (len(stubs.generated), len(recorder.requests)) == (0, 0)


def test_a_generation_failure_records_nothing(setup, monkeypatch: pytest.MonkeyPatch) -> None:
    _, recorder = setup

    def fail(document: dict[str, Any], model: Any) -> str:
        raise digest.GenerationError("claude timed out after 600s")

    monkeypatch.setattr(digest, "generate_after_qualifying", fail)
    assert main(["run"]) == 1
    assert recorder.requests == []
    assert record() is None


def test_a_refused_message_can_be_retried(setup, monkeypatch: pytest.MonkeyPatch) -> None:
    refused = Recorder(httpx.Response(400, json=PARSE_ERROR))
    monkeypatch.setattr(cli, "_telegram_client", refused.client)
    assert main(["run"]) == 1
    assert record() is None

    _, recorder = setup
    monkeypatch.setattr(cli, "_telegram_client", recorder.client)
    assert main(["run"]) == 0
    assert len(recorder.requests) == 1


def test_a_server_error_names_the_qualifying_forget_command(
    setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = Recorder(httpx.Response(502, text="<html>Bad Gateway</html>"))
    monkeypatch.setattr(cli, "_telegram_client", recorder.client)

    assert main(["run"]) == 1
    saved = record()
    assert saved is not None and (saved.status, saved.messages) == (Status.PARTIAL, 0)
    assert "`f1brief forget --season 2026 --round 18 --qualifying`" in capsys.readouterr().err

    assert main(["run"]) == 0
    assert len(recorder.requests) == 1
    assert main(["forget", "--season", "2026", "--round", "18", "--qualifying"]) == 0
    assert capsys.readouterr().out.endswith(
        "forgot the message after qualifying for round 18 of 2026\n"
    )
    assert record() is None


def test_forget_without_qualifying_leaves_the_message(setup) -> None:
    assert main(["run"]) == 0
    assert main(["forget", "--season", "2026", "--round", "18"]) == 0
    assert record() is not None


def test_forget_qualifying_needs_a_round(setup) -> None:
    assert main(["forget", "--month", "2026-01", "--qualifying"]) == 2
    assert main(["forget", "--qualifying"]) == 2


def test_due_shows_the_qualifying_decision_during_the_weekend(
    setup, capsys: pytest.CaptureFixture[str]
) -> None:
    stubs, recorder = setup

    assert main(["due"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Round 19: ")  # the digest looks at the next round already
    assert out.endswith(
        "After qualifying (round 18 of 2026): due: the race starts Sun 25 Oct 21:00.\n"
    )
    assert (len(stubs.collected), len(recorder.requests)) == (0, 0)


def test_quali_writes_the_prompt_for_a_given_round(
    setup, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stubs, recorder = setup
    document = {"kind": "after_qualifying", "sections": {"qualifying": {}}, "missing": []}
    monkeypatch.setattr(after_qualifying, "collect", lambda *args: document)

    args = ["quali", "--round", "17", "--driver", "bearman", "--team", "haas", "--prompt-only"]
    assert main(args) == 0
    prompt = capsys.readouterr().out
    assert facts_of(prompt) == document
    assert prompt.endswith(digest.template(digest.QUALIFYING_TEMPLATE).split("</facts>")[1])
    assert (stubs.generated, recorder.requests) == ([], [])


def test_quali_passes_the_favourites_and_writes_the_text(setup, tmp_path: Path) -> None:
    stubs, recorder = setup
    output = tmp_path / "quali.txt"

    args = ["quali", "--driver", "bearman", "--driver", "ocon", "--team", "haas"]
    assert main([*args, "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == MESSAGE + "\n"
    assert stubs.collected == [(18, 22, ["bearman", "ocon"], ["haas"])]
    assert recorder.requests == []
    assert record() is None


def test_quali_for_an_unknown_round_is_an_error(setup, capsys) -> None:
    assert main(["quali", "--round", "99"]) == 1
    assert capsys.readouterr().err == "error: no such race in season current\n"


def test_quali_before_the_classification_is_published(setup) -> None:
    stubs, _ = setup
    stubs.results = []
    assert main(["quali"]) == 1
    assert stubs.collected == []

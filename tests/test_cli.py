import pytest

from f1brief import __version__
from f1brief.cli import main


def test_main_without_arguments_succeeds() -> None:
    assert main([]) == 0


def test_version_flag_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"f1brief {__version__}"


def test_unknown_argument_fails() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--no-such-flag"])

    assert exc.value.code == 2


def test_next_prints_upcoming_weekend(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    schedule_2026: dict,
) -> None:
    from datetime import UTC, datetime

    from f1brief import cli, schedule

    races = schedule.parse_schedule(schedule_2026)
    monkeypatch.setattr(schedule, "fetch_schedule", lambda season: races)
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 10, 15, tzinfo=UTC))

    assert main(["next"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Round 18: United States Grand Prix (2026)")


def test_next_reports_fetch_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from f1brief import schedule

    def boom(season: str) -> list:
        raise schedule.ScheduleError("network down")

    monkeypatch.setattr(schedule, "fetch_schedule", boom)

    assert main(["next"]) == 1
    assert capsys.readouterr().err == "error: network down\n"

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


def test_standings_prints_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    driver_standings_r16: dict,
    constructor_standings_r16: dict,
    driver_standings_r17: dict,
    constructor_standings_r17: dict,
) -> None:
    from f1brief import standings

    rep = standings.report(
        standings.combine(driver_standings_r17, constructor_standings_r17),
        standings.combine(driver_standings_r16, constructor_standings_r16),
    )
    monkeypatch.setattr(standings, "fetch_report", lambda season: rep)

    assert main(["standings"]) == 0
    assert capsys.readouterr().out.startswith("Standings after round 17 (2026)")


def test_standings_reports_fetch_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from f1brief import standings

    def boom(season: str) -> None:
        raise standings.StandingsError("network down")

    monkeypatch.setattr(standings, "fetch_report", boom)

    assert main(["standings"]) == 1
    assert capsys.readouterr().err == "error: network down\n"


def test_form_prints_team_form(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mclaren_2026: tuple
) -> None:
    from f1brief import form

    rounds = form.build_rounds(*mclaren_2026)
    team_form = form.TeamForm("mclaren", "McLaren", tuple(rounds[-3:]))
    calls: list[tuple] = []

    def fake_fetch(team: str, last: int, season: str) -> form.TeamForm:
        calls.append((team, last, season))
        return team_form

    monkeypatch.setattr(form, "fetch_team_form", fake_fetch)

    assert main(["form", "--last", "3"]) == 0
    assert calls == [("mclaren", 3, "current")]
    assert capsys.readouterr().out.startswith("McLaren form, last 3 rounds")


def test_form_rejects_non_positive_last(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["form", "--last", "0"]) == 2
    assert "at least 1" in capsys.readouterr().err


def test_form_reports_fetch_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from f1brief import form

    def boom(team: str, last: int, season: str) -> None:
        raise form.FormError("network down")

    monkeypatch.setattr(form, "fetch_team_form", boom)

    assert main(["form"]) == 1
    assert capsys.readouterr().err == "error: network down\n"


def test_drivers_defaults_to_favourites(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from f1brief import drivers

    calls: list[tuple] = []

    def fake_fetch(driver_id: str, last: int, season: str) -> drivers.DriverForm:
        calls.append((driver_id, last, season))
        return drivers.DriverForm(driver_id, driver_id.title(), ())

    monkeypatch.setattr(drivers, "fetch_driver_form", fake_fetch)

    assert main(["drivers"]) == 0
    assert calls == [("russell", 5, "current"), ("ocon", 5, "current")]
    assert capsys.readouterr().out == (
        "Russell: no races in the selected period.\n\nOcon: no races in the selected period.\n"
    )


def test_drivers_accepts_repeated_driver_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from f1brief import drivers

    calls: list[str] = []
    monkeypatch.setattr(
        drivers,
        "fetch_driver_form",
        lambda driver_id, last, season: (
            calls.append(driver_id) or drivers.DriverForm(driver_id, "X", ())
        ),
    )

    assert main(["drivers", "--driver", "lawson", "--driver", "norris", "--last", "3"]) == 0
    assert calls == ["lawson", "norris"]


def test_drivers_reports_fetch_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from f1brief import drivers, form

    def boom(driver_id: str, last: int, season: str) -> None:
        raise form.FormError("network down")

    monkeypatch.setattr(drivers, "fetch_driver_form", boom)

    assert main(["drivers"]) == 1
    assert capsys.readouterr().err == "error: network down\n"


def test_pace_prints_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, datetime

    from test_pace import singapore_report

    from f1brief import cli, pace

    report = singapore_report()
    seen: list[datetime] = []
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 10, 15, tzinfo=UTC))
    monkeypatch.setattr(pace, "fetch_report", lambda now: seen.append(now) or report)

    assert main(["pace"]) == 0
    assert seen == [datetime(2026, 10, 15, tzinfo=UTC)]
    assert capsys.readouterr().out.startswith("Long-run pace, Singapore (Marina Bay)")


def test_pace_reports_fetch_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from f1brief import pace

    def boom(now: object) -> None:
        raise pace.PaceError("network down")

    monkeypatch.setattr(pace, "fetch_report", boom)

    assert main(["pace"]) == 1
    assert capsys.readouterr().err == "error: network down\n"

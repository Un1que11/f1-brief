from pathlib import Path

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
    # Every favourite team in the config, in its order.
    assert calls == [("mclaren", 3, "current"), ("haas", 3, "current")]
    assert capsys.readouterr().out.startswith("McLaren form, last 3 rounds")

    calls.clear()
    assert main(["form", "--team", "ferrari", "--last", "3"]) == 0
    assert calls == [("ferrari", 3, "current")]


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
    assert calls == [("russell", 5, "current"), ("ocon", 5, "current"), ("bearman", 5, "current")]
    assert capsys.readouterr().out == (
        "Russell: no races in the selected period.\n\nOcon: no races in the selected period."
        "\n\nBearman: no races in the selected period.\n"
    )


def test_the_favourites_come_from_the_config_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from f1brief import drivers, form

    path = tmp_path / "config.toml"
    path.write_text(
        '[[favourites.teams]]\nid = "williams"\nname = "Williams"\n'
        '[[favourites.drivers]]\nid = "albon"\nname = "Alexander Albon"\n',
        encoding="utf-8",
    )
    teams: list[str] = []
    driver_ids: list[str] = []
    monkeypatch.setattr(
        form,
        "fetch_team_form",
        lambda team, last, season: teams.append(team) or form.TeamForm(team, team, ()),
    )
    monkeypatch.setattr(
        drivers,
        "fetch_driver_form",
        lambda d, last, season: driver_ids.append(d) or drivers.DriverForm(d, d, ()),
    )

    assert main(["--config", str(path), "form"]) == 0
    assert main(["--config", str(path), "drivers"]) == 0
    assert (teams, driver_ids) == (["williams"], ["albon"])


def test_no_favourites_in_the_config_asks_for_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[favourites]\nteams = []\ndrivers = []\n", encoding="utf-8")

    assert main(["--config", str(path), "form"]) == 2
    assert main(["--config", str(path), "drivers"]) == 2
    err = capsys.readouterr().err
    assert "pass --team" in err
    assert "pass --driver" in err


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
    monkeypatch.setattr(pace, "fetch_report", lambda now, favourites: seen.append(now) or report)

    assert main(["pace"]) == 0
    assert seen == [datetime(2026, 10, 15, tzinfo=UTC)]
    assert capsys.readouterr().out.startswith("Long-run pace, Singapore (Marina Bay)")


def test_pace_reports_fetch_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from f1brief import pace

    def boom(now: object, favourites: object) -> None:
        raise pace.PaceError("network down")

    monkeypatch.setattr(pace, "fetch_report", boom)

    assert main(["pace"]) == 1
    assert capsys.readouterr().err == "error: network down\n"


def test_track_defaults_to_next_race_circuit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], schedule_2026: dict
) -> None:
    from datetime import UTC, date, datetime

    from test_track_history import austin

    from f1brief import cli, schedule, track_history

    calls: list[tuple] = []
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 10, 15, tzinfo=UTC))
    monkeypatch.setattr(schedule, "fetch_schedule", lambda: schedule.parse_schedule(schedule_2026))
    monkeypatch.setattr(
        track_history,
        "fetch_track_history",
        lambda circuit, before, favourites, count: (
            calls.append((circuit, before, count)) or austin()
        ),
    )

    assert main(["track"]) == 0
    # Next race is Austin (R18, 25 Oct): editions before that day.
    assert calls == [("americas", date(2026, 10, 25), 3)]
    assert capsys.readouterr().out.startswith("Favourites at Circuit of the Americas")


def test_track_with_explicit_circuit_uses_today(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, date, datetime

    from test_track_history import austin

    from f1brief import cli, track_history

    calls: list[tuple] = []
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 10, 15, tzinfo=UTC))
    monkeypatch.setattr(
        track_history,
        "fetch_track_history",
        lambda circuit, before, favourites, count: (
            calls.append((circuit, before, count)) or austin()
        ),
    )

    assert main(["track", "--circuit", "monaco", "--editions", "2"]) == 0
    assert calls == [("monaco", date(2026, 10, 15), 2)]


def test_track_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from f1brief import form, track_history

    assert main(["track", "--circuit", "x", "--editions", "0"]) == 2

    def boom(circuit: str, before: object, favourites: object, count: int) -> None:
        raise form.FormError("network down")

    monkeypatch.setattr(track_history, "fetch_track_history", boom)
    assert main(["track", "--circuit", "x"]) == 1
    assert capsys.readouterr().err.endswith("error: network down\n")


def test_news_uses_window_and_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, datetime

    from f1brief import cli, news

    seen: list[datetime] = []
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 10, 15, tzinfo=UTC))
    monkeypatch.setattr(
        news,
        "fetch_news",
        lambda since, favourites: (
            seen.append(since) or news.NewsReport(since=since, items=(), failed_feeds=())
        ),
    )

    assert main(["news", "--days", "3"]) == 0
    assert seen == [datetime(2026, 10, 12, tzinfo=UTC)]
    assert capsys.readouterr().out.startswith("F1 news since 12 Oct 00:00 UTC")


def test_news_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from f1brief import news

    assert main(["news", "--days", "0"]) == 2

    def boom(since: object, favourites: object) -> None:
        raise news.NewsError("no feed could be read")

    monkeypatch.setattr(news, "fetch_news", boom)
    assert main(["news"]) == 1
    assert capsys.readouterr().err.endswith("error: no feed could be read\n")


def test_weather_for_next_race(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], schedule_2026: dict
) -> None:
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from f1brief import cli, schedule, weather

    payload = json.loads(
        (Path(__file__).parent / "fixtures/openmeteo/austin_2026-10-23_25.json").read_text()
    )
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 10, 15, tzinfo=UTC))
    monkeypatch.setattr(schedule, "fetch_schedule", lambda: schedule.parse_schedule(schedule_2026))
    monkeypatch.setattr(
        weather, "fetch_forecast", lambda race: weather.parse_forecast(payload, race)
    )

    assert main(["weather"]) == 0
    assert capsys.readouterr().out.startswith("Weather for United States Grand Prix")


def test_weather_not_available_yet_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], schedule_2026: dict
) -> None:
    from datetime import UTC, datetime

    from f1brief import cli, schedule, weather

    def too_early(race: object) -> None:
        raise weather.ForecastNotAvailable("out of allowed range")

    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 10, 15, tzinfo=UTC))
    monkeypatch.setattr(schedule, "fetch_schedule", lambda: schedule.parse_schedule(schedule_2026))
    monkeypatch.setattr(weather, "fetch_forecast", too_early)

    assert main(["weather"]) == 0
    assert "No forecast yet for United States Grand Prix" in capsys.readouterr().out

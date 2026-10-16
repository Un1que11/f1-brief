"""Command-line entry point."""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from f1brief import (
    __version__,
    drivers,
    form,
    news,
    pace,
    schedule,
    standings,
    track_history,
    weather,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="f1brief",
        description="Build and send the F1 pre-race-weekend digest.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")

    next_cmd = commands.add_parser("next", help="show the next race weekend in Warsaw time")
    next_cmd.add_argument("--season", default="current", help="season year (default: current)")

    standings_cmd = commands.add_parser(
        "standings", help="show championship standings and changes since the previous round"
    )
    standings_cmd.add_argument("--season", default="current", help="season year (default: current)")

    form_cmd = commands.add_parser("form", help="show a team's recent race and qualifying form")
    form_cmd.add_argument("--team", default="mclaren", help="Jolpica constructor id")
    form_cmd.add_argument(
        "--last", type=int, default=form.DEFAULT_ROUNDS, help="number of rounds (default: 5)"
    )
    form_cmd.add_argument("--season", default="current", help="season year (default: current)")

    drivers_cmd = commands.add_parser(
        "drivers", help="show drivers' recent form compared with their teammates"
    )
    drivers_cmd.add_argument(
        "--driver",
        action="append",
        dest="driver_ids",
        metavar="ID",
        help="Jolpica driver id; repeatable (default: russell, ocon)",
    )
    drivers_cmd.add_argument(
        "--last", type=int, default=form.DEFAULT_ROUNDS, help="number of races (default: 5)"
    )
    drivers_cmd.add_argument("--season", default="current", help="season year (default: current)")

    commands.add_parser(
        "pace", help="show long-run race pace of the favourites vs the leaders (last race)"
    )

    track_cmd = commands.add_parser(
        "track", help="show the favourites' results at a circuit over its last editions"
    )
    track_cmd.add_argument(
        "--circuit", help="Jolpica circuit id (default: the next race's circuit)"
    )
    track_cmd.add_argument(
        "--editions",
        type=int,
        default=track_history.DEFAULT_EDITIONS,
        help="number of past races (default: 3)",
    )

    news_cmd = commands.add_parser("news", help="show recent news about the favourites")
    news_cmd.add_argument("--days", type=int, default=7, help="how far back (default: 7)")

    commands.add_parser("weather", help="show the weather forecast for the next race weekend")
    return parser


def _now() -> datetime:
    return datetime.now(UTC)


def _cmd_next(season: str) -> int:
    try:
        races = schedule.fetch_schedule(season)
    except schedule.ScheduleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    race = schedule.next_race(races, _now())
    if race is None:
        print(f"No upcoming races in season {season}.")
        return 0
    print(schedule.format_schedule(race))
    return 0


def _cmd_standings(season: str) -> int:
    try:
        rep = standings.fetch_report(season)
    except standings.StandingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(standings.format_report(rep))
    return 0


def _cmd_form(team: str, last: int, season: str) -> int:
    if last < 1:
        print("error: --last must be at least 1", file=sys.stderr)
        return 2
    try:
        team_form = form.fetch_team_form(team, last, season)
    except form.FormError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(form.format_team_form(team_form))
    return 0


def _cmd_drivers(driver_ids: list[str] | None, last: int, season: str) -> int:
    if last < 1:
        print("error: --last must be at least 1", file=sys.stderr)
        return 2
    blocks = []
    for driver_id in driver_ids or drivers.FAVOURITES:
        try:
            blocks.append(
                drivers.format_driver_form(drivers.fetch_driver_form(driver_id, last, season))
            )
        except form.FormError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print("\n\n".join(blocks))
    return 0


def _cmd_pace() -> int:
    try:
        report = pace.fetch_report(_now())
    except pace.PaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(pace.format_report(report))
    return 0


def _cmd_track(circuit_id: str | None, editions: int) -> int:
    if editions < 1:
        print("error: --editions must be at least 1", file=sys.stderr)
        return 2
    now = _now()
    before = now.date()
    try:
        if circuit_id is None:
            race = schedule.next_race(schedule.fetch_schedule(), now)
            if race is None:
                print("error: no upcoming race; pass --circuit", file=sys.stderr)
                return 1
            circuit_id, before = race.circuit.id, race.race.date
        history = track_history.fetch_track_history(circuit_id, before, editions)
    except (schedule.ScheduleError, form.FormError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(track_history.format_track_history(history))
    return 0


def _cmd_news(days: int) -> int:
    if days < 1:
        print("error: --days must be at least 1", file=sys.stderr)
        return 2
    try:
        report = news.fetch_news(news.default_since(_now(), days))
    except news.NewsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(news.format_report(report))
    return 0


def _cmd_weather() -> int:
    try:
        race = schedule.next_race(schedule.fetch_schedule(), _now())
        if race is None:
            print("No upcoming race.")
            return 0
        forecast = weather.fetch_forecast(race)
    except weather.ForecastNotAvailable as exc:
        print(f"No forecast yet for {race.name}: it is beyond the forecast range ({exc}).")
        return 0
    except (schedule.ScheduleError, weather.WeatherError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(weather.format_forecast(forecast))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "next":
        return _cmd_next(args.season)
    if args.command == "standings":
        return _cmd_standings(args.season)
    if args.command == "form":
        return _cmd_form(args.team, args.last, args.season)
    if args.command == "drivers":
        return _cmd_drivers(args.driver_ids, args.last, args.season)
    if args.command == "pace":
        return _cmd_pace()
    if args.command == "track":
        return _cmd_track(args.circuit, args.editions)
    if args.command == "news":
        return _cmd_news(args.days)
    if args.command == "weather":
        return _cmd_weather()
    return 0

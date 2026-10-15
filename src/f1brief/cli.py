"""Command-line entry point."""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from f1brief import __version__, drivers, form, pace, schedule, standings


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
    return 0

"""Command-line entry point."""

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from f1brief import (
    __version__,
    digest,
    drivers,
    facts,
    form,
    news,
    pace,
    schedule,
    standings,
    telegram_format,
    telegram_send,
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

    facts_cmd = commands.add_parser(
        "facts", help="collect every fact for the next race weekend as JSON with sources"
    )
    facts_cmd.add_argument("--output", metavar="PATH", help="write to a file instead of stdout")

    digest_cmd = commands.add_parser(
        "digest", help="write the digest text from the facts with headless Claude Code"
    )
    digest_cmd.add_argument(
        "--facts", metavar="PATH", help="a saved `facts` document (default: collect now)"
    )
    digest_cmd.add_argument("--output", metavar="PATH", help="write to a file instead of stdout")
    digest_cmd.add_argument("--model", help="model for claude -p (default: claude's own)")
    digest_cmd.add_argument(
        "--prompt-only", action="store_true", help="print the prompt instead of running claude"
    )

    format_cmd = commands.add_parser(
        "format", help="show a digest text as Telegram messages (MarkdownV2, split to fit)"
    )
    format_cmd.add_argument(
        "--input", metavar="PATH", help="a saved `digest` text (default: stdin)"
    )
    format_cmd.add_argument(
        "--limit",
        type=int,
        default=telegram_format.MAX_LENGTH,
        help=f"maximum message length (default: {telegram_format.MAX_LENGTH})",
    )

    send_cmd = commands.add_parser(
        "send", help="send a digest text to the Telegram chat (or print it with --dry-run)"
    )
    send_cmd.add_argument("--input", metavar="PATH", help="a saved `digest` text (default: stdin)")
    send_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="print the messages instead of sending them (needs no token)",
    )
    send_cmd.add_argument(
        "--env-file",
        default=telegram_send.DEFAULT_ENV_FILE,
        metavar="PATH",
        help=f"where to look for {telegram_send.TOKEN_VAR} and {telegram_send.CHAT_ID_VAR} "
        f"when they are not in the environment (default: {telegram_send.DEFAULT_ENV_FILE})",
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


def _collect_facts() -> dict | None:
    """The facts document, or None (after printing why) when no section could be collected."""
    document = facts.collect(_now())
    if not any(data is not None for data in document["sections"].values()):
        for gap in document["missing"]:
            print(f"error: {gap['section']}: {gap['reason']}", file=sys.stderr)
        return None
    return document


def _warn_missing(document: dict) -> None:
    for gap in document["missing"]:
        print(f"warning: no {gap['section']} facts: {gap['reason']}", file=sys.stderr)


def _write(text: str, output: str | None) -> None:
    if output:
        with open(output, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)


def _cmd_facts(output: str | None) -> int:
    document = _collect_facts()
    if document is None:
        return 1
    _write(json.dumps(document, ensure_ascii=False, indent=2) + "\n", output)
    _warn_missing(document)
    return 0


def _cmd_digest(
    facts_path: str | None, output: str | None, model: str | None, prompt_only: bool
) -> int:
    if facts_path:
        try:
            with open(facts_path, encoding="utf-8") as fh:
                document = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"error: cannot read {facts_path}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(document, dict) or not {"sections", "missing"} <= document.keys():
            print(f"error: {facts_path} is not a `facts` document", file=sys.stderr)
            return 1
    else:
        document = _collect_facts()
        if document is None:
            return 1
    if prompt_only:
        _write(digest.build_prompt(document), output)
        return 0
    _warn_missing(document)
    try:
        text = digest.generate(document, digest.ClaudeCli(model=model))
    except digest.GenerationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _write(text + "\n", output)
    return 0


def _read_text(input_path: str | None) -> str | None:
    """The text of `input_path`, or of stdin; None (after printing why) when unreadable."""
    try:
        if input_path:
            with open(input_path, encoding="utf-8") as fh:
                return fh.read()
        return sys.stdin.read()
    except OSError as exc:
        print(f"error: cannot read {input_path}: {exc}", file=sys.stderr)
        return None


def _messages(text: str, limit: int = telegram_format.MAX_LENGTH) -> list[str] | None:
    try:
        return telegram_format.messages(text, limit)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _print_messages(chunks: list[str]) -> None:
    for i, chunk in enumerate(chunks, 1):
        size = telegram_format.length(chunk)
        print(f"--- message {i}/{len(chunks)}, {size} chars ---")
        print(chunk)


def _cmd_format(input_path: str | None, limit: int) -> int:
    if limit < 2:
        print("error: --limit must be at least 2", file=sys.stderr)
        return 2
    text = _read_text(input_path)
    if text is None:
        return 1
    chunks = _messages(text, limit)
    if chunks is None:
        return 1
    _print_messages(chunks)
    return 0


def _telegram_client() -> httpx.Client:
    return httpx.Client(timeout=15.0)


def _cmd_send(input_path: str | None, dry_run: bool, env_file: str) -> int:
    text = _read_text(input_path)
    if text is None:
        return 1
    chunks = _messages(text)
    if chunks is None:
        return 1
    if dry_run:
        _print_messages(chunks)
        print(f"dry run: {len(chunks)} message(s) not sent", file=sys.stderr)
        return 0
    try:
        credentials = telegram_send.load_credentials(env_file=env_file)
        with _telegram_client() as client:
            telegram_send.send(chunks, credentials, client)
    except telegram_send.SendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"sent {len(chunks)} message(s)", file=sys.stderr)
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
    if args.command == "facts":
        return _cmd_facts(args.output)
    if args.command == "digest":
        return _cmd_digest(args.facts, args.output, args.model, args.prompt_only)
    if args.command == "format":
        return _cmd_format(args.input, args.limit)
    if args.command == "send":
        return _cmd_send(args.input, args.dry_run, args.env_file)
    return 0

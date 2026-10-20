"""Command-line entry point."""

import argparse
import contextlib
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from f1brief import (
    __version__,
    after_qualifying,
    alerts,
    archive_page,
    bot,
    config,
    digest,
    drivers,
    facts,
    form,
    launchd,
    lifecycle,
    news,
    news_archive,
    offseason,
    pace,
    schedule,
    send_log,
    send_window,
    standings,
    stories,
    story_detection,
    story_seeds,
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
    parser.add_argument(
        "--config",
        metavar="PATH",
        help=f"settings file (default: ./{config.DEFAULT_PATH}, built-in defaults if absent)",
    )
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("config", help="show the settings in effect and where they come from")

    next_cmd = commands.add_parser("next", help="show the next race weekend in Warsaw time")
    next_cmd.add_argument("--season", default="current", help="season year (default: current)")

    standings_cmd = commands.add_parser(
        "standings", help="show championship standings and changes since the previous round"
    )
    standings_cmd.add_argument("--season", default="current", help="season year (default: current)")

    form_cmd = commands.add_parser(
        "form", help="show the favourite teams' recent race and qualifying form"
    )
    form_cmd.add_argument(
        "--team",
        action="append",
        dest="team_ids",
        metavar="ID",
        help="Jolpica constructor id; repeatable (default: the favourite teams in the config)",
    )
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
        help="Jolpica driver id; repeatable (default: the favourite drivers in the config)",
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
    facts_cmd.add_argument(
        "--state",
        default=send_log.DEFAULT_STATE_FILE,
        metavar="PATH",
        help=f"SQLite file with the stories and sends (default: {send_log.DEFAULT_STATE_FILE})",
    )

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
    digest_cmd.add_argument(
        "--state",
        default=send_log.DEFAULT_STATE_FILE,
        metavar="PATH",
        help=f"SQLite file with the stories and sends (default: {send_log.DEFAULT_STATE_FILE})",
    )

    summary_cmd = commands.add_parser(
        "summary",
        help="write the off-season monthly summary now, whatever the date (it is not sent)",
    )
    summary_cmd.add_argument("--output", metavar="PATH", help="write to a file instead of stdout")
    summary_cmd.add_argument("--model", help="model for claude -p (default: claude's own)")
    summary_cmd.add_argument(
        "--prompt-only", action="store_true", help="print the prompt instead of running claude"
    )
    summary_cmd.add_argument(
        "--state",
        default=send_log.DEFAULT_STATE_FILE,
        metavar="PATH",
        help=f"SQLite file with the archived news (default: {send_log.DEFAULT_STATE_FILE})",
    )

    quali_cmd = commands.add_parser(
        "quali",
        help="write the message after qualifying for a round now, whatever the time (it is "
        "not sent)",
    )
    quali_cmd.add_argument("--season", default="current", help="season year (default: current)")
    quali_cmd.add_argument(
        "--round", type=int, help="round number (default: the next race that has not started)"
    )
    quali_cmd.add_argument(
        "--driver",
        action="append",
        dest="driver_ids",
        metavar="ID",
        help="favourite Jolpica driver id; repeatable (default: the favourite drivers in the "
        "config)",
    )
    quali_cmd.add_argument(
        "--team",
        action="append",
        dest="team_ids",
        metavar="ID",
        help="favourite Jolpica constructor id; repeatable (default: the favourite teams in the "
        "config)",
    )
    quali_cmd.add_argument("--output", metavar="PATH", help="write to a file instead of stdout")
    quali_cmd.add_argument("--model", help="model for claude -p (default: claude's own)")
    quali_cmd.add_argument(
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

    def add_state(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument(
            "--state",
            default=send_log.DEFAULT_STATE_FILE,
            metavar="PATH",
            help=f"SQLite file of sent rounds (default: {send_log.DEFAULT_STATE_FILE})",
        )

    def add_days_before(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument(
            "--days-before",
            type=int,
            metavar="N",
            help="open the send window N days before FP1, Warsaw time "
            "(default: digest.days_before_fp1 in the config file)",
        )

    due_cmd = commands.add_parser(
        "due",
        help="show whether it is time to send the next round's digest or its message after "
        "qualifying (or, in the off-season, the monthly summary)",
    )
    add_days_before(due_cmd)
    add_state(due_cmd)

    run_cmd = commands.add_parser(
        "run",
        help="the daily job: when the digest, the message after qualifying (or, in the "
        "off-season, the monthly summary) is due, write it and send it, once; then send the "
        "story alerts that are due",
    )
    add_days_before(run_cmd)
    add_state(run_cmd)
    run_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="print the messages instead of sending them; records nothing, needs no token",
    )
    run_cmd.add_argument("--model", help="model for claude -p (default: claude's own)")
    run_cmd.add_argument(
        "--env-file",
        default=telegram_send.DEFAULT_ENV_FILE,
        metavar="PATH",
        help=f"where to look for the Telegram settings (default: {telegram_send.DEFAULT_ENV_FILE})",
    )

    alerts_cmd = commands.add_parser(
        "alerts",
        help="send the alerts on followed stories that are due (none in the quiet hours "
        "23:00-08:00 Warsaw, at most 3 a day) and the offers to unfollow a followed story "
        "with no news for 60 days; `run` does this too",
    )
    alerts_cmd.add_argument(
        "--state",
        default=send_log.DEFAULT_STATE_FILE,
        metavar="PATH",
        help=f"SQLite file with the stories (default: {send_log.DEFAULT_STATE_FILE})",
    )
    alerts_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="print the alerts and offers that are due instead of sending them; records "
        "nothing, needs no token",
    )
    alerts_cmd.add_argument(
        "--env-file",
        default=telegram_send.DEFAULT_ENV_FILE,
        metavar="PATH",
        help=f"where to look for the Telegram settings (default: {telegram_send.DEFAULT_ENV_FILE})",
    )

    detect_cmd = commands.add_parser(
        "detect-stories",
        help="sort the new hot news items into stories (asks claude -p; sends nothing)",
    )
    detect_cmd.add_argument(
        "--days", type=int, default=7, help="look at news from the last N days (default: 7)"
    )
    detect_cmd.add_argument(
        "--state",
        default=send_log.DEFAULT_STATE_FILE,
        metavar="PATH",
        help=f"SQLite file with the stories (default: {send_log.DEFAULT_STATE_FILE})",
    )
    detect_cmd.add_argument("--model", help="model for claude -p (default: claude's own)")
    detect_cmd.add_argument(
        "--prompt-only", action="store_true", help="print the prompt instead of running claude"
    )
    detect_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="run claude and show what it decided, but store nothing",
    )

    seed_cmd = commands.add_parser(
        "seed-stories",
        help="send the articles of the seeds file that no story has yet through story "
        "detection and follow the stories they start (asks claude -p; sends nothing)",
    )
    seed_cmd.add_argument(
        "--seeds",
        default=str(story_seeds.DEFAULT_PATH),
        metavar="PATH",
        help=f"TOML file with the seed articles (default: {story_seeds.DEFAULT_PATH})",
    )
    seed_cmd.add_argument(
        "--state",
        default=send_log.DEFAULT_STATE_FILE,
        metavar="PATH",
        help=f"SQLite file with the stories (default: {send_log.DEFAULT_STATE_FILE})",
    )
    seed_cmd.add_argument("--model", help="model for claude -p (default: claude's own)")
    seed_cmd.add_argument(
        "--prompt-only", action="store_true", help="print the prompt instead of running claude"
    )
    seed_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="run claude and show what it decided, but store and follow nothing",
    )

    bot_cmd = commands.add_parser(
        "bot",
        help="the Telegram bot for hot stories: answers /stories, /follow, ... and the story "
        "buttons, only in the configured chat (long polling; runs until stopped)",
    )
    bot_cmd.add_argument(
        "--state",
        default=send_log.DEFAULT_STATE_FILE,
        metavar="PATH",
        help=f"SQLite file with the stories (default: {send_log.DEFAULT_STATE_FILE})",
    )
    bot_cmd.add_argument(
        "--env-file",
        default=telegram_send.DEFAULT_ENV_FILE,
        metavar="PATH",
        help=f"where to look for the Telegram settings (default: {telegram_send.DEFAULT_ENV_FILE})",
    )
    bot_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="answer the updates in --updates and print the replies: nothing is sent, stored "
        "or written to the config, and no token is needed",
    )
    bot_cmd.add_argument(
        "--updates",
        metavar="PATH",
        help="a saved getUpdates answer (or a JSON list of updates), for --dry-run",
    )
    bot_cmd.add_argument(
        "--chat-id",
        metavar="ID",
        help=f"with --dry-run: the chat to accept (default: {telegram_send.CHAT_ID_VAR})",
    )

    archive_cmd = commands.add_parser(
        "archive-page",
        help="write the story archive as a static HTML page (timelines with sources; filters "
        "followed / all / resolved); the bot sends the same page with its /archive reply",
    )
    archive_cmd.add_argument(
        "--output",
        default=archive_page.DEFAULT_OUTPUT,
        metavar="PATH",
        help=f"where to write the page, '-' for stdout (default: {archive_page.DEFAULT_OUTPUT})",
    )
    archive_cmd.add_argument(
        "--state",
        default=send_log.DEFAULT_STATE_FILE,
        metavar="PATH",
        help=f"SQLite file with the stories (default: {send_log.DEFAULT_STATE_FILE})",
    )

    forget_cmd = commands.add_parser(
        "forget",
        help="drop a round (its message after qualifying, or an off-season month) from the "
        "state file, so that `run` may send it again",
    )
    forget_cmd.add_argument("--season", type=int)
    forget_cmd.add_argument("--round", type=int)
    forget_cmd.add_argument(
        "--qualifying",
        action="store_true",
        help="the round's message after qualifying instead of its digest",
    )
    forget_cmd.add_argument("--month", metavar="YYYY-MM", help="an off-season month's summary")
    add_state(forget_cmd)

    launchd_cmd = commands.add_parser(
        "launchd",
        help="print (or write) the macOS LaunchAgent that runs `f1brief run` every hour",
    )
    launchd_cmd.add_argument(
        "--output",
        metavar="PATH",
        help="write the plist here (e.g. ~/Library/LaunchAgents/"
        f"{launchd.PLIST_NAME}) and create the log directory; default: print it",
    )
    launchd_cmd.add_argument(
        "--workdir",
        metavar="DIR",
        default=".",
        help="the f1-brief checkout, with config.toml, .env and state.db (default: .)",
    )
    launchd_cmd.add_argument(
        "--interval",
        type=int,
        default=launchd.DEFAULT_INTERVAL_MINUTES,
        metavar="MINUTES",
        help=f"how often launchd starts `run` (default: {launchd.DEFAULT_INTERVAL_MINUTES})",
    )
    launchd_cmd.add_argument(
        "--log-dir",
        metavar="DIR",
        help="where the job's output goes (default: ~/Library/Logs/f1brief)",
    )
    launchd_cmd.add_argument(
        "--uv", metavar="PATH", help="full path of uv (default: found on PATH)"
    )
    launchd_cmd.add_argument(
        "--claude", metavar="PATH", help="full path of claude (default: found on PATH)"
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


def _cmd_form(team_ids: Sequence[str], last: int, season: str) -> int:
    if last < 1:
        print("error: --last must be at least 1", file=sys.stderr)
        return 2
    if not team_ids:
        print("error: no favourite teams in the config; pass --team", file=sys.stderr)
        return 2
    try:
        forms = [form.fetch_team_form(team, last, season) for team in team_ids]
    except form.FormError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("\n\n".join(form.format_team_form(f) for f in forms))
    return 0


def _cmd_drivers(driver_ids: Sequence[str], last: int, season: str) -> int:
    if last < 1:
        print("error: --last must be at least 1", file=sys.stderr)
        return 2
    if not driver_ids:
        print("error: no favourite drivers in the config; pass --driver", file=sys.stderr)
        return 2
    try:
        forms = [drivers.fetch_driver_form(d, last, season) for d in driver_ids]
    except form.FormError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    blocks = [drivers.format_driver_form(f) for f in drivers.share_head_to_heads(forms)]
    print("\n\n".join(blocks))
    return 0


def _cmd_pace(favourites: config.Favourites) -> int:
    try:
        report = pace.fetch_report(_now(), favourites)
    except pace.PaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(pace.format_report(report))
    return 0


def _cmd_track(circuit_id: str | None, editions: int, favourites: config.Favourites) -> int:
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
        history = track_history.fetch_track_history(circuit_id, before, favourites, editions)
    except (schedule.ScheduleError, form.FormError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(track_history.format_track_history(history, favourites))
    return 0


def _cmd_news(days: int, favourites: config.Favourites) -> int:
    if days < 1:
        print("error: --days must be at least 1", file=sys.stderr)
        return 2
    try:
        report = news.fetch_news(news.default_since(_now(), days), favourites)
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


def _stories_reader(state: str, now: datetime, favourites: config.Favourites) -> Callable[[], dict]:
    """Builds the facts' `stories` section from the state file: the followed stories'
    updates since the last digest."""

    def read() -> dict:
        with send_log.SendLog(state) as log:
            last_digest = log.last_digest_at(now)
        with stories.StoryStore(state) as store:
            since = facts.stories_since(last_digest, now)
            return facts.stories_facts(store, since, last_digest is not None, favourites)

    return read


def _collect_facts(favourites: config.Favourites, state: str) -> dict | None:
    """The facts document, or None (after printing why) when no section could be collected."""
    now = _now()
    document = facts.collect(now, favourites, stories=_stories_reader(state, now, favourites))
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


def _cmd_facts(output: str | None, state: str, favourites: config.Favourites) -> int:
    document = _collect_facts(favourites, state)
    if document is None:
        return 1
    _write(json.dumps(document, ensure_ascii=False, indent=2) + "\n", output)
    _warn_missing(document)
    return 0


def _cmd_digest(
    facts_path: str | None,
    output: str | None,
    model: str | None,
    prompt_only: bool,
    state: str,
    favourites: config.Favourites,
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
        document = _collect_facts(favourites, state)
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


def _calendar() -> list[schedule.Race] | None:
    """The current season's calendar, or None (after printing why) when it is unavailable."""
    try:
        return schedule.fetch_schedule()
    except schedule.ScheduleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _decide_summary(races: list[schedule.Race], log: send_log.SendLog) -> offseason.Decision | None:
    """The off-season decision, or None (after printing why) when the calendar is
    unavailable. `races` is the current season's calendar, reused when it is this year's."""
    now = _now()
    year = now.astimezone(schedule.WARSAW).year
    calendar = [r for r in races if r.season == year]
    try:
        if not calendar:
            calendar = schedule.fetch_schedule(year)
    except schedule.ScheduleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None

    def next_season() -> list[schedule.Race]:
        try:
            return schedule.fetch_schedule(year + 1)
        except schedule.ScheduleError as exc:
            print(f"warning: next season's calendar: {exc}", file=sys.stderr)
            return []

    return offseason.decide(calendar, now, log, next_season)


def _cmd_due(days_before: int, state: str) -> int:
    if days_before < 0:
        print("error: --days-before must not be negative", file=sys.stderr)
        return 2
    try:
        with send_log.SendLog(state) as log:
            races = _calendar()
            if races is None:
                return 1
            decision = send_window.decide(races, _now(), log, days_before)
            print(send_window.describe(decision))
            if decision.due:
                return 0
            quali = after_qualifying.decide(races, _now(), log)
            if quali.under_way:
                print(after_qualifying.describe(quali))
            summary = _decide_summary(races, log)
            if summary is None:
                return 1
            if summary.off_season:
                print(offseason.describe(summary))
    except send_log.StateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _digest_for(
    race: schedule.Race, model: str | None, state: str, favourites: config.Favourites
) -> list[str] | None:
    """The digest messages for `race`, or None (after printing why) when they cannot be made."""
    document = _collect_facts(favourites, state)
    if document is None:
        return None
    about = document["sections"]["race"]
    if about is None or (about["season"], about["round"]) != (race.season, race.round):
        print(
            f"error: the facts are not about round {race.round} of {race.season}", file=sys.stderr
        )
        return None
    _warn_missing(document)
    try:
        text = digest.generate(document, digest.ClaudeCli(model=model))
    except digest.GenerationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    return _messages(text)


@dataclass(frozen=True)
class _Target:
    """What a run sends: a round's digest or a month's summary, and its row in the log."""

    label: str  # "round 18 of 2026"
    forget_args: str  # "--season 2026 --round 18"
    claim: Callable[[datetime], bool]
    release: Callable[[], None]
    mark_partial: Callable[[datetime, int], None]
    mark_sent: Callable[[datetime, int], None]


def _round_target(log: send_log.SendLog, race: schedule.Race) -> _Target:
    season, round_ = race.season, race.round
    return _Target(
        label=f"round {round_} of {season}",
        forget_args=f"--season {season} --round {round_}",
        claim=lambda now: log.claim(season, round_, now),
        release=lambda: log.release(season, round_),
        mark_partial=lambda now, n: log.mark_partial(season, round_, now, n),
        mark_sent=lambda now, n: log.mark_sent(season, round_, now, n),
    )


def _qualifying_target(log: send_log.SendLog, race: schedule.Race) -> _Target:
    season, round_ = race.season, race.round
    return _Target(
        label=f"the message after qualifying for round {round_} of {season}",
        forget_args=f"--season {season} --round {round_} --qualifying",
        claim=lambda now: log.claim_qualifying(season, round_, now),
        release=lambda: log.release_qualifying(season, round_),
        mark_partial=lambda now, n: log.mark_qualifying_partial(season, round_, now, n),
        mark_sent=lambda now, n: log.mark_qualifying_sent(season, round_, now, n),
    )


def _summary_target(log: send_log.SendLog, month: str) -> _Target:
    return _Target(
        label=f"the {month} summary",
        forget_args=f"--month {month}",
        claim=lambda now: log.claim_summary(month, now),
        release=lambda: log.release_summary(month),
        mark_partial=lambda now, n: log.mark_summary_partial(month, now, n),
        mark_sent=lambda now, n: log.mark_summary_sent(month, now, n),
    )


def _deliver(
    target: _Target,
    chunks: list[str],
    credentials: telegram_send.Credentials,
) -> int:
    # Claimed right before sending: of two runs at once, only one gets past this.
    if not target.claim(_now()):
        print(f"{target.label} is already sent or being sent", file=sys.stderr)
        return 0
    try:
        with _telegram_client() as client:
            telegram_send.send(chunks, credentials, client)
    except telegram_send.SendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.nothing_delivered:
            target.release()
            print("nothing was delivered; the next run will try again", file=sys.stderr)
        else:
            target.mark_partial(_now(), exc.sent)
            print(
                "part of it may be in the chat, so it is not resent automatically; "
                f"check the chat, then `f1brief forget {target.forget_args}` "
                "to allow another send",
                file=sys.stderr,
            )
        return 1
    target.mark_sent(_now(), len(chunks))
    print(f"sent {len(chunks)} message(s) for {target.label}", file=sys.stderr)
    return 0


def _archive_news(
    state: str, since: datetime, dry_run: bool, favourites: config.Favourites
) -> tuple[list[news.NewsItem], tuple[str, ...]]:
    """Fetches today's news, adds it to the archive (unless `dry_run`) and returns every
    item since `since` with the feeds that failed today. A fetch failure is only a warning."""
    fresh: list[news.NewsItem] = []
    failed: tuple[str, ...] = tuple(f.name for f in news.FEEDS)
    try:
        report = news.fetch_news(since, favourites)
        fresh, failed = list(report.items), report.failed_feeds
    except news.NewsError as exc:
        print(f"warning: news: {exc}", file=sys.stderr)
    with news_archive.NewsArchive(state) as archive:
        if not dry_run:
            archive.add(fresh, _now())
        items = archive.since(since)
    # After `add` the archive holds today's items too; on a dry run they are added here.
    # Either way `news.select` merges any item that appears twice.
    return items + fresh, failed


def _summary_for(
    decision: offseason.Decision,
    items: list[news.NewsItem],
    failed: tuple[str, ...],
    model: str | None,
    favourites: config.Favourites,
) -> list[str] | None:
    """The monthly summary messages, or None (after printing why) when they cannot be made."""
    assert decision.since is not None
    document = offseason.collect(
        _now(), decision.since, decision.next_race, items, favourites, failed
    )
    _warn_missing(document)
    try:
        text = digest.generate_summary(
            document, digest.ClaudeCli(model=model, system_prompt=digest.SUMMARY_SYSTEM_PROMPT)
        )
    except digest.GenerationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    return _messages(text)


def _classification(race: schedule.Race) -> list[form.QualifyingResult] | None:
    """The round's qualifying classification (empty, after saying so, while it is not
    published), or None (after printing why) when Jolpica failed."""
    try:
        results = after_qualifying.fetch_classification(race)
    except form.FormError as exc:
        print(f"error: qualifying: {exc}", file=sys.stderr)
        return None
    if not results:
        print(
            f"the qualifying classification for {race.name} is not published yet",
            file=sys.stderr,
        )
    return results


def _qualifying_document(
    race: schedule.Race, results: list[form.QualifyingResult], favourites: config.Favourites
) -> dict:
    document = after_qualifying.collect(race, results, _now(), favourites)
    _warn_missing(document)
    return document


def _qualifying_text(document: dict, model: str | None) -> str | None:
    try:
        return digest.generate_after_qualifying(
            document,
            digest.ClaudeCli(model=model, system_prompt=digest.QUALIFYING_SYSTEM_PROMPT),
        )
    except digest.GenerationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _send_or_print(
    chunks: list[str] | None, credentials: telegram_send.Credentials | None, target: _Target
) -> int:
    if chunks is None:
        return 1
    if credentials is None:
        _print_messages(chunks)
        print(f"dry run: {len(chunks)} message(s) not sent, nothing recorded", file=sys.stderr)
        return 0
    return _deliver(target, chunks, credentials)


def _credentials(dry_run: bool, env_file: str) -> telegram_send.Credentials | None:
    """None for a dry run; raises SendError when the settings are missing."""
    return None if dry_run else telegram_send.load_credentials(env_file=env_file)


def _cmd_run(
    days_before: int,
    state: str,
    dry_run: bool,
    model: str | None,
    env_file: str,
    favourites: config.Favourites,
) -> int:
    if days_before < 0:
        print("error: --days-before must not be negative", file=sys.stderr)
        return 2
    code = _run_due(days_before, state, dry_run, model, env_file, favourites)
    # Every run, whatever was due: alerts that waited for the morning go out with the first
    # run after the quiet hours.
    return max(code, _cmd_alerts(state, dry_run, env_file))


def _cmd_alerts(state: str, dry_run: bool, env_file: str) -> int:
    now = _now()
    try:
        with stories.StoryStore(state) as store:
            planned = alerts.plan(store, now)
            offers = lifecycle.plan(store, now)
            any_alert = bool(planned.send or planned.waiting or planned.skip)
            any_offer = bool(offers.send or offers.waiting)
            if not (any_alert or any_offer):
                return 0
            if any_alert:
                print(planned.describe(), file=sys.stderr)
            if any_offer:
                print(offers.describe(), file=sys.stderr)
            if dry_run:
                for alert in planned.send:
                    for reply in alerts.replies(alert):
                        _print_action(reply)
                for offer in offers.send:
                    _print_action(lifecycle.reply(offer, now))
                print("dry run: no alert sent, nothing recorded", file=sys.stderr)
                return 0
            if not (planned.send or offers.send):
                alerts.deliver(store, planned, now, send=lambda reply: None)
                return 0
            credentials = telegram_send.load_credentials(env_file=env_file)
            with _telegram_client() as client:
                send = bot.BotApi(credentials, client).send
                # Offers wait while Telegram refuses the alerts.
                ok = alerts.deliver(store, planned, now, send) and lifecycle.deliver(
                    store, offers, now, send
                )
            return 0 if ok else 1
    except (telegram_send.SendError, send_log.StateError, stories.StoryError) as exc:
        print(f"error: story alerts: {exc}", file=sys.stderr)
        return 1


def _run_due(
    days_before: int,
    state: str,
    dry_run: bool,
    model: str | None,
    env_file: str,
    favourites: config.Favourites,
) -> int:
    """The digest, the message after qualifying or the monthly summary, whichever is due."""
    try:
        with send_log.SendLog(state) as log:
            races = _calendar()
            if races is None:
                return 1
            decision = send_window.decide(races, _now(), log, days_before)
            print(send_window.describe(decision), file=sys.stderr)
            if decision.due and decision.race is not None:
                # Before the digest is written, so missing settings cost no claude run.
                credentials = _credentials(dry_run, env_file)
                chunks = _digest_for(decision.race, model, state, favourites)
                return _send_or_print(chunks, credentials, _round_target(log, decision.race))
            quali = after_qualifying.decide(races, _now(), log)
            if quali.under_way:
                print(after_qualifying.describe(quali), file=sys.stderr)
            if quali.due and quali.race is not None:
                credentials = _credentials(dry_run, env_file)
                # Nothing is claimed until the text is ready: while the classification is
                # not published (or Jolpica fails) the next run tries again.
                results = _classification(quali.race)
                if not results:
                    return 0 if results is not None else 1
                document = _qualifying_document(quali.race, results, favourites)
                text = _qualifying_text(document, model)
                chunks = None if text is None else _messages(text)
                return _send_or_print(chunks, credentials, _qualifying_target(log, quali.race))
            summary = _decide_summary(races, log)
            if summary is None:
                return 1
            if not summary.off_season:
                return 0
            print(offseason.describe(summary), file=sys.stderr)
            assert summary.since is not None
            if not summary.due:
                # Every off-season day keeps the news, so the summary covers a whole month.
                if not dry_run:
                    _archive_news(state, summary.since, False, favourites)
                return 0
            credentials = _credentials(dry_run, env_file)
            items, failed = _archive_news(state, summary.since, dry_run, favourites)
            chunks = _summary_for(summary, items, failed, model, favourites)
            return _send_or_print(chunks, credentials, _summary_target(log, summary.month))
    except telegram_send.SendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except send_log.StateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_summary(
    output: str | None,
    model: str | None,
    prompt_only: bool,
    state: str,
    favourites: config.Favourites,
) -> int:
    now = _now()
    today = now.astimezone(schedule.WARSAW).date()
    races = _calendar()
    if races is None:
        return 1
    race = offseason.next_weekend(races, today)
    if race is None:
        try:
            race = offseason.next_weekend(schedule.fetch_schedule(today.year + 1), today)
        except schedule.ScheduleError as exc:
            print(f"warning: next season's calendar: {exc}", file=sys.stderr)
    since = offseason.period_start(today)
    try:
        # Reads the archive but adds nothing to it: only `run` keeps the news.
        items, failed = _archive_news(state, since, True, favourites)
    except send_log.StateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    document = offseason.collect(now, since, race, items, favourites, failed)
    if prompt_only:
        _write(digest.build_prompt(document, digest.SUMMARY_TEMPLATE), output)
        return 0
    _warn_missing(document)
    try:
        text = digest.generate_summary(
            document, digest.ClaudeCli(model=model, system_prompt=digest.SUMMARY_SYSTEM_PROMPT)
        )
    except digest.GenerationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _write(text + "\n", output)
    return 0


def _cmd_quali(
    season: str,
    round_: int | None,
    driver_ids: list[str] | None,
    team_ids: list[str] | None,
    output: str | None,
    model: str | None,
    prompt_only: bool,
    favourites: config.Favourites,
) -> int:
    try:
        races = schedule.fetch_schedule(season)
    except schedule.ScheduleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if round_ is None:
        race = schedule.next_race(races, _now())
    else:
        race = next((r for r in races if r.round == round_), None)
    if race is None:
        print(f"error: no such race in season {season}", file=sys.stderr)
        return 1
    results = _classification(race)
    if not results:
        return 1
    document = _qualifying_document(race, results, favourites.only(team_ids, driver_ids))
    if prompt_only:
        _write(digest.build_prompt(document, digest.QUALIFYING_TEMPLATE), output)
        return 0
    text = _qualifying_text(document, model)
    if text is None:
        return 1
    _write(text + "\n", output)
    return 0


class _DryRun(Exception):
    """Rolls back what a dry run stored."""


def _print_action(action: bot.Action) -> None:
    if isinstance(action, bot.Answer):
        print(f"[button answer] {action.text}\n")
        return
    if isinstance(action, bot.Document):
        print(f"[file {action.filename}, {len(action.content)} bytes] {action.caption}\n")
        return
    print(action.text)
    for row in action.buttons:
        print(" ".join(f"[{b.text}]" for b in row))
    print()


def _cmd_bot(
    state: str,
    env_file: str,
    dry_run: bool,
    updates_path: str | None,
    chat_id: str | None,
    settings: config.Config,
) -> int:
    if dry_run != (updates_path is not None):
        print("error: --dry-run and --updates go together", file=sys.stderr)
        return 2
    if chat_id is not None and not dry_run:
        print("error: --chat-id only works with --dry-run", file=sys.stderr)
        return 2
    config_path = settings.source or config.DEFAULT_PATH

    def add_favourites(
        teams: Sequence[config.Favourite], drivers: Sequence[config.Favourite]
    ) -> config.Favourites:
        updated = config.add_favourites(config_path, teams, drivers, write=not dry_run)
        if dry_run:
            names = ", ".join(f.name for f in (*teams, *drivers))
            print(f"dry run: {config_path} not changed (would add {names})", file=sys.stderr)
        return updated.favourites

    try:
        if dry_run:
            text = _read_text(updates_path)
            if text is None:
                return 1
            raw = json.loads(text)
            updates = raw.get("result") if isinstance(raw, dict) else raw
            if not isinstance(updates, list):
                print("error: --updates must hold a list of updates", file=sys.stderr)
                return 1
            chat = chat_id or telegram_send.load_chat_id(env_file=env_file)
            with stories.StoryStore(state) as store:
                handler = bot.Bot(store, chat, settings.favourites, add_favourites)
                with contextlib.suppress(_DryRun), store.transaction():
                    bot.handle_all(handler, updates, _now(), _print_action)
                    raise _DryRun
            print(f"dry run: {len(updates)} update(s), nothing sent or stored", file=sys.stderr)
            return 0
        credentials = telegram_send.load_credentials(env_file=env_file)
        timeout = httpx.Timeout(15.0, read=bot.POLL_TIMEOUT_S + 15.0)
        with (
            stories.StoryStore(state) as store,
            bot.OffsetStore(state) as offsets,
            httpx.Client(timeout=timeout) as client,
        ):
            handler = bot.Bot(store, credentials.chat_id, settings.favourites, add_favourites)
            print("listening for updates (Ctrl-C to stop)", file=sys.stderr)
            bot.listen(bot.BotApi(credentials, client), handler, offsets=offsets)
    except KeyboardInterrupt:
        return 0
    except json.JSONDecodeError as exc:
        print(f"error: {updates_path}: not JSON: {exc}", file=sys.stderr)
        return 1
    except (
        telegram_send.SendError,
        bot.BotError,
        send_log.StateError,
        stories.StoryError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_detect_stories(
    days: int,
    state: str,
    model: str | None,
    prompt_only: bool,
    dry_run: bool,
    favourites: config.Favourites,
) -> int:
    if days < 1:
        print("error: --days must be at least 1", file=sys.stderr)
        return 2
    now = _now()
    try:
        # All items: one about a followed story's entity (Ferrari, Hamilton) that is hot only
        # through `story_detection.EXTRA_TERMS` matches no favourite and no news topic.
        report = news.fetch_news(news.default_since(now, days), favourites, relevant_only=False)
    except news.NewsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if report.failed_feeds:
        print(f"warning: could not read {', '.join(report.failed_feeds)}", file=sys.stderr)
    # The words that mark a news item as about a favourite (their aliases in the config).
    aliases = [a for fav in favourites.teams + favourites.drivers for a in fav.aliases]
    try:
        with stories.StoryStore(state) as store:
            watch = story_detection.watch_terms(store, aliases)
            items = story_detection.pending(store, report.items, watch)
            if not items:
                print("no new hot news items")
                return 0
            if prompt_only:
                print(story_detection.build_prompt(store, items))
                return 0
            result = story_detection.detect(
                store,
                items,
                digest.ClaudeCli(
                    model=model, system_prompt=story_detection.DETECTION_SYSTEM_PROMPT
                ),
            )
            if not dry_run:
                story_detection.apply(store, result, now)
    except (
        send_log.StateError,
        stories.StoryError,
        digest.GenerationError,
        story_detection.DetectionError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(story_detection.format_plan(result, stored=not dry_run))
    return 0


def _cmd_seed_stories(
    seeds_path: str,
    state: str,
    model: str | None,
    prompt_only: bool,
    dry_run: bool,
    favourites: config.Favourites,
) -> int:
    try:
        seeds = story_seeds.load(seeds_path)
    except story_seeds.SeedError as exc:
        print(f"error: seeds: {exc}", file=sys.stderr)
        return 2
    now = _now()
    try:
        with stories.StoryStore(state) as store:
            fresh = set(store.without_updates([s.url for s in seeds]))
            seeds = tuple(s for s in seeds if s.url in fresh)
            if not seeds:
                print("no seed article left: each is in a story already")
                return 0
            items = story_seeds.items(seeds, favourites, now)
            if prompt_only:
                print(story_detection.build_prompt(store, items))
                return 0
            result = story_detection.detect(
                store,
                items,
                digest.ClaudeCli(
                    model=model, system_prompt=story_detection.DETECTION_SYSTEM_PROMPT
                ),
            )
            slugs = story_seeds.to_follow(result, seeds)
            followed: list[stories.Story] = []
            if not dry_run:
                with store.transaction():
                    story_detection.apply(store, result, now)
                    for slug in slugs:
                        story = store.get_by_slug(slug)
                        if story is None:
                            raise stories.StoryError(f"no story {slug!r}")
                        store.follow(story.id, now)
                        followed.append(story)
    except (
        send_log.StateError,
        stories.StoryError,
        digest.GenerationError,
        story_detection.DetectionError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(story_detection.format_plan(result, stored=not dry_run))
    for slug in slugs if dry_run else ():
        print(f"  would follow {slug}")
    for story in followed:
        print(f"  following #{story.id} {story.slug}")
    stored = story_seeds.stored(result)
    for seed in seeds:
        if seed.url not in stored:
            print(f"  no story from {seed.url}: `seed-stories` asks again next time")
    return 0


def _cmd_forget(
    season: int | None, round_: int | None, month: str | None, qualifying: bool, state: str
) -> int:
    by_round = month is None and season is not None and round_ is not None
    by_month = month is not None and season is None and round_ is None and not qualifying
    if not (by_round or by_month):
        print(
            "error: give either --season and --round (and maybe --qualifying), or --month",
            file=sys.stderr,
        )
        return 2
    if month is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        print("error: --month must look like 2027-01", file=sys.stderr)
        return 2
    try:
        with send_log.SendLog(state) as log:
            if month is not None:
                removed, label = log.forget_summary(month), f"the {month} summary"
            else:
                assert season is not None and round_ is not None
                if qualifying:
                    removed = log.forget_qualifying(season, round_)
                    label = f"the message after qualifying for round {round_} of {season}"
                else:
                    removed, label = log.forget(season, round_), f"round {round_} of {season}"
    except send_log.StateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if removed:
        print(f"forgot {label}")
    else:
        print(f"{label} was not in {state}")
    return 0


def _cmd_archive_page(output: str, state: str) -> int:
    if not Path(state).is_file():
        print(f"error: no state file {state} (no stories yet?)", file=sys.stderr)
        return 1
    now = _now()
    try:
        with stories.StoryStore(state) as store:
            page = archive_page.build(store, now)
    except send_log.StateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if output == "-":
        sys.stdout.write(page)
        return 0
    path = Path(output).expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page, encoding="utf-8")
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {path}")
    print(f"open it with: open {path}")
    return 0


def _cmd_launchd(
    output: str | None,
    workdir: str,
    interval: int,
    log_dir: str | None,
    uv: str | None,
    claude: str | None,
) -> int:
    home = Path.home()
    try:
        job = launchd.make_job(
            Path(workdir),
            Path(log_dir) if log_dir else launchd.default_log_dir(home),
            interval_minutes=interval,
            uv=uv,
            claude=claude,
        )
    except launchd.LaunchdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    content = launchd.render(job)
    if output is None:
        sys.stdout.write(content.decode("utf-8"))
        return 0
    path = Path(output).expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        job.log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {path}")
    print(f"logs: {job.stdout_log}, {job.stderr_log}")
    print("to load it (and run it once now), run:")
    for command in launchd.install_commands(path):
        print(f"  {command}")
    return 0


def _cmd_config(settings: config.Config) -> int:
    where = str(settings.source) if settings.source else "built-in defaults (no config file)"
    print(f"settings: {where}")
    print(f"language: {settings.language}")
    print(f"days before FP1: {settings.days_before_fp1}")
    print(f"time zone: {settings.timezone}")
    for label, entries in (
        ("teams", settings.favourites.teams),
        ("drivers", settings.favourites.drivers),
    ):
        print(f"favourite {label}:")
        for fav in entries:
            extra = " (and their future in F1)" if fav.track_future else ""
            print(f"  {fav.id}: {fav.name}, news words: {', '.join(fav.aliases)}{extra}")
        if not entries:
            print("  none")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = config.load(args.config)
    except config.ConfigError as exc:
        print(f"error: config: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "days_before", "absent") is None:
        args.days_before = settings.days_before_fp1
    if args.command == "config":
        return _cmd_config(settings)
    if args.command == "next":
        return _cmd_next(args.season)
    if args.command == "standings":
        return _cmd_standings(args.season)
    favourites = settings.favourites
    if args.command == "form":
        return _cmd_form(args.team_ids or favourites.team_ids, args.last, args.season)
    if args.command == "drivers":
        return _cmd_drivers(args.driver_ids or favourites.driver_ids, args.last, args.season)
    if args.command == "pace":
        return _cmd_pace(favourites)
    if args.command == "track":
        return _cmd_track(args.circuit, args.editions, favourites)
    if args.command == "news":
        return _cmd_news(args.days, favourites)
    if args.command == "weather":
        return _cmd_weather()
    if args.command == "facts":
        return _cmd_facts(args.output, args.state, favourites)
    if args.command == "digest":
        return _cmd_digest(
            args.facts, args.output, args.model, args.prompt_only, args.state, favourites
        )
    if args.command == "summary":
        return _cmd_summary(args.output, args.model, args.prompt_only, args.state, favourites)
    if args.command == "quali":
        return _cmd_quali(
            args.season,
            args.round,
            args.driver_ids,
            args.team_ids,
            args.output,
            args.model,
            args.prompt_only,
            favourites,
        )
    if args.command == "format":
        return _cmd_format(args.input, args.limit)
    if args.command == "send":
        return _cmd_send(args.input, args.dry_run, args.env_file)
    if args.command == "due":
        return _cmd_due(args.days_before, args.state)
    if args.command == "run":
        return _cmd_run(
            args.days_before, args.state, args.dry_run, args.model, args.env_file, favourites
        )
    if args.command == "alerts":
        return _cmd_alerts(args.state, args.dry_run, args.env_file)
    if args.command == "bot":
        return _cmd_bot(
            args.state, args.env_file, args.dry_run, args.updates, args.chat_id, settings
        )
    if args.command == "detect-stories":
        return _cmd_detect_stories(
            args.days, args.state, args.model, args.prompt_only, args.dry_run, favourites
        )
    if args.command == "seed-stories":
        return _cmd_seed_stories(
            args.seeds, args.state, args.model, args.prompt_only, args.dry_run, favourites
        )
    if args.command == "archive-page":
        return _cmd_archive_page(args.output, args.state)
    if args.command == "launchd":
        return _cmd_launchd(
            args.output, args.workdir, args.interval, args.log_dir, args.uv, args.claude
        )
    if args.command == "forget":
        return _cmd_forget(args.season, args.round, args.month, args.qualifying, args.state)
    return 0

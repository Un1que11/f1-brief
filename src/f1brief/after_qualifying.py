"""A short message after qualifying (F19): the favourites' grid positions and what they mean
for the race.

The message for a round is due from `OPENS_AFTER` after the qualifying start (the session takes
about an hour, and Jolpica publishes the classification a little later) until the race starts.
The daily job sends it on its first run in that window, once per round (`qualifying_sends` in
the send log); a run that finds the classification not published yet leaves it to the next
one. With the qualifying start time to be confirmed the window opens at midnight after the
qualifying day, and with the race start time to be confirmed it closes at midnight before the
race day, so it can be empty.

Positions are the qualifying classification: Jolpica has no starting grid before the race, so
grid penalties are known only from the news in the facts. Every function takes the current
time and the favourites as arguments.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from typing import Any

import httpx

from f1brief import drivers, facts, form, jolpica, news, schedule
from f1brief.config import Favourites
from f1brief.schedule import WARSAW, Race, Session, SessionKind
from f1brief.send_log import SendLog, SendRecord, Status
from f1brief.send_window import Window

# 2 (F20): favourites from the config (ids and names), shared teammate head-to-heads.
SCHEMA_VERSION = 2
OPENS_AFTER = timedelta(hours=2)
# News since FP1 (penalties, grid drops); this far back when FP1 has no start time.
NEWS_FALLBACK = timedelta(days=3)
QUALIFYING_PARTS = ("q1", "q2", "q3")


# --- when it is due ---------------------------------------------------------------------


def qualifying_session(race: Race) -> Session | None:
    return next((s for s in race.sessions if s.kind is SessionKind.QUALIFYING), None)


def _local_midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time(), tzinfo=tz).astimezone(UTC)


def window(race: Race, tz: tzinfo = WARSAW) -> Window | None:
    """None when the calendar has no qualifying for the weekend."""
    quali = qualifying_session(race)
    if quali is None:
        return None
    if quali.start_utc is not None:
        opens = quali.start_utc + OPENS_AFTER
    else:
        opens = _local_midnight(quali.date + timedelta(days=1), tz)
    closes = race.race.start_utc or _local_midnight(race.race.date, tz)
    return Window(opens=opens, closes=closes)


@dataclass(frozen=True)
class Decision:
    due: bool
    reason: str
    race: Race | None = None
    window: Window | None = None
    # The race's weekend has started (FP1 is under way or done); before that the decision is
    # not worth showing.
    under_way: bool = False


def _when(moment: datetime, tz: tzinfo = WARSAW) -> str:
    return moment.astimezone(tz).strftime("%a %d %b %H:%M")


def _already(record: SendRecord) -> str:
    when = _when(record.started_at)
    if record.status is Status.SENT:
        return f"already sent at {when}"
    return f"(maybe) partly sent at {when}; not resent automatically, check the chat"


def decide(races: list[Race], now: datetime, log: SendLog) -> Decision:
    """The decision for the next race that has not started. `now` must be timezone-aware."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    race = schedule.next_race(races, now)
    if race is None:
        return Decision(False, "no upcoming race weekend in the calendar")
    fp1 = race.first_session
    under_way = now >= (fp1.start_utc or _local_midnight(fp1.date, WARSAW))
    w = window(race)
    if w is None:
        return Decision(
            False, "the calendar has no qualifying for this weekend", race, None, under_way
        )
    record = log.get_qualifying(race.season, race.round)
    if record is not None:
        return Decision(False, _already(record), race, w, under_way)
    if now < w.opens:
        return Decision(False, f"not yet: due from {_when(w.opens)}", race, w, under_way)
    if now >= w.closes:
        return Decision(False, "the race has started", race, w, under_way)
    return Decision(True, f"due: the race starts {_when(w.closes)}", race, w, under_way)


def describe(decision: Decision) -> str:
    """One line for the console."""
    prefix = "After qualifying"
    if decision.race is not None:
        prefix += f" (round {decision.race.round} of {decision.race.season})"
    return f"{prefix}: {decision.reason}."


# --- the classification -----------------------------------------------------------------


def classification_url(race: Race) -> str:
    return f"{jolpica.BASE_URL}/{race.season}/{race.round}/qualifying.json"


def parse_classification(payload: dict[str, Any], race: Race) -> list[form.QualifyingResult]:
    """The round's classification, pole first; empty while it is not published."""
    rows = form.parse_qualifying(payload).get((race.season, race.round), [])
    return sorted(rows, key=lambda q: q.position)


def fetch_classification(
    race: Race, client: httpx.Client | None = None
) -> list[form.QualifyingResult]:
    try:
        payload = jolpica.get_json(f"{race.season}/{race.round}/qualifying", client)
    except jolpica.JolpicaError as exc:
        raise form.FormError(str(exc)) from exc
    return parse_classification(payload, race)


def lap_seconds(text: str | None) -> Decimal | None:
    """A lap time in seconds ("1:31.712" is 91.712); None for no time or an unreadable one."""
    if not text:
        return None
    minutes, _, seconds = text.rpartition(":")
    try:
        return Decimal(minutes or 0) * 60 + Decimal(seconds)
    except ArithmeticError:
        return None


def last_part(q: form.QualifyingResult) -> str | None:
    """The last part ("Q1", "Q2", "Q3") the driver set a time in."""
    timed = [p for p in QUALIFYING_PARTS if lap_seconds(getattr(q, p)) is not None]
    return timed[-1].upper() if timed else None


def _fastest(results: Iterable[form.QualifyingResult], part: str) -> Decimal | None:
    times = [t for q in results if (t := lap_seconds(getattr(q, part.lower()))) is not None]
    return min(times, default=None)


def _row(
    q: form.QualifyingResult,
    results: Sequence[form.QualifyingResult],
    favourite: bool,
) -> dict[str, Any]:
    part = last_part(q)
    gap = None
    if part is not None:
        own, fastest = lap_seconds(getattr(q, part.lower())), _fastest(results, part)
        if own is not None and fastest is not None:
            gap = float(own - fastest)
    return {
        "position": q.position,
        "driver_id": q.driver_id,
        "driver": q.driver_name,
        "code": q.driver_code,
        "team_id": q.constructor_id,
        "team": q.constructor_name,
        "q1": q.q1,
        "q2": q.q2,
        "q3": q.q3,
        # The last part with a time; null = no time set.
        "last_part": part,
        # Seconds behind the fastest time of that part.
        "gap_to_fastest_in_part_s": gap,
        "favourite": favourite,
    }


def qualifying_facts(
    race: Race, results: Sequence[form.QualifyingResult], favourites: Favourites
) -> dict[str, Any]:
    """The classification with the favourites marked, and each favourite against their
    teammate. A favourite is a favourite driver or a driver of a favourite team. When two
    favourites are teammates, the comparison is given once, under the one classified first."""
    driver_ids, team_ids = set(favourites.driver_ids), set(favourites.team_ids)

    def is_favourite(q: form.QualifyingResult) -> bool:
        return q.driver_id in driver_ids or q.constructor_id in team_ids

    listed: list[dict[str, Any]] = []
    for q in results:
        if not is_favourite(q):
            continue
        mates = [m for m in results if m.constructor_id == q.constructor_id and m is not q]
        mate = mates[0] if len(mates) == 1 else None
        row: dict[str, Any] = {
            "driver_id": q.driver_id,
            "driver": q.driver_name,
            "team": q.constructor_name,
            "position": q.position,
            "teammate": mate.driver_name if mate else None,
        }
        if mate is not None and any(f["driver_id"] == mate.driver_id for f in listed):
            row["head_to_head_in"] = mate.driver_id  # that entry has the comparison
        else:
            row["teammate_position"] = mate.position if mate else None
            row["ahead_of_teammate"] = q.position < mate.position if mate else None
        listed.append(row)
    classified = {q.driver_id for q in results}
    return {
        "note": (
            "Qualifying classification. Jolpica has no starting grid before the race: grid "
            "penalties are known only from the news."
        ),
        "classification": [_row(q, results, is_favourite(q)) for q in results],
        "favourites": listed,
        # Favourite drivers missing from the classification (did not take part, or replaced).
        "favourite_drivers_not_classified": sorted(driver_ids - classified),
        "sources": [{"name": jolpica.SOURCE_NAME, "url": classification_url(race)}],
    }


# --- the facts document -----------------------------------------------------------------


SECTIONS = ("race", "qualifying", "drivers", "news")


def news_since(race: Race, now: datetime) -> datetime:
    return race.first_session.start_utc or now - NEWS_FALLBACK


def form_before(f: drivers.DriverForm, race: Race) -> drivers.DriverForm:
    """Only the rounds before `race`: for a past round (`quali --round`) the results would
    otherwise include the race itself."""
    before = tuple(r for r in f.rounds if (r.season, r.round) < (race.season, race.round))
    return replace(f, rounds=before)


def collect(
    race: Race,
    results: Sequence[form.QualifyingResult],
    now: datetime,
    favourites: Favourites,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """The facts document for the message: the weekend, its qualifying `results`, the
    favourite drivers' recent form (grid against finish, teammate) and the news since FP1.
    A section that fails is `null` and listed in `missing`. `now` must be aware."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    recorder = facts.Recorder()
    client.event_hooks["request"].append(recorder)
    sections: dict[str, dict[str, Any] | None] = {
        "race": facts.race_facts(race),
        "qualifying": qualifying_facts(race, results, favourites),
    }
    missing: list[dict[str, str]] = []

    def section(name: str, build: Callable[[], dict[str, Any]]) -> None:
        try:
            data = build()
        except facts.COLLECTION_ERRORS as exc:
            sections[name] = None
            missing.append({"section": name, "reason": str(exc)})
        else:
            sources = recorder.take()
            if name == "news":  # a feed that could not be read is not a source
                sources = [s for s in sources if s["name"] not in data["failed_feeds"]]
            sections[name] = {**data, "sources": sources}
        recorder.take()

    try:
        section(
            "drivers",
            lambda: facts.drivers_facts(
                form_before(drivers.fetch_driver_form(d, client=client), race)
                for d in favourites.driver_ids
            ),
        )
        section(
            "news",
            lambda: facts.news_facts(
                news.fetch_news(news_since(race, now), favourites, client=client)
            ),
        )
    finally:
        client.event_hooks["request"].remove(recorder)
        if owns_client:
            client.close()

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "after_qualifying",
        "generated_at": now.astimezone(WARSAW).isoformat(),
        "timezone": str(WARSAW),
        "favourites": facts.favourites_facts(favourites),
        "sections": {name: sections[name] for name in SECTIONS},
        "missing": missing,
    }

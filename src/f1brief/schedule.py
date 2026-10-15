"""Season calendar and the next race weekend, from the Jolpica-F1 API."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, tzinfo
from enum import StrEnum
from itertools import groupby
from typing import Any
from zoneinfo import ZoneInfo

import httpx

JOLPICA_BASE_URL = "https://api.jolpi.ca/ergast/f1"
WARSAW = ZoneInfo("Europe/Warsaw")
SOURCE_NAME = "Jolpica-F1"


class SessionKind(StrEnum):
    FP1 = "FP1"
    FP2 = "FP2"
    FP3 = "FP3"
    SPRINT_QUALIFYING = "Sprint Qualifying"
    SPRINT = "Sprint"
    QUALIFYING = "Qualifying"
    RACE = "Race"


# Jolpica (Ergast-compatible) keys for the non-race sessions of a weekend.
_SESSION_KEYS: dict[str, SessionKind] = {
    "FirstPractice": SessionKind.FP1,
    "SecondPractice": SessionKind.FP2,
    "ThirdPractice": SessionKind.FP3,
    "SprintQualifying": SessionKind.SPRINT_QUALIFYING,
    "Sprint": SessionKind.SPRINT,
    "Qualifying": SessionKind.QUALIFYING,
}


@dataclass(frozen=True)
class Session:
    kind: SessionKind
    date: date
    # None while the start time is still to be confirmed; only the date is known then.
    start_utc: datetime | None

    def start_in(self, tz: tzinfo) -> datetime | None:
        return self.start_utc.astimezone(tz) if self.start_utc else None


@dataclass(frozen=True)
class Circuit:
    id: str
    name: str
    locality: str
    country: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Race:
    season: int
    round: int
    name: str
    circuit: Circuit
    # Chronological; the race itself is always last.
    sessions: tuple[Session, ...]
    source_url: str

    @property
    def race(self) -> Session:
        return self.sessions[-1]

    @property
    def first_session(self) -> Session:
        return self.sessions[0]

    @property
    def is_sprint_weekend(self) -> bool:
        return any(s.kind is SessionKind.SPRINT for s in self.sessions)


class ScheduleError(Exception):
    """The calendar could not be fetched or understood."""


def _parse_session(kind: SessionKind, raw: Mapping[str, Any]) -> Session:
    day = date.fromisoformat(raw["date"])
    raw_time = raw.get("time")
    start = None
    if raw_time:
        # Jolpica times are UTC with a trailing "Z", e.g. "13:00:00Z".
        start = datetime.combine(day, time.fromisoformat(raw_time.replace("Z", "+00:00")))
    return Session(kind=kind, date=day, start_utc=start)


# Usual order of sessions within a weekend, used when a start time is not confirmed yet.
# Covers both formats: FP1-FP2-FP3-Q and FP1-SQ-Sprint-Q.
_SESSION_RANK = {
    kind: rank
    for rank, kind in enumerate(
        [
            SessionKind.FP1,
            SessionKind.FP2,
            SessionKind.SPRINT_QUALIFYING,
            SessionKind.FP3,
            SessionKind.SPRINT,
            SessionKind.QUALIFYING,
        ]
    )
}


def _sort_sessions(sessions: list[Session]) -> list[Session]:
    """Chronological order: by date, then by start time when every time that day is known."""
    ordered: list[Session] = []
    by_usual_order = sorted(sessions, key=lambda s: (s.date, _SESSION_RANK[s.kind]))
    for _, group in groupby(by_usual_order, key=lambda s: s.date):
        day = list(group)
        if all(s.start_utc is not None for s in day):
            day.sort(key=lambda s: s.start_utc)
        ordered.extend(day)
    return ordered


def parse_race(raw: Mapping[str, Any]) -> Race:
    try:
        sessions = _sort_sessions(
            [_parse_session(kind, raw[key]) for key, kind in _SESSION_KEYS.items() if key in raw]
        )
        sessions.append(_parse_session(SessionKind.RACE, raw))

        circuit = raw["Circuit"]
        location = circuit["Location"]
        return Race(
            season=int(raw["season"]),
            round=int(raw["round"]),
            name=raw["raceName"],
            circuit=Circuit(
                id=circuit["circuitId"],
                name=circuit["circuitName"],
                locality=location["locality"],
                country=location["country"],
                latitude=float(location["lat"]),
                longitude=float(location["long"]),
            ),
            sessions=tuple(sessions),
            source_url=raw.get("url", ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ScheduleError(f"unexpected race entry in schedule: {exc!r}") from exc


def parse_schedule(payload: Mapping[str, Any]) -> list[Race]:
    try:
        races = payload["MRData"]["RaceTable"]["Races"]
    except (KeyError, TypeError) as exc:
        raise ScheduleError("schedule response has no MRData.RaceTable.Races") from exc
    return sorted((parse_race(r) for r in races), key=lambda r: r.round)


def fetch_schedule(season: int | str = "current", client: httpx.Client | None = None) -> list[Race]:
    """Fetches one season's calendar. Read-only GET; no API key needed."""
    url = f"{JOLPICA_BASE_URL}/{season}.json"
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        response = client.get(url, params={"limit": 100})
        response.raise_for_status()
        return parse_schedule(response.json())
    except httpx.HTTPError as exc:
        raise ScheduleError(f"failed to fetch {url}: {exc}") from exc
    except ValueError as exc:
        raise ScheduleError(f"invalid JSON from {url}") from exc
    finally:
        if owns_client:
            client.close()


def next_race(races: list[Race], now: datetime) -> Race | None:
    """The upcoming weekend: the first race that has not started yet.

    While a weekend is in progress (FP1 done, race still ahead) that weekend is returned.
    A race whose start time is unknown counts as upcoming until its date has passed.
    `now` must be timezone-aware.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    for race in sorted(races, key=lambda r: (r.season, r.round)):
        start = race.race.start_utc
        if start is not None and start > now:
            return race
        if start is None and race.race.date >= now.astimezone(WARSAW).date():
            return race
    return None


def format_schedule(race: Race, tz: tzinfo = WARSAW) -> str:
    """Human-readable weekend schedule in the given time zone."""
    lines = [
        f"Round {race.round}: {race.name} ({race.season})",
        f"{race.circuit.name}, {race.circuit.locality}, {race.circuit.country}",
    ]
    for session in race.sessions:
        local = session.start_in(tz)
        when = local.strftime("%a %d %b %H:%M") if local else f"{session.date:%a %d %b} TBC"
        lines.append(f"  {session.kind.value:<17} {when}")
    lines.append(f"Times: {tz}. Source: {SOURCE_NAME}.")
    return "\n".join(lines)

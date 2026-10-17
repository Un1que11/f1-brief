"""Race pace: median lap time on long runs in the last race, favourites vs the leaders.

Data comes from OpenF1 (laps, stints, race control, results). FastF1 was the original plan,
but the F1 live-timing archive it reads now answers 401 and its mirror has no data.
"""

import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from f1brief import openf1
from f1brief.config import Favourites
from f1brief.news import term_pattern

# A stint counts as a long run from this many laps.
MIN_STINT_LAPS = 8
# Laps slower than this multiple of the driver's median clean lap are traffic or incidents.
OUTLIER_FACTOR = Decimal("1.07")
# Fewer clean laps than this gives no pace figure.
MIN_LAPS = 5
# The reference is the best long-run pace among this many top finishers.
LEADERS = 3


class PaceError(openf1.OpenF1Error):
    """Pace data could not be fetched or understood."""


@dataclass(frozen=True)
class Lap:
    driver_number: int
    lap_number: int
    # Seconds; None when the lap has no time (e.g. a red-flag lap).
    duration: Decimal | None
    is_pit_out_lap: bool


@dataclass(frozen=True)
class Stint:
    driver_number: int
    stint_number: int
    lap_start: int
    lap_end: int
    compound: str | None

    @property
    def length(self) -> int:
        return self.lap_end - self.lap_start + 1


@dataclass(frozen=True)
class Driver:
    number: int
    code: str
    name: str
    team: str


@dataclass(frozen=True)
class DriverPace:
    driver: Driver
    # Finishing position; None if not classified.
    position: int | None
    # Median long-run lap in seconds; None with too few clean laps.
    median: Decimal | None
    laps: int


@dataclass(frozen=True)
class PaceReport:
    session_key: int
    race_name: str
    date: datetime
    # Laps excluded for every driver: safety car, VSC, red flag (plus the restart lap).
    neutralised_laps: frozenset[int]
    reference: DriverPace | None
    leaders: tuple[DriverPace, ...]
    favourites: tuple[DriverPace, ...]

    def delta(self, pace: DriverPace) -> Decimal | None:
        """Seconds per lap slower than the reference (negative = faster)."""
        if pace.median is None or self.reference is None or self.reference.median is None:
            return None
        return pace.median - self.reference.median


def neutralised_laps(race_control: Iterable[dict[str, Any]], last_lap: int) -> frozenset[int]:
    """Laps run behind the safety car or VSC, or under a red flag, plus the lap after each
    (restarts are not representative). A neutralisation that never ends runs to `last_lap`."""
    laps: set[int] = set()
    start: int | None = None
    for message in sorted(race_control, key=lambda m: m.get("date") or ""):
        lap = message.get("lap_number")
        text = (message.get("message") or "").upper()
        if lap is None:
            continue
        if message.get("flag") == "RED":
            laps.update({lap, lap + 1})
        if message.get("category") != "SafetyCar":
            continue
        if "DEPLOYED" in text and start is None:
            start = lap
        elif ("ENDING" in text or "IN THIS LAP" in text) and start is not None:
            laps.update(range(start, lap + 2))
            start = None
    if start is not None:
        laps.update(range(start, last_lap + 1))
    return frozenset(laps)


def long_run_laps(
    laps: Sequence[Lap], stints: Sequence[Stint], neutralised: frozenset[int]
) -> list[Decimal]:
    """One driver's representative laps: long stints only, without lap 1, pit in/out laps,
    neutralised laps and slow outliers."""
    if not stints:
        return []
    last_stint = max(s.stint_number for s in stints)
    candidates = []
    for lap in laps:
        if lap.duration is None or lap.lap_number <= 1 or lap.is_pit_out_lap:
            continue
        if lap.lap_number in neutralised:
            continue
        stint = next((s for s in stints if s.lap_start <= lap.lap_number <= s.lap_end), None)
        if stint is None or stint.length < MIN_STINT_LAPS:
            continue
        if lap.lap_number == stint.lap_end and stint.stint_number < last_stint:
            continue  # in-lap: ends in the pit lane
        candidates.append(lap.duration)
    if not candidates:
        return []
    limit = statistics.median(candidates) * OUTLIER_FACTOR
    return [d for d in candidates if d <= limit]


def driver_pace(
    driver: Driver,
    position: int | None,
    laps: Sequence[Lap],
    stints: Sequence[Stint],
    neutralised: frozenset[int],
) -> DriverPace:
    clean = long_run_laps(laps, stints, neutralised)
    median = statistics.median(clean) if len(clean) >= MIN_LAPS else None
    return DriverPace(driver=driver, position=position, median=median, laps=len(clean))


def _seconds_value(raw: Any) -> Decimal | None:
    """Lap time as Decimal. A float goes through its shortest text form ("97.3"), which is what
    the API sent; Decimal(97.3) directly would carry binary noise (97.29999...)."""
    if raw is None:
        return None
    return Decimal(str(raw)) if isinstance(raw, float) else Decimal(raw)


def _parse_laps(rows: Iterable[dict[str, Any]]) -> list[Lap]:
    return [
        Lap(
            driver_number=int(r["driver_number"]),
            lap_number=int(r["lap_number"]),
            duration=_seconds_value(r.get("lap_duration")),
            is_pit_out_lap=bool(r.get("is_pit_out_lap")),
        )
        for r in rows
    ]


def _parse_stints(rows: Iterable[dict[str, Any]]) -> list[Stint]:
    return [
        Stint(
            driver_number=int(r["driver_number"]),
            stint_number=int(r["stint_number"]),
            lap_start=int(r["lap_start"]),
            lap_end=int(r["lap_end"]),
            compound=r.get("compound"),
        )
        for r in rows
        if r.get("lap_start") is not None and r.get("lap_end") is not None
    ]


def _parse_drivers(rows: Iterable[dict[str, Any]]) -> dict[int, Driver]:
    return {
        int(r["driver_number"]): Driver(
            number=int(r["driver_number"]),
            code=r["name_acronym"],
            name=r.get("full_name") or r["name_acronym"],
            team=r.get("team_name") or "",
        )
        for r in rows
    }


def favourite_matcher(favourites: Favourites) -> Callable[[Driver], bool]:
    """Whether an OpenF1 driver is a favourite: OpenF1 has no Jolpica ids, so a driver counts
    when a favourite driver's alias is in their full name ("George RUSSELL") or a favourite
    team's alias is in their team name ("Haas F1 Team")."""
    teams = [a for t in favourites.teams for a in t.aliases]
    drivers = [a for d in favourites.drivers for a in d.aliases]
    team_pattern = term_pattern(teams) if teams else None
    driver_pattern = term_pattern(drivers) if drivers else None

    def matches(driver: Driver) -> bool:
        return bool(
            (team_pattern and team_pattern.search(driver.team))
            or (driver_pattern and driver_pattern.search(driver.name))
        )

    return matches


def build_report(
    session: dict[str, Any],
    drivers_rows: list[dict[str, Any]],
    laps_rows: list[dict[str, Any]],
    stints_rows: list[dict[str, Any]],
    race_control_rows: list[dict[str, Any]],
    result_rows: list[dict[str, Any]],
    favourites: Favourites,
) -> PaceReport:
    try:
        drivers = _parse_drivers(drivers_rows)
        laps = _parse_laps(laps_rows)
        stints = _parse_stints(stints_rows)
        positions = {
            int(r["driver_number"]): (int(r["position"]) if r.get("position") else None)
            for r in result_rows
        }
        neutral = neutralised_laps(
            race_control_rows, max((lap.lap_number for lap in laps), default=0)
        )
        paces = {
            number: driver_pace(
                driver,
                positions.get(number),
                [lap for lap in laps if lap.driver_number == number],
                [s for s in stints if s.driver_number == number],
                neutral,
            )
            for number, driver in drivers.items()
        }
        date = datetime.fromisoformat(session["date_start"])
        race_name = f"{session.get('country_name', '')} ({session.get('location', '')})"
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise PaceError(f"unexpected OpenF1 data: {exc!r}") from exc

    leaders = tuple(
        sorted(
            (p for p in paces.values() if p.position is not None and p.position <= LEADERS),
            key=lambda p: p.position,
        )
    )
    with_pace = [p for p in leaders if p.median is not None]
    reference = min(with_pace, key=lambda p: p.median) if with_pace else None
    is_favourite = favourite_matcher(favourites)
    chosen = tuple(
        sorted(
            (p for p in paces.values() if is_favourite(p.driver)),
            key=lambda p: (p.position is None, p.position or 0, p.driver.code),
        )
    )
    return PaceReport(
        session_key=int(session["session_key"]),
        race_name=race_name,
        date=date,
        neutralised_laps=neutral,
        reference=reference,
        leaders=leaders,
        favourites=chosen,
    )


def last_race_session(sessions: Iterable[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """The most recent Grand Prix (not sprint, not cancelled) that has finished by `now`."""
    finished = [
        s
        for s in sessions
        if s.get("session_name") == "Race"
        and not s.get("is_cancelled")
        and s.get("date_end")
        and datetime.fromisoformat(s["date_end"]) <= now
    ]
    return max(finished, key=lambda s: s["date_end"], default=None)


def fetch_report(
    now: datetime, favourites: Favourites, client: httpx.Client | None = None, **kwargs: Any
) -> PaceReport:
    """Pace report for the last finished race (this season, else the previous one)."""

    def rows(endpoint: str, **params: Any) -> list[dict[str, Any]]:
        try:
            return openf1.get_rows(endpoint, params, client, **kwargs)
        except openf1.OpenF1Error as exc:
            raise PaceError(str(exc)) from exc

    session = last_race_session(rows("sessions", year=now.year, session_name="Race"), now)
    if session is None:
        session = last_race_session(rows("sessions", year=now.year - 1, session_name="Race"), now)
    if session is None:
        raise PaceError("no finished race found")
    key = session["session_key"]
    return build_report(
        session,
        rows("drivers", session_key=key),
        rows("laps", session_key=key),
        rows("stints", session_key=key),
        rows("race_control", session_key=key),
        rows("session_result", session_key=key),
        favourites,
    )


def _seconds(value: Decimal) -> str:
    minutes, seconds = divmod(value, 60)
    return f"{int(minutes)}:{seconds:06.3f}"


def _delta(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value:+.3f}s"


def format_report(report: PaceReport) -> str:
    lines = [
        f"Long-run pace, {report.race_name} {report.date:%Y-%m-%d}",
        "Median lap on stints of 8+ laps; lap 1, pit in/out, SC/VSC/red-flag and slow laps "
        "excluded.",
    ]
    if report.neutralised_laps:
        lines.append(f"Neutralised laps: {', '.join(map(str, sorted(report.neutralised_laps)))}")
    ref = report.reference
    if ref is not None and ref.median is not None:
        lines.append(f"Reference: {ref.driver.code} (P{ref.position}) {_seconds(ref.median)}")
    for title, group in (("Leaders", report.leaders), ("Favourites", report.favourites)):
        lines += ["", title]
        for p in group:
            position = f"P{p.position}" if p.position else "NC"
            median = _seconds(p.median) if p.median is not None else "n/a"
            lines.append(
                f"  {position:<4}{p.driver.code} {p.driver.team:<16} {median}  "
                f"{_delta(report.delta(p)):>8}  ({p.laps} laps)"
            )
    lines.append(f"Source: {openf1.SOURCE_NAME}.")
    return "\n".join(lines)

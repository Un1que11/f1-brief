"""Driver and constructor championship standings, and how they moved since the last round."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from f1brief import jolpica


class StandingsError(jolpica.JolpicaError):
    """Standings could not be fetched or understood."""


@dataclass(frozen=True)
class Team:
    id: str
    name: str


@dataclass(frozen=True)
class Standing:
    """One row of a championship table: a driver or a constructor."""

    id: str
    name: str
    # None when the API gives no numeric position (e.g. positionText "-" or "D").
    position: int | None
    position_text: str
    # Decimal: half points have been awarded before.
    points: Decimal
    wins: int
    # Drivers only: every team the driver has scored for this season. The API lists them in
    # alphabetical order, not chronologically, so this does not say which team is current
    # (e.g. Lawson 2026: rb, then red_bull, then rb again is listed as rb, red_bull).
    teams: tuple[Team, ...] = ()
    code: str | None = None


@dataclass(frozen=True)
class Standings:
    season: int
    round: int
    drivers: tuple[Standing, ...]
    constructors: tuple[Standing, ...]


@dataclass(frozen=True)
class StandingChange:
    current: Standing
    # None when the entry was not in the previous table (first round, or a new driver).
    previous: Standing | None

    @property
    def points_gained(self) -> Decimal:
        return self.current.points - (self.previous.points if self.previous else Decimal(0))

    @property
    def positions_gained(self) -> int | None:
        """Positive when moving up the table; None if either position is unknown."""
        if self.previous is None or self.previous.position is None:
            return None
        if self.current.position is None:
            return None
        return self.previous.position - self.current.position


@dataclass(frozen=True)
class StandingsReport:
    season: int
    round: int
    # None when there is no earlier round to compare with.
    previous_round: int | None
    drivers: tuple[StandingChange, ...]
    constructors: tuple[StandingChange, ...]


def _decimal(raw: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"invalid points value {raw!r}") from exc


def _position(raw: Mapping[str, Any]) -> int | None:
    return int(raw["position"]) if "position" in raw else None


def _parse_driver(raw: Mapping[str, Any]) -> Standing:
    driver = raw["Driver"]
    return Standing(
        id=driver["driverId"],
        name=f"{driver['givenName']} {driver['familyName']}",
        position=_position(raw),
        position_text=raw["positionText"],
        points=_decimal(raw["points"]),
        wins=int(raw["wins"]),
        teams=tuple(
            Team(id=c["constructorId"], name=c["name"]) for c in raw.get("Constructors") or []
        ),
        code=driver.get("code"),
    )


def _parse_constructor(raw: Mapping[str, Any]) -> Standing:
    constructor = raw["Constructor"]
    return Standing(
        id=constructor["constructorId"],
        name=constructor["name"],
        position=_position(raw),
        position_text=raw["positionText"],
        points=_decimal(raw["points"]),
        wins=int(raw["wins"]),
    )


def _parse_table(
    payload: Mapping[str, Any], key: str, parse_row: Callable[[Mapping[str, Any]], Standing]
) -> tuple[int, int, list[Standing]]:
    try:
        lists = payload["MRData"]["StandingsTable"]["StandingsLists"]
        if not lists:
            raise StandingsError("no standings published for the requested round")
        table = lists[0]
        rows = [parse_row(row) for row in table[key]]
        return int(table["season"]), int(table["round"]), rows
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise StandingsError(f"unexpected standings response: {exc!r}") from exc


def parse_driver_standings(payload: Mapping[str, Any]) -> tuple[int, int, list[Standing]]:
    """Returns (season, round, rows)."""
    return _parse_table(payload, "DriverStandings", _parse_driver)


def parse_constructor_standings(payload: Mapping[str, Any]) -> tuple[int, int, list[Standing]]:
    """Returns (season, round, rows)."""
    return _parse_table(payload, "ConstructorStandings", _parse_constructor)


def combine(driver_payload: Mapping[str, Any], constructor_payload: Mapping[str, Any]) -> Standings:
    season, round_, drivers = parse_driver_standings(driver_payload)
    c_season, c_round, constructors = parse_constructor_standings(constructor_payload)
    if (season, round_) != (c_season, c_round):
        # Jolpica updates the two tables separately; never mix rounds.
        raise StandingsError(
            f"driver standings are for {season} round {round_} but constructor standings "
            f"are for {c_season} round {c_round}"
        )
    return Standings(season, round_, tuple(drivers), tuple(constructors))


def fetch_standings(
    season: int | str = "current",
    round_: int | None = None,
    client: httpx.Client | None = None,
) -> Standings:
    """Standings after `round_`, or after the latest completed round when omitted."""
    base = f"{season}/{round_}" if round_ is not None else str(season)
    try:
        drivers = jolpica.get_json(f"{base}/driverStandings", client)
        constructors = jolpica.get_json(f"{base}/constructorStandings", client)
    except jolpica.JolpicaError as exc:
        raise StandingsError(str(exc)) from exc
    return combine(drivers, constructors)


def compare(
    current: Sequence[Standing], previous: Sequence[Standing]
) -> tuple[StandingChange, ...]:
    """Pairs every current row with the same entry (by id) in the previous table."""
    by_id = {s.id: s for s in previous}
    return tuple(StandingChange(current=s, previous=by_id.get(s.id)) for s in current)


def report(current: Standings, previous: Standings | None) -> StandingsReport:
    if previous is not None and (previous.season, previous.round) >= (
        current.season,
        current.round,
    ):
        raise ValueError("previous standings must be from an earlier round")
    return StandingsReport(
        season=current.season,
        round=current.round,
        previous_round=previous.round if previous else None,
        drivers=compare(current.drivers, previous.drivers if previous else ()),
        constructors=compare(current.constructors, previous.constructors if previous else ()),
    )


def fetch_report(
    season: int | str = "current", client: httpx.Client | None = None
) -> StandingsReport:
    """Latest standings plus the changes since the round before."""
    current = fetch_standings(season, client=client)
    previous = None
    if current.round > 1:
        previous = fetch_standings(current.season, current.round - 1, client=client)
    return report(current, previous)


def _format_movement(change: StandingChange) -> str:
    moved = change.positions_gained
    if change.previous is None:
        return "new"
    if moved is None:
        return ""
    if moved > 0:
        return f"up {moved}"
    if moved < 0:
        return f"down {-moved}"
    return "="


def format_points(points: Decimal) -> str:
    # 25 -> "25", 0.5 -> "0.5"; never scientific notation.
    return format(points.normalize(), "f")


def format_report(rep: StandingsReport) -> str:
    since = f"since round {rep.previous_round}" if rep.previous_round else "first round"
    lines = [f"Standings after round {rep.round} ({rep.season}), changes {since}", "", "Drivers"]
    for change in rep.drivers:
        s = change.current
        lines.append(
            f"  {s.position_text:>2}. {s.name:<24} {' / '.join(t.name for t in s.teams):<26}"
            f" {format_points(s.points):>5} pts  +{format_points(change.points_gained):<4}"
            f" {_format_movement(change)}".rstrip()
        )
    lines += ["", "Constructors"]
    for change in rep.constructors:
        s = change.current
        lines.append(
            f"  {s.position_text:>2}. {s.name:<24}"
            f" {format_points(s.points):>5} pts  +{format_points(change.points_gained):<4}"
            f" {_format_movement(change)}".rstrip()
        )
    lines.append(f"Source: {jolpica.SOURCE_NAME}.")
    return "\n".join(lines)

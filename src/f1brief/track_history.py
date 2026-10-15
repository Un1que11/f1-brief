"""The favourites' results at a circuit over its last few editions, from Jolpica."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from f1brief import jolpica
from f1brief.form import (
    FormError,
    QualifyingResult,
    RaceResult,
    average,
    build_rounds,
    format_decimal,
)
from f1brief.standings import format_points

DEFAULT_EDITIONS = 3
FAVOURITE_TEAM = "mclaren"
FAVOURITE_DRIVERS = ("russell", "ocon")


@dataclass(frozen=True)
class Circuit:
    id: str
    name: str
    locality: str
    country: str


@dataclass(frozen=True)
class EditionEntry:
    race: RaceResult
    qualifying: QualifyingResult | None


@dataclass(frozen=True)
class Edition:
    season: int
    round: int
    race_name: str
    # Favourites only, in finishing order.
    entries: tuple[EditionEntry, ...]


@dataclass(frozen=True)
class DriverAtTrack:
    driver_id: str
    driver_name: str
    # Team names per edition the driver started, oldest first.
    teams: tuple[str, ...]
    starts: int
    points: Decimal
    best_finish: RaceResult | None
    average_finish: Decimal | None
    average_qualifying: Decimal | None
    non_classified: int


@dataclass(frozen=True)
class TrackHistory:
    circuit: Circuit
    # Oldest first.
    editions: tuple[Edition, ...]

    def team_points(self, constructor_id: str) -> list[Decimal]:
        """Grand Prix points for the team at each edition, oldest first."""
        return [
            sum(
                (e.race.points for e in ed.entries if e.race.constructor_id == constructor_id),
                Decimal(0),
            )
            for ed in self.editions
        ]

    def drivers(self) -> list[DriverAtTrack]:
        """Per-driver summary, in order of first appearance."""
        order: list[str] = []
        for ed in self.editions:
            for e in ed.entries:
                if e.race.driver_id not in order:
                    order.append(e.race.driver_id)
        summaries = []
        for driver_id in order:
            entries = [
                e for ed in self.editions for e in ed.entries if e.race.driver_id == driver_id
            ]
            classified = [e.race for e in entries if e.race.classified]
            summaries.append(
                DriverAtTrack(
                    driver_id=driver_id,
                    driver_name=entries[0].race.driver_name,
                    teams=tuple(e.race.constructor_name for e in entries),
                    starts=len(entries),
                    points=sum((e.race.points for e in entries), Decimal(0)),
                    best_finish=min(classified, key=lambda r: r.position) if classified else None,
                    average_finish=average(r.position for r in classified),
                    average_qualifying=average(
                        e.qualifying.position for e in entries if e.qualifying
                    ),
                    non_classified=len(entries) - len(classified),
                )
            )
        return summaries


def past_editions(
    races_payload: dict[str, Any], before: date, count: int = DEFAULT_EDITIONS
) -> tuple[Circuit, list[tuple[int, int]]]:
    """The circuit and the (season, round) of its last `count` races held before `before`."""
    try:
        data = races_payload["MRData"]
        races = data["RaceTable"]["Races"]
        if int(data["total"]) > len(races):
            raise FormError(f"response truncated: {len(races)} of {data['total']} races")
        if not races:
            raise FormError("no races found for this circuit")
        raw = races[0]["Circuit"]
        circuit = Circuit(
            id=raw["circuitId"],
            name=raw["circuitName"],
            locality=raw["Location"]["locality"],
            country=raw["Location"]["country"],
        )
        held = sorted(
            (int(r["season"]), int(r["round"]))
            for r in races
            if date.fromisoformat(r["date"]) < before
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FormError(f"unexpected circuit races response: {exc!r}") from exc
    return circuit, held[-count:] if count > 0 else []


def build_edition(
    results_payload: dict[str, Any],
    qualifying_payload: dict[str, Any],
    favourite_team: str = FAVOURITE_TEAM,
    favourite_drivers: Sequence[str] = FAVOURITE_DRIVERS,
) -> Edition:
    rounds = build_rounds(results_payload, qualifying_payload)
    if len(rounds) != 1:
        raise FormError(f"expected results for one race, got {len(rounds)}")
    (r,) = rounds
    quali = {q.driver_id: q for q in r.qualifying}
    entries = tuple(
        EditionEntry(race=x, qualifying=quali.get(x.driver_id))
        for x in r.race
        if x.constructor_id == favourite_team or x.driver_id in favourite_drivers
    )
    return Edition(season=r.season, round=r.round, race_name=r.race_name, entries=entries)


def fetch_track_history(
    circuit_id: str,
    before: date,
    count: int = DEFAULT_EDITIONS,
    favourite_team: str = FAVOURITE_TEAM,
    favourite_drivers: Sequence[str] = FAVOURITE_DRIVERS,
    client: httpx.Client | None = None,
) -> TrackHistory:
    """The favourites at the circuit's last `count` races before `before` (1 + 2 per race
    requests)."""
    if count < 1:
        raise ValueError("count must be at least 1")

    def get(path: str) -> dict[str, Any]:
        try:
            return jolpica.get_json(path, client)
        except jolpica.JolpicaError as exc:
            raise FormError(str(exc)) from exc

    circuit, editions = past_editions(get(f"circuits/{circuit_id}/races"), before, count)
    return TrackHistory(
        circuit=circuit,
        editions=tuple(
            build_edition(
                get(f"{season}/{round_}/results"),
                get(f"{season}/{round_}/qualifying"),
                favourite_team,
                favourite_drivers,
            )
            for season, round_ in editions
        ),
    )


def _finish(result: RaceResult) -> str:
    return f"P{result.position_text}" if result.classified else result.status


def format_track_history(
    history: TrackHistory, team_id: str = FAVOURITE_TEAM, team_name: str = "McLaren"
) -> str:
    c = history.circuit
    if not history.editions:
        return f"{c.name}: no earlier races found."
    seasons = ", ".join(str(ed.season) for ed in history.editions)
    lines = [f"Favourites at {c.name} ({c.locality}, {c.country}): {seasons}", ""]
    for ed in history.editions:
        lines.append(f"  {ed.season} {ed.race_name}")
        for e in ed.entries:
            r = e.race
            quali = f"P{e.qualifying.position}" if e.qualifying else "-"
            grid = f"P{r.grid}" if r.grid else "pit lane"
            lines.append(
                f"      {r.driver_code or r.driver_id:<4}{r.constructor_name:<18} quali {quali:<4}"
                f" grid {grid:<9} {_finish(r):<10} {format_points(r.points)} pts"
            )
    points = history.team_points(team_id)
    lines += [
        "",
        f"{team_name} Grand Prix points (sprints excluded): "
        + " / ".join(format_points(p) for p in points)
        + f" (total {format_points(sum(points, Decimal(0)))})",
    ]
    for d in history.drivers():
        best = _finish(d.best_finish) if d.best_finish else "-"
        teams = ", ".join(dict.fromkeys(d.teams))
        lines.append(
            f"  {d.driver_name} ({teams}): {d.starts} starts, best {best},"
            f" avg finish {format_decimal(d.average_finish)}, avg quali"
            f" {format_decimal(d.average_qualifying)}, {format_points(d.points)} pts,"
            f" not classified {d.non_classified}"
        )
    lines.append(f"Source: {jolpica.SOURCE_NAME}.")
    return "\n".join(lines)

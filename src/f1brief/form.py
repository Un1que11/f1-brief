"""Recent form: race and qualifying positions over the last N rounds, from Jolpica."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from f1brief import jolpica
from f1brief.standings import format_points

DEFAULT_ROUNDS = 5


class FormError(jolpica.JolpicaError):
    """Results could not be fetched or understood."""


@dataclass(frozen=True)
class RaceResult:
    driver_id: str
    driver_code: str | None
    driver_name: str
    constructor_id: str
    constructor_name: str
    # Starting position; None for a pit-lane start (the API reports grid "0").
    grid: int | None
    # Classification order. Present for every entry, including retirements.
    position: int
    # "1".."20" when classified; otherwise a code: R retired, W withdrawn / did not start,
    # D disqualified, E excluded, N not classified, F failed to qualify.
    position_text: str
    status: str
    # Grand Prix points only: sprints are a separate endpoint.
    points: Decimal

    @property
    def classified(self) -> bool:
        return self.position_text.isdigit()


@dataclass(frozen=True)
class QualifyingResult:
    driver_id: str
    driver_code: str | None
    driver_name: str
    constructor_id: str
    constructor_name: str
    position: int
    # Lap times as published ("1:31.712"); None when the driver did not take part in that part.
    q1: str | None
    q2: str | None
    q3: str | None


@dataclass(frozen=True)
class RoundForm:
    season: int
    round: int
    race_name: str
    race: tuple[RaceResult, ...]
    qualifying: tuple[QualifyingResult, ...]

    @property
    def points(self) -> Decimal:
        return sum((r.points for r in self.race), Decimal(0))

    @property
    def best_finish(self) -> RaceResult | None:
        classified = [r for r in self.race if r.classified]
        return min(classified, key=lambda r: r.position) if classified else None

    @property
    def best_qualifying(self) -> QualifyingResult | None:
        return min(self.qualifying, key=lambda q: q.position) if self.qualifying else None


@dataclass(frozen=True)
class DriverSummary:
    driver_id: str
    driver_name: str
    starts: int
    points: Decimal
    average_qualifying: Decimal | None
    average_finish: Decimal | None
    non_classified: int


@dataclass(frozen=True)
class TeamForm:
    constructor_id: str
    constructor_name: str
    # Oldest first.
    rounds: tuple[RoundForm, ...]

    @property
    def points(self) -> Decimal:
        return sum((r.points for r in self.rounds), Decimal(0))

    @property
    def average_best_qualifying(self) -> Decimal | None:
        return _average(q.position for r in self.rounds if (q := r.best_qualifying))

    @property
    def average_finish(self) -> Decimal | None:
        """Mean finishing position over all classified results of both cars."""
        return _average(x.position for r in self.rounds for x in r.race if x.classified)

    @property
    def non_classified(self) -> int:
        return sum(1 for r in self.rounds for x in r.race if not x.classified)

    def drivers(self) -> list[DriverSummary]:
        """Per-driver summary, most points first."""
        ids: dict[str, str] = {}
        for r in self.rounds:
            for x in r.race:
                ids.setdefault(x.driver_id, x.driver_name)
        summaries = []
        for driver_id, name in ids.items():
            races = [x for r in self.rounds for x in r.race if x.driver_id == driver_id]
            quali = [q for r in self.rounds for q in r.qualifying if q.driver_id == driver_id]
            summaries.append(
                DriverSummary(
                    driver_id=driver_id,
                    driver_name=name,
                    starts=len(races),
                    points=sum((x.points for x in races), Decimal(0)),
                    average_qualifying=_average(q.position for q in quali),
                    average_finish=_average(x.position for x in races if x.classified),
                    non_classified=sum(1 for x in races if not x.classified),
                )
            )
        return sorted(summaries, key=lambda s: s.points, reverse=True)


def _average(values: Iterable[int]) -> Decimal | None:
    values = list(values)
    if not values:
        return None
    mean = Decimal(sum(values)) / Decimal(len(values))
    return mean.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _driver_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    driver = raw["Driver"]
    return {
        "driver_id": driver["driverId"],
        "driver_code": driver.get("code"),
        "driver_name": f"{driver['givenName']} {driver['familyName']}",
        "constructor_id": raw["Constructor"]["constructorId"],
        "constructor_name": raw["Constructor"]["name"],
    }


def _parse_race_result(raw: Mapping[str, Any]) -> RaceResult:
    grid = int(raw["grid"])
    return RaceResult(
        **_driver_fields(raw),
        grid=grid or None,
        position=int(raw["position"]),
        position_text=raw["positionText"],
        status=raw["status"],
        points=Decimal(raw["points"]),
    )


def _parse_qualifying_result(raw: Mapping[str, Any]) -> QualifyingResult:
    return QualifyingResult(
        **_driver_fields(raw),
        position=int(raw["position"]),
        q1=raw.get("Q1") or None,
        q2=raw.get("Q2") or None,
        q3=raw.get("Q3") or None,
    )


def _races(payload: Mapping[str, Any], rows_key: str) -> list[Mapping[str, Any]]:
    """The Races list, after checking that the page holds every row."""
    data = payload["MRData"]
    races = data["RaceTable"]["Races"]
    rows = sum(len(r[rows_key]) for r in races)
    if int(data["total"]) > rows:
        # Jolpica pages by result row; a cut-off page would silently drop rounds.
        raise FormError(f"response truncated: {rows} of {data['total']} rows")
    return races


def parse_results(
    payload: Mapping[str, Any],
) -> dict[tuple[int, int], tuple[str, list[RaceResult]]]:
    """(season, round) -> (race name, race results) from a `.../results` response."""
    try:
        return {
            (int(r["season"]), int(r["round"])): (
                r["raceName"],
                [_parse_race_result(x) for x in r["Results"]],
            )
            for r in _races(payload, "Results")
        }
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise FormError(f"unexpected results response: {exc!r}") from exc


def parse_qualifying(payload: Mapping[str, Any]) -> dict[tuple[int, int], list[QualifyingResult]]:
    """(season, round) -> qualifying results from a `.../qualifying` response."""
    try:
        return {
            (int(r["season"]), int(r["round"])): [
                _parse_qualifying_result(x) for x in r["QualifyingResults"]
            ]
            for r in _races(payload, "QualifyingResults")
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise FormError(f"unexpected qualifying response: {exc!r}") from exc


def build_rounds(
    results_payload: Mapping[str, Any], qualifying_payload: Mapping[str, Any]
) -> list[RoundForm]:
    """Rounds with a race result, oldest first; qualifying attached where available."""
    results = parse_results(results_payload)
    qualifying = parse_qualifying(qualifying_payload)
    return [
        RoundForm(
            season=season,
            round=round_,
            race_name=name,
            race=tuple(sorted(race, key=lambda x: x.position)),
            qualifying=tuple(
                sorted(qualifying.get((season, round_), []), key=lambda q: q.position)
            ),
        )
        for (season, round_), (name, race) in sorted(results.items())
    ]


def _season_of(payload: Mapping[str, Any]) -> int:
    try:
        return int(payload["MRData"]["RaceTable"]["season"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FormError("response has no season") from exc


def _constructor_name(rounds: Sequence[RoundForm], constructor_id: str) -> str:
    for r in rounds:
        for x in r.race:
            if x.constructor_id == constructor_id:
                return x.constructor_name
    return constructor_id


def fetch_team_form(
    constructor_id: str = "mclaren",
    last: int = DEFAULT_ROUNDS,
    season: int | str = "current",
    client: httpx.Client | None = None,
) -> TeamForm:
    """The constructor's last `last` completed rounds. Early in a season this reaches back into
    the previous one, so there are always `last` rounds when that much history exists."""
    if last < 1:
        raise ValueError("last must be at least 1")

    def fetch(season_: int | str) -> tuple[int, list[RoundForm]]:
        try:
            results = jolpica.get_json(f"{season_}/constructors/{constructor_id}/results", client)
            quali = jolpica.get_json(f"{season_}/constructors/{constructor_id}/qualifying", client)
        except jolpica.JolpicaError as exc:
            raise FormError(str(exc)) from exc
        return _season_of(results), build_rounds(results, quali)

    year, rounds = fetch(season)
    if len(rounds) < last and year > 1950:
        _, earlier = fetch(year - 1)
        rounds = earlier + rounds
    rounds = rounds[-last:]
    return TeamForm(constructor_id, _constructor_name(rounds, constructor_id), tuple(rounds))


def _format_decimal(value: Decimal | None) -> str:
    return "-" if value is None else format(value, "f")


def _format_finish(result: RaceResult) -> str:
    return f"P{result.position_text}" if result.classified else f"{result.status}"


def format_team_form(form: TeamForm) -> str:
    if not form.rounds:
        return f"{form.constructor_name}: no completed rounds."
    first, last = form.rounds[0], form.rounds[-1]
    span = f"{first.season} R{first.round} to {last.season} R{last.round}"
    lines = [f"{form.constructor_name} form, last {len(form.rounds)} rounds ({span})", ""]
    for r in form.rounds:
        quali = ", ".join(f"{q.driver_code or q.driver_id} P{q.position}" for q in r.qualifying)
        race = ", ".join(
            f"{x.driver_code or x.driver_id} {_format_finish(x)}"
            + (f" (from P{x.grid})" if x.grid else " (pit lane)")
            for x in r.race
        )
        lines.append(f"  R{r.round:<2} {r.race_name}")
        lines.append(f"      Qualifying: {quali or 'no data'}")
        lines.append(f"      Race:       {race}  {format_points(r.points)} pts")
    lines += [
        "",
        f"Grand Prix points (sprints excluded): {format_points(form.points)}",
        f"Average best qualifying position: {_format_decimal(form.average_best_qualifying)}",
        f"Average finish (classified cars): {_format_decimal(form.average_finish)}",
        f"Not classified: {form.non_classified}",
    ]
    for d in form.drivers():
        lines.append(
            f"  {d.driver_name}: {format_points(d.points)} pts, avg quali "
            f"{_format_decimal(d.average_qualifying)}, avg finish "
            f"{_format_decimal(d.average_finish)}, not classified {d.non_classified}"
        )
    lines.append(f"Source: {jolpica.SOURCE_NAME}.")
    return "\n".join(lines)

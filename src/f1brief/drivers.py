"""Driver form over the last N rounds, compared with the teammate in the same car each round."""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

import httpx

from f1brief import jolpica
from f1brief.form import (
    DEFAULT_ROUNDS,
    FormError,
    QualifyingResult,
    RaceResult,
    RoundForm,
    average,
    fetch_constructor_rounds,
    format_decimal,
    parse_results,
    season_of,
)
from f1brief.standings import format_points

FAVOURITES = ("russell", "ocon")


@dataclass(frozen=True)
class RoundComparison:
    season: int
    round: int
    race_name: str
    constructor_id: str
    constructor_name: str
    race: RaceResult
    teammate_race: RaceResult | None
    qualifying: QualifyingResult | None
    teammate_qualifying: QualifyingResult | None

    @property
    def qualified_ahead(self) -> bool | None:
        """None when either driver has no qualifying result that round."""
        if self.qualifying is None or self.teammate_qualifying is None:
            return None
        return self.qualifying.position < self.teammate_qualifying.position

    @property
    def finished_ahead(self) -> bool | None:
        """Race head-to-head: a classified finish beats a non-classified one; between two
        classified cars the better position wins. None if neither was classified (or the
        teammate is unknown)."""
        mate = self.teammate_race
        if mate is None or (not self.race.classified and not mate.classified):
            return None
        if self.race.classified != mate.classified:
            return self.race.classified
        return self.race.position < mate.position


@dataclass(frozen=True)
class HeadToHead:
    ahead: int
    behind: int
    # Rounds where the comparison could not be made.
    undecided: int


@dataclass(frozen=True)
class DriverForm:
    driver_id: str
    driver_name: str
    # Oldest first.
    rounds: tuple[RoundComparison, ...]

    @property
    def points(self) -> Decimal:
        return sum((r.race.points for r in self.rounds), Decimal(0))

    @property
    def teammate_points(self) -> Decimal:
        return sum((r.teammate_race.points for r in self.rounds if r.teammate_race), Decimal(0))

    @property
    def average_qualifying(self) -> Decimal | None:
        return average(r.qualifying.position for r in self.rounds if r.qualifying)

    @property
    def average_finish(self) -> Decimal | None:
        return average(r.race.position for r in self.rounds if r.race.classified)

    @property
    def non_classified(self) -> int:
        return sum(1 for r in self.rounds if not r.race.classified)

    @property
    def qualifying_head_to_head(self) -> HeadToHead:
        return _tally(r.qualified_ahead for r in self.rounds)

    @property
    def race_head_to_head(self) -> HeadToHead:
        return _tally(r.finished_ahead for r in self.rounds)

    def teammates(self) -> list[str]:
        """Teammate names in order of first appearance (more than one after a team change)."""
        names: list[str] = []
        for r in self.rounds:
            if r.teammate_race and r.teammate_race.driver_name not in names:
                names.append(r.teammate_race.driver_name)
        return names


def _tally(outcomes: Iterable[bool | None]) -> HeadToHead:
    outcomes = list(outcomes)
    return HeadToHead(
        ahead=sum(1 for o in outcomes if o is True),
        behind=sum(1 for o in outcomes if o is False),
        undecided=sum(1 for o in outcomes if o is None),
    )


def compare_rounds(
    driver_id: str,
    driver_results: dict[tuple[int, int], tuple[str, list[RaceResult]]],
    team_rounds: dict[tuple[int, str], list[RoundForm]],
) -> list[RoundComparison]:
    """Pairs each of the driver's races with the same round of the team they drove for.

    `team_rounds` maps (season, constructor_id) to that constructor's rounds.
    """
    comparisons = []
    for (season, round_), (race_name, results) in sorted(driver_results.items()):
        own = next((r for r in results if r.driver_id == driver_id), None)
        if own is None:
            raise FormError(f"{driver_id} missing from their own results in {season} R{round_}")
        team_round = next(
            (r for r in team_rounds.get((season, own.constructor_id), []) if r.round == round_),
            None,
        )
        race_mates = [x for x in team_round.race if x.driver_id != driver_id] if team_round else []
        quali = list(team_round.qualifying) if team_round else []
        own_quali = next((q for q in quali if q.driver_id == driver_id), None)
        mate = race_mates[0] if race_mates else None
        mate_quali = next((q for q in quali if mate and q.driver_id == mate.driver_id), None)
        comparisons.append(
            RoundComparison(
                season=season,
                round=round_,
                race_name=race_name,
                constructor_id=own.constructor_id,
                constructor_name=own.constructor_name,
                race=own,
                teammate_race=mate,
                qualifying=own_quali,
                teammate_qualifying=mate_quali,
            )
        )
    return comparisons


def fetch_driver_form(
    driver_id: str,
    last: int = DEFAULT_ROUNDS,
    season: int | str = "current",
    client: httpx.Client | None = None,
) -> DriverForm:
    """The driver's last `last` races with teammate comparison, reaching back into the
    previous season when the current one is shorter than `last`."""
    if last < 1:
        raise ValueError("last must be at least 1")

    def driver_season(
        season_: int | str,
    ) -> tuple[int, dict[tuple[int, int], tuple[str, list[RaceResult]]]]:
        try:
            payload = jolpica.get_json(f"{season_}/drivers/{driver_id}/results", client)
        except jolpica.JolpicaError as exc:
            raise FormError(str(exc)) from exc
        return season_of(payload), parse_results(payload)

    year, results = driver_season(season)
    if len(results) < last and year > 1950:
        _, earlier = driver_season(year - 1)
        results = {**earlier, **results}
    keep = sorted(results)[-last:]
    results = {key: results[key] for key in keep}

    teams = sorted(
        {
            (season_, x.constructor_id)
            for (season_, _), (_, rows) in results.items()
            for x in rows
            if x.driver_id == driver_id
        }
    )
    team_rounds = {
        (season_, team): fetch_constructor_rounds(team, season_, client)[1]
        for season_, team in teams
    }
    rounds = compare_rounds(driver_id, results, team_rounds)
    name = rounds[0].race.driver_name if rounds else driver_id
    return DriverForm(driver_id, name, tuple(rounds))


def _h2h(h: HeadToHead) -> str:
    text = f"{h.ahead}-{h.behind}"
    return f"{text} ({h.undecided} undecided)" if h.undecided else text


def _finish(result: RaceResult | None) -> str:
    if result is None:
        return "-"
    return f"P{result.position_text}" if result.classified else result.status


def format_driver_form(form: DriverForm) -> str:
    if not form.rounds:
        return f"{form.driver_name}: no races in the selected period."
    first, last = form.rounds[0], form.rounds[-1]
    span = f"{first.season} R{first.round} to {last.season} R{last.round}"
    mates = ", ".join(form.teammates()) or "none"
    lines = [
        f"{form.driver_name}, last {len(form.rounds)} races ({span}), vs teammate ({mates})",
        "",
    ]
    for r in form.rounds:
        mate = r.teammate_race
        mate_code = (mate.driver_code or mate.driver_id) if mate else "-"
        q = f"P{r.qualifying.position}" if r.qualifying else "-"
        mq = f"P{r.teammate_qualifying.position}" if r.teammate_qualifying else "-"
        lines.append(
            f"  R{r.round:<2} {r.race_name} ({r.constructor_name}): quali {q} vs {mate_code} {mq}; "
            f"race {_finish(r.race)} vs {_finish(mate)}; {format_points(r.race.points)} pts"
        )
    q_h2h, r_h2h = form.qualifying_head_to_head, form.race_head_to_head
    lines += [
        "",
        f"Grand Prix points (sprints excluded): {format_points(form.points)}"
        f" (teammate {format_points(form.teammate_points)})",
        f"Qualifying head-to-head: {_h2h(q_h2h)}",
        f"Race head-to-head: {_h2h(r_h2h)}",
        f"Average qualifying position: {format_decimal(form.average_qualifying)}",
        f"Average finish (classified): {format_decimal(form.average_finish)}",
        f"Not classified: {form.non_classified}",
        f"Source: {jolpica.SOURCE_NAME}.",
    ]
    return "\n".join(lines)

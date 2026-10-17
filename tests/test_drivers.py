from dataclasses import replace
from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import load_fixture

from f1brief.drivers import (
    DriverForm,
    HeadToHead,
    RoundComparison,
    compare_rounds,
    fetch_driver_form,
    format_driver_form,
    share_head_to_heads,
)
from f1brief.form import FormError, build_rounds, parse_results

# Recorded 2026-10-15 (R1-R17): /2026/drivers/{id}/results and /2026/constructors/{id}/...
DRIVER_FIXTURES = {d: f"jolpica/{d}_results_2026.json" for d in ("russell", "ocon", "lawson")}
# Bearman drove only for Haas, so the Haas results stand in for his own.
DRIVER_FIXTURES["bearman"] = "jolpica/haas_results_2026.json"
TEAMS = ("mercedes", "haas", "rb", "red_bull")


def team_rounds_2026() -> dict[tuple[int, str], list]:
    return {
        (2026, team): build_rounds(
            load_fixture(f"jolpica/{team}_results_2026.json"),
            load_fixture(f"jolpica/{team}_qualifying_2026.json"),
        )
        for team in TEAMS
    }


def form_for(driver_id: str, last: int) -> DriverForm:
    results = parse_results(load_fixture(DRIVER_FIXTURES[driver_id]))
    keep = sorted(results)[-last:]
    rounds = compare_rounds(driver_id, {k: results[k] for k in keep}, team_rounds_2026())
    return DriverForm(driver_id, rounds[0].race.driver_name, tuple(rounds))


def by_round(form: DriverForm) -> dict[int, RoundComparison]:
    return {r.round: r for r in form.rounds}


def test_russell_against_antonelli_last_five() -> None:
    form = form_for("russell", 5)

    assert [r.round for r in form.rounds] == [13, 14, 15, 16, 17]
    assert form.teammates() == ["Andrea Kimi Antonelli"]
    # Quali 2/7, 6/2, 1/16, 8/4, 6/4 -> ahead in R13 and R15.
    assert form.qualifying_head_to_head == HeadToHead(ahead=2, behind=3, undecided=0)
    # Race 2/1, 5/1, 1/5, 20/2, 9/3 -> ahead only in R15.
    assert form.race_head_to_head == HeadToHead(ahead=1, behind=4, undecided=0)
    # 18 + 10 + 25 + 0 + 2 vs 25 + 25 + 10 + 18 + 15.
    assert (form.points, form.teammate_points) == (Decimal("55"), Decimal("93"))
    # Quali 2, 6, 1, 8, 6 -> 4.6; finishes 2, 5, 1, 20, 9 -> 7.4.
    assert form.average_qualifying == Decimal("4.6")
    assert form.average_finish == Decimal("7.4")
    assert form.non_classified == 0


def test_ocon_against_bearman_with_missing_teammate_qualifying() -> None:
    form = form_for("ocon", 5)
    r14 = by_round(form)[14]

    # R14: Bearman has no qualifying result, so that round is undecided.
    assert r14.teammate_qualifying is None
    assert r14.qualified_ahead is None
    # Quali 16/12, 13/-, 14/11, 19/18, 13/12.
    assert form.qualifying_head_to_head == HeadToHead(ahead=0, behind=4, undecided=1)
    # Race 16/15, 11/16, 8/9, 15/14, 13/12.
    assert form.race_head_to_head == HeadToHead(ahead=2, behind=3, undecided=0)
    assert (form.points, form.teammate_points) == (Decimal("4"), Decimal("2"))
    # Quali 16, 13, 14, 19, 13 -> 15.0; finishes 16, 11, 8, 15, 13 -> 12.6.
    assert form.average_qualifying == Decimal("15.0")
    assert form.average_finish == Decimal("12.6")


def test_team_change_compares_against_the_teammate_of_each_round() -> None:
    # Lawson 2026: RB (R1-11), Red Bull (R12-14), RB (R15-).
    form = form_for("lawson", 7)
    rounds = by_round(form)

    assert form.teammates() == ["Arvid Lindblad", "Max Verstappen"]
    assert [(r.round, r.constructor_id) for r in form.rounds] == [
        (11, "rb"),
        (12, "red_bull"),
        (13, "red_bull"),
        (14, "red_bull"),
        (15, "rb"),
        (16, "rb"),
        (17, "rb"),
    ]
    assert rounds[12].teammate_race.driver_id == "max_verstappen"
    assert rounds[15].teammate_race.driver_id == "arvid_lindblad"
    # R12: Lawson P7, Verstappen retired -> classified beats non-classified.
    assert rounds[12].finished_ahead is True
    # Race 8/10, 7/R, 14/3, 6/2, 12/7, 7/10, 7/15 -> 4-3. Quali 11/9, 8/7, 14/6, 8/3,
    # 12/15, 11/16, 9/10 -> 3-4.
    assert form.race_head_to_head == HeadToHead(ahead=4, behind=3, undecided=0)
    assert form.qualifying_head_to_head == HeadToHead(ahead=3, behind=4, undecided=0)
    assert form.points == Decimal("30")


def test_race_head_to_head_rules() -> None:
    form = form_for("russell", 1)
    base = form.rounds[0]
    retired = replace(base.race, position_text="R", status="Retired")
    mate_retired = replace(base.teammate_race, position_text="R", status="Retired")

    assert replace(base, race=retired).finished_ahead is False
    assert replace(base, teammate_race=mate_retired).finished_ahead is True
    assert replace(base, race=retired, teammate_race=mate_retired).finished_ahead is None
    assert replace(base, teammate_race=None).finished_ahead is None


def test_round_missing_from_team_data_has_no_comparison() -> None:
    results = parse_results(load_fixture(DRIVER_FIXTURES["russell"]))
    last = {max(results): results[max(results)]}

    (round_,) = compare_rounds("russell", last, {})

    assert round_.teammate_race is None
    assert round_.qualifying is None
    assert round_.qualified_ahead is None


def test_driver_missing_from_own_results_raises() -> None:
    results = parse_results(load_fixture(DRIVER_FIXTURES["russell"]))

    with pytest.raises(FormError, match="missing"):
        compare_rounds("ocon", results, team_rounds_2026())


def test_format_driver_form() -> None:
    text = format_driver_form(form_for("ocon", 5))

    assert text.splitlines()[0] == (
        "Esteban Ocon, last 5 races (2026 R13 to 2026 R17), vs teammate (Oliver Bearman)"
    )
    assert (
        "  R14 Spanish Grand Prix (Haas F1 Team): quali P13 vs BEA -; race P11 vs P16; 0 pts"
        in text
    )
    assert "Grand Prix points (sprints excluded): 4 (teammate 2)" in text
    assert "Qualifying head-to-head: 0-4 (1 undecided)" in text
    assert "Race head-to-head: 2-3" in text
    assert text.endswith("Source: Jolpica-F1.")


def test_teammate_favourites_share_one_head_to_head() -> None:
    ocon, russell, bearman = share_head_to_heads(
        [form_for("ocon", 5), form_for("russell", 5), form_for("bearman", 5)]
    )

    # Listed first, Ocon keeps the comparison with Bearman exactly as before.
    assert ocon == form_for("ocon", 5)
    assert russell == form_for("russell", 5)
    # Bearman's rounds against Ocon are marked shared and leave his head-to-head.
    assert all(r.shared for r in bearman.rounds)
    assert bearman.shared_with() == ["Esteban Ocon"]
    assert bearman.qualifying_head_to_head == HeadToHead(0, 0, 0)
    assert bearman.race_head_to_head == HeadToHead(0, 0, 0)
    assert bearman.teammate_points == Decimal(0)
    # His own results stay: 2 points over the five rounds (Ocon's teammate points above).
    assert bearman.points == Decimal("2")
    assert bearman.teammates() == ["Esteban Ocon"]


def test_sharing_depends_on_the_order_and_only_on_teammates() -> None:
    bearman, ocon = share_head_to_heads([form_for("bearman", 5), form_for("ocon", 5)])

    assert not any(r.shared for r in bearman.rounds)
    assert all(r.shared for r in ocon.rounds)
    # Lawson's teammates (Hadjar, Lindblad) are not favourites: nothing is shared.
    (lawson,) = share_head_to_heads([form_for("lawson", 7)])
    assert not any(r.shared for r in lawson.rounds)


def test_format_a_shared_head_to_head() -> None:
    _, bearman = share_head_to_heads([form_for("ocon", 5), form_for("bearman", 5)])
    text = format_driver_form(bearman)

    assert (
        "  R14 Spanish Grand Prix (Haas F1 Team): quali -; race P16 (vs OCO: see above); 0 pts"
        in text
    )
    assert "Qualifying head-to-head, shared rounds excluded: 0-0" in text
    assert "Head-to-head with Esteban Ocon (5 shared rounds): shown once, in their form above" in (
        text
    )


def test_format_retirement_and_empty_form() -> None:
    lawson = format_driver_form(form_for("lawson", 7))

    assert "race P7 vs Retired" in lawson
    assert format_driver_form(DriverForm("x", "X", ())) == "X: no races in the selected period."


def _transport(seen: list[str]) -> httpx.MockTransport:
    routes: dict[str, Any] = {
        f"/ergast/f1/current/drivers/{d}/results.json": load_fixture(f)
        for d, f in DRIVER_FIXTURES.items()
    }
    for team in TEAMS:
        for kind in ("results", "qualifying"):
            routes[f"/ergast/f1/2026/constructors/{team}/{kind}.json"] = load_fixture(
                f"jolpica/{team}_{kind}_2026.json"
            )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        body = routes.get(request.url.path)
        return httpx.Response(200, json=body) if body else httpx.Response(404)

    return httpx.MockTransport(handler)


def test_fetch_driver_form_requests_driver_then_each_team() -> None:
    seen: list[str] = []

    with httpx.Client(transport=_transport(seen)) as client:
        form = fetch_driver_form("lawson", last=7, client=client)

    assert form.driver_name == "Liam Lawson"
    assert len(form.rounds) == 7
    assert seen == [
        "/ergast/f1/current/drivers/lawson/results.json",
        "/ergast/f1/2026/constructors/rb/results.json",
        "/ergast/f1/2026/constructors/rb/qualifying.json",
        "/ergast/f1/2026/constructors/red_bull/results.json",
        "/ergast/f1/2026/constructors/red_bull/qualifying.json",
    ]


def test_fetch_driver_form_only_one_team_needed() -> None:
    seen: list[str] = []

    with httpx.Client(transport=_transport(seen)) as client:
        form = fetch_driver_form("lawson", last=3, client=client)

    assert {r.constructor_id for r in form.rounds} == {"rb"}
    assert len(seen) == 3


def test_fetch_driver_form_rejects_non_positive_last() -> None:
    with pytest.raises(ValueError):
        fetch_driver_form("russell", last=0)


def test_fetch_error_is_form_error() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500))

    with httpx.Client(transport=transport) as client, pytest.raises(FormError, match="500"):
        fetch_driver_form("russell", client=client)

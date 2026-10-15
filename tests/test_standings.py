import copy
from decimal import Decimal
from typing import Any

import httpx
import pytest

from f1brief.standings import (
    Standing,
    StandingChange,
    Standings,
    StandingsError,
    Team,
    combine,
    compare,
    fetch_report,
    fetch_standings,
    format_report,
    parse_constructor_standings,
    parse_driver_standings,
    report,
)


def rows(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return payload["MRData"]["StandingsTable"]["StandingsLists"][0][key]


def by_id(changes: tuple[StandingChange, ...]) -> dict[str, StandingChange]:
    return {c.current.id: c for c in changes}


@pytest.fixture
def r16(driver_standings_r16: dict, constructor_standings_r16: dict) -> Standings:
    return combine(driver_standings_r16, constructor_standings_r16)


@pytest.fixture
def r17(driver_standings_r17: dict, constructor_standings_r17: dict) -> Standings:
    return combine(driver_standings_r17, constructor_standings_r17)


def test_parses_driver_standings(driver_standings_r17: dict[str, Any]) -> None:
    season, round_, drivers = parse_driver_standings(driver_standings_r17)

    assert (season, round_) == (2026, 17)
    assert len(drivers) == 23
    leader = drivers[0]
    assert leader == Standing(
        id="antonelli",
        name="Andrea Kimi Antonelli",
        position=1,
        position_text="1",
        points=Decimal("340"),
        wins=8,
        teams=(Team("mercedes", "Mercedes"),),
        code="ANT",
    )


def test_driver_with_several_teams_keeps_all_of_them(driver_standings_r17: dict) -> None:
    # Lawson 2026: rb (R1-11), red_bull (R12-14), rb again (R15-). The API lists both teams
    # alphabetically, so no "current team" is derived from standings.
    _, _, drivers = parse_driver_standings(driver_standings_r17)
    lawson = next(d for d in drivers if d.id == "lawson")

    assert lawson.teams == (Team("rb", "RB F1 Team"), Team("red_bull", "Red Bull"))


def test_parses_constructor_standings(constructor_standings_r17: dict[str, Any]) -> None:
    season, round_, constructors = parse_constructor_standings(constructor_standings_r17)

    assert (season, round_) == (2026, 17)
    assert [c.id for c in constructors[:3]] == ["mercedes", "ferrari", "mclaren"]
    mclaren = constructors[2]
    assert (mclaren.position, mclaren.points, mclaren.teams) == (3, Decimal("339"), ())


def test_fractional_points_are_exact(driver_standings_r17: dict[str, Any]) -> None:
    payload = copy.deepcopy(driver_standings_r17)
    rows(payload, "DriverStandings")[0]["points"] = "340.5"

    _, _, drivers = parse_driver_standings(payload)

    assert drivers[0].points == Decimal("340.5")


def test_row_without_numeric_position(driver_standings_r17: dict[str, Any]) -> None:
    payload = copy.deepcopy(driver_standings_r17)
    last = rows(payload, "DriverStandings")[-1]
    del last["position"]
    last["positionText"] = "-"

    _, _, drivers = parse_driver_standings(payload)

    assert drivers[-1].position is None
    assert drivers[-1].position_text == "-"


def test_empty_standings_list_raises() -> None:
    payload = {"MRData": {"StandingsTable": {"season": "2027", "StandingsLists": []}}}

    with pytest.raises(StandingsError, match="no standings"):
        parse_driver_standings(payload)


def test_malformed_row_raises(driver_standings_r17: dict[str, Any]) -> None:
    payload = copy.deepcopy(driver_standings_r17)
    rows(payload, "DriverStandings")[0]["points"] = "lots"

    with pytest.raises(StandingsError):
        parse_driver_standings(payload)


def test_combine_rejects_mismatched_rounds(
    driver_standings_r17: dict, constructor_standings_r16: dict
) -> None:
    with pytest.raises(StandingsError, match=r"round 17.*round 16"):
        combine(driver_standings_r17, constructor_standings_r16)


def test_driver_changes_since_previous_round(r16: Standings, r17: Standings) -> None:
    changes = by_id(report(r17, r16).drivers)

    # Verstappen: 6th 188 -> 4th 221 (Singapore sprint weekend: +33).
    assert changes["max_verstappen"].positions_gained == 2
    assert changes["max_verstappen"].points_gained == Decimal("33")
    # Leclerc: 4th 191 -> 5th 215.
    assert changes["leclerc"].positions_gained == -1
    assert changes["leclerc"].points_gained == Decimal("24")
    # Russell (favourite): 2nd 236 -> 2nd 238.
    assert changes["russell"].positions_gained == 0
    assert changes["russell"].points_gained == Decimal("2")
    # Ocon (favourite): 16th 7 -> 16th 7.
    assert changes["ocon"].positions_gained == 0
    assert changes["ocon"].points_gained == Decimal("0")


def test_constructor_changes_since_previous_round(r16: Standings, r17: Standings) -> None:
    changes = by_id(report(r17, r16).constructors)

    # McLaren (favourite): 3rd 316 -> 3rd 339.
    assert changes["mclaren"].positions_gained == 0
    assert changes["mclaren"].points_gained == Decimal("23")
    # Red Bull 298 -> 332: Verstappen +33 and Hadjar +1.
    assert changes["red_bull"].points_gained == Decimal("34")


def test_points_gained_sum_matches_table_totals(r16: Standings, r17: Standings) -> None:
    rep = report(r17, r16)
    drivers_gained = sum((c.points_gained for c in rep.drivers), Decimal(0))
    constructors_gained = sum((c.points_gained for c in rep.constructors), Decimal(0))

    assert drivers_gained == constructors_gained == Decimal("137")


def test_new_entry_has_no_previous() -> None:
    newcomer = Standing("new_driver", "New Driver", 20, "20", Decimal("1"), 0)
    (change,) = compare([newcomer], [])

    assert change.previous is None
    assert change.positions_gained is None
    assert change.points_gained == Decimal("1")


def test_unknown_previous_position_gives_no_movement() -> None:
    now = Standing("x", "X", 10, "10", Decimal("5"), 0)
    before = Standing("x", "X", None, "-", Decimal("0"), 0)

    (change,) = compare([now], [before])

    assert change.positions_gained is None
    assert change.points_gained == Decimal("5")


def test_first_round_report_has_no_previous(r17: Standings) -> None:
    rep = report(r17, None)

    assert rep.previous_round is None
    assert all(c.previous is None for c in rep.drivers + rep.constructors)


def test_report_rejects_previous_from_same_or_later_round(r16: Standings, r17: Standings) -> None:
    with pytest.raises(ValueError):
        report(r16, r17)
    with pytest.raises(ValueError):
        report(r17, r17)


def test_format_report(r16: Standings, r17: Standings) -> None:
    text = format_report(report(r17, r16))
    lines = text.splitlines()

    assert lines[0] == "Standings after round 17 (2026), changes since round 16"
    assert "   4. Max Verstappen           Red Bull" in text
    assert lines[6].endswith("221 pts  +33   up 2")
    assert lines[7].endswith("215 pts  +24   down 1")
    assert "RB F1 Team / Red Bull" in text
    assert "   3. McLaren                    339 pts  +23   =" in lines
    assert lines[-1] == "Source: Jolpica-F1."


def test_format_points_without_exponent() -> None:
    standing = Standing("x", "X", 1, "1", Decimal("2.5E+1"), 0)
    rep = report(Standings(2026, 1, (standing,), ()), None)

    assert " 25 pts  +25" in format_report(rep)


def _standings_transport(fixtures: dict[str, dict[str, Any]], seen: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        payload = fixtures.get(request.url.path)
        return httpx.Response(200, json=payload) if payload else httpx.Response(404)

    return httpx.MockTransport(handler)


def test_fetch_standings_for_specific_round(
    driver_standings_r16: dict, constructor_standings_r16: dict
) -> None:
    seen: list[str] = []
    transport = _standings_transport(
        {
            "/ergast/f1/2026/16/driverStandings.json": driver_standings_r16,
            "/ergast/f1/2026/16/constructorStandings.json": constructor_standings_r16,
        },
        seen,
    )
    with httpx.Client(transport=transport) as client:
        result = fetch_standings(2026, 16, client=client)

    assert result.round == 16
    assert seen == [
        "/ergast/f1/2026/16/driverStandings.json",
        "/ergast/f1/2026/16/constructorStandings.json",
    ]


def test_fetch_report_fetches_latest_then_previous_round(
    driver_standings_r16: dict,
    constructor_standings_r16: dict,
    driver_standings_r17: dict,
    constructor_standings_r17: dict,
) -> None:
    seen: list[str] = []
    transport = _standings_transport(
        {
            "/ergast/f1/current/driverStandings.json": driver_standings_r17,
            "/ergast/f1/current/constructorStandings.json": constructor_standings_r17,
            "/ergast/f1/2026/16/driverStandings.json": driver_standings_r16,
            "/ergast/f1/2026/16/constructorStandings.json": constructor_standings_r16,
        },
        seen,
    )
    with httpx.Client(transport=transport) as client:
        rep = fetch_report(client=client)

    assert (rep.season, rep.round, rep.previous_round) == (2026, 17, 16)
    assert seen[2:] == [
        "/ergast/f1/2026/16/driverStandings.json",
        "/ergast/f1/2026/16/constructorStandings.json",
    ]


def test_fetch_report_after_first_round_skips_previous(
    driver_standings_r17: dict, constructor_standings_r17: dict
) -> None:
    drivers = copy.deepcopy(driver_standings_r17)
    constructors = copy.deepcopy(constructor_standings_r17)
    for payload in (drivers, constructors):
        payload["MRData"]["StandingsTable"]["StandingsLists"][0]["round"] = "1"
    seen: list[str] = []
    transport = _standings_transport(
        {
            "/ergast/f1/current/driverStandings.json": drivers,
            "/ergast/f1/current/constructorStandings.json": constructors,
        },
        seen,
    )
    with httpx.Client(transport=transport) as client:
        rep = fetch_report(client=client)

    assert rep.previous_round is None
    assert len(seen) == 2


def test_fetch_http_error_raises_standings_error() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500))

    with httpx.Client(transport=transport) as client, pytest.raises(StandingsError, match="500"):
        fetch_standings(client=client)

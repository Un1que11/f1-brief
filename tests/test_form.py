import copy
from decimal import Decimal
from typing import Any

import httpx
import pytest

from f1brief.form import (
    FormError,
    TeamForm,
    build_rounds,
    fetch_team_form,
    format_team_form,
    parse_qualifying,
    parse_results,
)

Payloads = tuple[dict[str, Any], dict[str, Any]]


def races(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload["MRData"]["RaceTable"]["Races"]


def keep_rounds(payload: dict[str, Any], rounds: set[int], rows_key: str) -> dict[str, Any]:
    """Copy of a payload with only the given rounds, and a consistent `total`."""
    trimmed = copy.deepcopy(payload)
    kept = [r for r in races(trimmed) if int(r["round"]) in rounds]
    trimmed["MRData"]["RaceTable"]["Races"] = kept
    trimmed["MRData"]["total"] = str(sum(len(r[rows_key]) for r in kept))
    return trimmed


def last_five(mclaren_2026: Payloads) -> TeamForm:
    rounds = build_rounds(*mclaren_2026)
    return TeamForm("mclaren", "McLaren", tuple(rounds[-5:]))


def test_parses_race_results(mclaren_2026: Payloads) -> None:
    results = parse_results(mclaren_2026[0])

    assert len(results) == 17
    name, race = results[(2026, 17)]
    assert name == "Singapore Grand Prix"
    norris = next(r for r in race if r.driver_id == "norris")
    assert (norris.grid, norris.position, norris.points) == (5, 4, Decimal("12"))
    assert norris.classified
    assert norris.constructor_name == "McLaren"


def test_retirement_is_not_classified(mclaren_2026: Payloads) -> None:
    _, race = parse_results(mclaren_2026[0])[(2026, 15)]
    norris = next(r for r in race if r.driver_id == "norris")

    assert (norris.position_text, norris.status, norris.classified) == ("R", "Retired", False)
    assert norris.points == Decimal("0")


def test_did_not_start_is_not_classified(mclaren_2026: Payloads) -> None:
    # Round 2: both cars withdrawn before the start.
    _, race = parse_results(mclaren_2026[0])[(2026, 2)]

    assert [r.position_text for r in race] == ["W", "W"]
    assert not any(r.classified for r in race)


def test_grid_differs_from_qualifying_after_penalty(mclaren_2026: Payloads) -> None:
    # Round 10: Norris qualified P3 but started P13.
    rounds = {r.round: r for r in build_rounds(*mclaren_2026)}
    r10 = rounds[10]
    quali = next(q for q in r10.qualifying if q.driver_id == "norris")
    race = next(x for x in r10.race if x.driver_id == "norris")

    assert (quali.position, race.grid) == (3, 13)


def test_pit_lane_start_has_no_grid(mclaren_2026: Payloads) -> None:
    payload = copy.deepcopy(mclaren_2026[0])
    races(payload)[-1]["Results"][0]["grid"] = "0"

    _, race = parse_results(payload)[(2026, 17)]

    assert race[0].grid is None


def test_parses_qualifying(mclaren_2026: Payloads) -> None:
    quali = parse_qualifying(mclaren_2026[1])[(2026, 17)]
    norris = next(q for q in quali if q.driver_id == "norris")

    assert (norris.position, norris.q1, norris.q2, norris.q3) == (
        5,
        "1:32.775",
        "1:31.895",
        "1:31.712",
    )


def test_missing_q3_is_none(mclaren_2026: Payloads) -> None:
    payload = copy.deepcopy(mclaren_2026[1])
    del races(payload)[-1]["QualifyingResults"][0]["Q3"]

    quali = parse_qualifying(payload)[(2026, 17)]

    assert quali[0].q3 is None


def test_truncated_page_is_rejected(mclaren_2026: Payloads) -> None:
    payload = copy.deepcopy(mclaren_2026[0])
    payload["MRData"]["total"] = "60"

    with pytest.raises(FormError, match="truncated"):
        parse_results(payload)


def test_malformed_result_raises(mclaren_2026: Payloads) -> None:
    payload = copy.deepcopy(mclaren_2026[0])
    del races(payload)[0]["Results"][0]["Driver"]

    with pytest.raises(FormError):
        parse_results(payload)


def test_rounds_sorted_with_entries_in_order(mclaren_2026: Payloads) -> None:
    rounds = build_rounds(*mclaren_2026)

    assert [r.round for r in rounds] == list(range(1, 18))
    for r in rounds:
        assert [x.position for x in r.race] == sorted(x.position for x in r.race)
        assert [q.position for q in r.qualifying] == sorted(q.position for q in r.qualifying)


def test_round_without_qualifying_data_still_counts(mclaren_2026: Payloads) -> None:
    results, quali = mclaren_2026
    quali = keep_rounds(quali, set(range(1, 17)), "QualifyingResults")

    r17 = build_rounds(results, quali)[-1]

    assert r17.round == 17
    assert r17.qualifying == ()
    assert r17.best_qualifying is None


def test_team_summary_last_five_rounds(mclaren_2026: Payloads) -> None:
    form = last_five(mclaren_2026)

    # R13-R17 Grand Prix points: 22 + 19 + 0 + 10 + 22.
    assert [r.points for r in form.rounds] == [Decimal(p) for p in (22, 19, 0, 10, 22)]
    assert form.points == Decimal("73")
    # Best qualifying per round: 3, 1, 3, 6, 5 -> 18 / 5.
    assert form.average_best_qualifying == Decimal("3.6")
    # Classified finishes: 4, 5, 3, 8, 13, 6, 9, 4, 5 -> 57 / 9 = 6.33.
    assert form.average_finish == Decimal("6.3")
    # Norris retired in R15.
    assert form.non_classified == 1
    assert [r.best_finish.position for r in form.rounds] == [4, 3, 13, 6, 4]


def test_driver_summaries(mclaren_2026: Payloads) -> None:
    norris, piastri = last_five(mclaren_2026).drivers()

    # Norris: 12 + 15 + 0 + 2 + 12; quali 9, 1, 5, 6, 5; finishes 4, 3, 9, 4 (R15 retired).
    assert (norris.driver_name, norris.starts, norris.points) == ("Lando Norris", 5, Decimal("41"))
    assert norris.average_qualifying == Decimal("5.2")
    assert norris.average_finish == Decimal("5.0")
    assert norris.non_classified == 1
    # Piastri: 10 + 4 + 0 + 8 + 10; quali 3, 7, 3, 7, 7; finishes 5, 8, 13, 6, 5.
    assert piastri.points == Decimal("32")
    assert piastri.average_qualifying == Decimal("5.4")
    assert piastri.average_finish == Decimal("7.4")
    assert piastri.non_classified == 0


def test_round_with_no_classified_car(mclaren_2026: Payloads) -> None:
    r2 = build_rounds(*mclaren_2026)[1]
    form = TeamForm("mclaren", "McLaren", (r2,))

    assert r2.best_finish is None
    assert form.average_finish is None
    assert form.non_classified == 2


def test_format_team_form(mclaren_2026: Payloads) -> None:
    text = format_team_form(last_five(mclaren_2026))

    assert text.splitlines()[0] == "McLaren form, last 5 rounds (2026 R13 to 2026 R17)"
    assert "      Race:       PIA P13 (from P3), NOR Retired (from P5)  0 pts" in text
    assert "Grand Prix points (sprints excluded): 73" in text
    assert "Average best qualifying position: 3.6" in text
    assert "  Lando Norris: 41 pts, avg quali 5.2, avg finish 5.0, not classified 1" in text
    assert text.endswith("Source: Jolpica-F1.")


def test_format_empty_form() -> None:
    assert format_team_form(TeamForm("mclaren", "McLaren", ())) == ("McLaren: no completed rounds.")


def _transport(routes: dict[str, dict[str, Any]], seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        body = routes.get(request.url.path)
        return httpx.Response(200, json=body) if body else httpx.Response(404)

    return httpx.MockTransport(handler)


def _routes(season: str, payloads: Payloads) -> dict[str, dict[str, Any]]:
    base = f"/ergast/f1/{season}/constructors/mclaren"
    return {f"{base}/results.json": payloads[0], f"{base}/qualifying.json": payloads[1]}


def test_fetch_team_form_within_season(mclaren_2026: Payloads) -> None:
    seen: list[str] = []
    transport = _transport(_routes("current", mclaren_2026), seen)

    with httpx.Client(transport=transport) as client:
        form = fetch_team_form("mclaren", client=client)

    assert form.constructor_name == "McLaren"
    assert [r.round for r in form.rounds] == [13, 14, 15, 16, 17]
    assert len(seen) == 2


def test_fetch_team_form_reaches_into_previous_season(
    mclaren_2026: Payloads, mclaren_2025: Payloads
) -> None:
    # Pretend only R1-R2 of 2026 have been run: the other three come from late 2025.
    early_2026 = (
        keep_rounds(mclaren_2026[0], {1, 2}, "Results"),
        keep_rounds(mclaren_2026[1], {1, 2}, "QualifyingResults"),
    )
    seen: list[str] = []
    transport = _transport(
        {**_routes("current", early_2026), **_routes("2025", mclaren_2025)}, seen
    )

    with httpx.Client(transport=transport) as client:
        form = fetch_team_form("mclaren", last=5, client=client)

    assert [(r.season, r.round) for r in form.rounds] == [
        (2025, 22),
        (2025, 23),
        (2025, 24),
        (2026, 1),
        (2026, 2),
    ]
    assert len(seen) == 4


def test_fetch_team_form_rejects_non_positive_last() -> None:
    with pytest.raises(ValueError):
        fetch_team_form("mclaren", last=0)


def test_fetch_error_is_form_error() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(502))

    with httpx.Client(transport=transport) as client, pytest.raises(FormError, match="502"):
        fetch_team_form("mclaren", client=client)

from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import load_fixture

from f1brief.form import FormError
from f1brief.track_history import (
    TrackHistory,
    build_edition,
    fetch_track_history,
    format_track_history,
    past_editions,
)

# Recorded 2026-10-15: /circuits/americas/races and /{season}/{round}/{results,qualifying}.
EDITIONS = {2023: 18, 2024: 19, 2025: 19}


def races() -> dict[str, Any]:
    return load_fixture("jolpica/americas_races.json")


def edition(season: int, **kwargs: Any):
    return build_edition(
        load_fixture(f"jolpica/results_{season}_americas.json"),
        load_fixture(f"jolpica/qualifying_{season}_americas.json"),
        **kwargs,
    )


def austin() -> TrackHistory:
    circuit, _ = past_editions(races(), date(2026, 10, 25))
    return TrackHistory(circuit, tuple(edition(s) for s in EDITIONS))


def test_past_editions_before_upcoming_race() -> None:
    circuit, held = past_editions(races(), date(2026, 10, 25))

    assert (circuit.id, circuit.name, circuit.locality) == (
        "americas",
        "Circuit of the Americas",
        "Austin",
    )
    # The 2026 race (25 Oct) is not held yet: the last three are 2023-2025.
    assert held == [(2023, 18), (2024, 19), (2025, 19)]


def test_past_editions_count_and_cutoff() -> None:
    _, held = past_editions(races(), date(2026, 10, 26), count=2)
    assert held == [(2025, 19), (2026, 18)]
    _, none_yet = past_editions(races(), date(2012, 1, 1))
    assert none_yet == []


def test_past_editions_rejects_truncated_or_empty() -> None:
    truncated = races()
    truncated["MRData"]["total"] = "200"
    with pytest.raises(FormError, match="truncated"):
        past_editions(truncated, date(2026, 10, 25))

    empty = races()
    empty["MRData"]["RaceTable"]["Races"] = []
    empty["MRData"]["total"] = "0"
    with pytest.raises(FormError, match="no races"):
        past_editions(empty, date(2026, 10, 25))


def test_edition_keeps_only_favourites_in_finishing_order() -> None:
    ed = edition(2023)

    assert (ed.season, ed.round, ed.race_name) == (2023, 18, "United States Grand Prix")
    # Norris P2, Russell P5, then the two retirements in classification order.
    assert [e.race.driver_code for e in ed.entries] == ["NOR", "RUS", "PIA", "OCO"]
    ocon = ed.entries[3]
    assert (ocon.race.constructor_id, ocon.race.status, ocon.race.classified) == (
        "alpine",
        "Retired",
        False,
    )
    assert ocon.qualifying.position == 8


def test_edition_grid_differs_from_qualifying() -> None:
    russell = next(e for e in edition(2024).entries if e.race.driver_id == "russell")

    # 2024: qualified P6, started P20.
    assert (russell.qualifying.position, russell.race.grid) == (6, 20)


def test_edition_with_other_favourites() -> None:
    ed = edition(2025, favourite_team="ferrari", favourite_drivers=("alonso",))

    assert {e.race.driver_id for e in ed.entries} == {"leclerc", "hamilton", "alonso"}


def test_edition_requires_exactly_one_race() -> None:
    results = load_fixture("jolpica/results_2025_americas.json")
    results["MRData"]["RaceTable"]["Races"] = []
    results["MRData"]["total"] = "0"

    with pytest.raises(FormError, match="one race"):
        build_edition(results, load_fixture("jolpica/qualifying_2025_americas.json"))


def test_team_points_per_edition() -> None:
    # McLaren GP points: 2023 18 + 0, 2024 12 + 10, 2025 18 + 10.
    assert austin().team_points("mclaren") == [Decimal(18), Decimal(22), Decimal(28)]
    assert austin().team_points("williams") == [Decimal(0)] * 3


def test_driver_summaries() -> None:
    summaries = {d.driver_id: d for d in austin().drivers()}

    russell = summaries["russell"]
    # Finishes 5, 6, 6 -> 5.7; quali 5, 6, 4 -> 5.0; 10 + 8 + 8 points.
    assert (russell.best_finish.position, russell.average_finish) == (5, Decimal("5.7"))
    assert (russell.average_qualifying, russell.points) == (Decimal("5.0"), Decimal("26"))
    assert russell.teams == ("Mercedes", "Mercedes", "Mercedes")

    ocon = summaries["ocon"]
    # Retired, P18, P15 -> best P15, average (18 + 15) / 2 = 16.5; quali 8, 13, 17 -> 12.7.
    assert ocon.best_finish.position == 15
    assert ocon.average_finish == Decimal("16.5")
    assert ocon.average_qualifying == Decimal("12.7")
    assert ocon.non_classified == 1
    assert ocon.teams == ("Alpine F1 Team", "Alpine F1 Team", "Haas F1 Team")

    # Norris: 2, 4, 2 -> 2.7, 18 + 12 + 18 points.
    assert (summaries["norris"].average_finish, summaries["norris"].points) == (
        Decimal("2.7"),
        Decimal("48"),
    )


def test_format_track_history() -> None:
    text = format_track_history(austin())

    assert text.splitlines()[0] == (
        "Favourites at Circuit of the Americas (Austin, USA): 2023, 2024, 2025"
    )
    assert "      RUS Mercedes           quali P6   grid P20       P6         8 pts" in text
    assert "      OCO Alpine F1 Team     quali P8   grid P8        Retired    0 pts" in text
    assert "McLaren Grand Prix points (sprints excluded): 18 / 22 / 28 (total 68)" in text
    assert (
        "  Esteban Ocon (Alpine F1 Team, Haas F1 Team): 3 starts, best P15, avg finish 16.5,"
        " avg quali 12.7, 0 pts, not classified 1" in text
    )
    assert text.endswith("Source: Jolpica-F1.")


def test_format_without_editions() -> None:
    circuit, _ = past_editions(races(), date(2026, 10, 25))

    assert format_track_history(TrackHistory(circuit, ())) == (
        "Circuit of the Americas: no earlier races found."
    )


def _transport(seen: list[str]) -> httpx.MockTransport:
    routes = {"/ergast/f1/circuits/americas/races.json": races()}
    for season, round_ in EDITIONS.items():
        for kind in ("results", "qualifying"):
            routes[f"/ergast/f1/{season}/{round_}/{kind}.json"] = load_fixture(
                f"jolpica/{kind}_{season}_americas.json"
            )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        body = routes.get(request.url.path)
        return httpx.Response(200, json=body) if body else httpx.Response(404)

    return httpx.MockTransport(handler)


def test_fetch_track_history_requests() -> None:
    seen: list[str] = []

    with httpx.Client(transport=_transport(seen)) as client:
        history = fetch_track_history("americas", date(2026, 10, 25), client=client)

    assert [ed.season for ed in history.editions] == [2023, 2024, 2025]
    assert seen == [
        "/ergast/f1/circuits/americas/races.json",
        "/ergast/f1/2023/18/results.json",
        "/ergast/f1/2023/18/qualifying.json",
        "/ergast/f1/2024/19/results.json",
        "/ergast/f1/2024/19/qualifying.json",
        "/ergast/f1/2025/19/results.json",
        "/ergast/f1/2025/19/qualifying.json",
    ]


def test_fetch_rejects_non_positive_count() -> None:
    with pytest.raises(ValueError):
        fetch_track_history("americas", date(2026, 10, 25), count=0)


def test_fetch_error_is_form_error() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(503))

    with httpx.Client(transport=transport) as client, pytest.raises(FormError, match="503"):
        fetch_track_history("americas", date(2026, 10, 25), client=client)

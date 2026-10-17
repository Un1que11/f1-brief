from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import openf1_fixture

from f1brief import openf1
from f1brief.config import DEFAULT_FAVOURITES, Favourite, Favourites
from f1brief.pace import (
    Driver,
    Lap,
    PaceError,
    Stint,
    build_report,
    driver_pace,
    fetch_report,
    format_report,
    last_race_session,
    long_run_laps,
    neutralised_laps,
)

SINGAPORE = 11388


def singapore_session() -> dict[str, Any]:
    sessions = openf1_fixture("sessions_2026_race")
    return next(s for s in sessions if s["session_key"] == SINGAPORE)


def singapore_report():
    return build_report(
        singapore_session(),
        openf1_fixture("drivers_11388"),
        openf1_fixture("laps_11388"),
        openf1_fixture("stints_11388"),
        openf1_fixture("race_control_11388"),
        openf1_fixture("session_result_11388"),
        DEFAULT_FAVOURITES,
    )


def lap(n: int, seconds: str | None, pit_out: bool = False) -> Lap:
    return Lap(1, n, Decimal(seconds) if seconds else None, pit_out)


# Synthetic driver: stint 1 = laps 1-10, stint 2 = laps 11-14 (too short), stint 3 = laps 15-24.
STINTS = [Stint(1, 1, 1, 10, "MEDIUM"), Stint(1, 2, 11, 14, "SOFT"), Stint(1, 3, 15, 24, "HARD")]
LAPS = (
    [lap(1, "100")]
    + [lap(n, str(88 + n)) for n in range(2, 10)]  # laps 2-9: 90..97
    + [lap(10, "99")]  # in-lap
    + [lap(n, "85") for n in range(11, 15)]  # short stint: ignored even though fast
    + [lap(15, "110", pit_out=True)]
    + [lap(n, "89") for n in range(16, 24)]  # laps 16-23: 89
    + [lap(24, "120")]  # slow outlier
)


def test_long_run_laps_apply_every_exclusion() -> None:
    clean = long_run_laps(LAPS, STINTS, frozenset({5}))

    # Candidates: laps 2-9 without lap 5 (90, 91, 92, 94, 95, 96, 97) and laps 16-24 (89 x 8,
    # 120). Median of those 16 = (89 + 90) / 2 = 89.5; limit 89.5 * 1.07 = 95.765, so 96, 97
    # and 120 go. Left: 89 x 8, 90, 91, 92, 94, 95.
    assert sorted(clean) == [Decimal(x) for x in [89] * 8 + [90, 91, 92, 94, 95]]


def test_driver_pace_median_and_minimum_laps() -> None:
    driver = Driver(1, "TST", "Test Driver", "Team")

    pace = driver_pace(driver, 4, LAPS, STINTS, frozenset({5}))
    # 13 clean laps; the 7th of 13 sorted values is 89.
    assert (pace.median, pace.laps, pace.position) == (Decimal("89"), 13, 4)

    too_few = driver_pace(driver, 4, LAPS[:5], STINTS, frozenset())
    # Laps 2-5 only: 4 clean laps is below the minimum of 5.
    assert (too_few.median, too_few.laps) == (None, 4)


def test_no_stints_or_untimed_laps_give_no_pace() -> None:
    assert long_run_laps(LAPS, [], frozenset()) == []
    untimed = [lap(n, None) for n in range(2, 10)]
    assert long_run_laps(untimed, STINTS, frozenset()) == []


def test_neutralised_laps_from_recorded_race_control() -> None:
    # Singapore 2026: VSC laps 8-9 and 39-40, each plus the restart lap.
    assert neutralised_laps(openf1_fixture("race_control_11388"), 62) == {8, 9, 10, 39, 40, 41}


def test_neutralised_laps_safety_car_red_flag_and_open_end() -> None:
    messages = [
        {"date": "1", "lap_number": 12, "category": "SafetyCar", "message": "SAFETY CAR DEPLOYED"},
        {
            "date": "2",
            "lap_number": 15,
            "category": "SafetyCar",
            "message": "SAFETY CAR IN THIS LAP",
        },
        {"date": "3", "lap_number": 30, "category": "Flag", "flag": "RED", "message": "RED FLAG"},
        {"date": "4", "lap_number": 50, "category": "SafetyCar", "message": "VSC DEPLOYED"},
    ]

    assert neutralised_laps(messages, 52) == {12, 13, 14, 15, 16, 30, 31, 50, 51, 52}


def test_singapore_report_leaders_and_favourites() -> None:
    report = singapore_report()
    leaders = {p.driver.code: p for p in report.leaders}
    favourites = {p.driver.code: p for p in report.favourites}

    # Values cross-checked with an independent prototype of the same rules over the raw data.
    assert [p.driver.code for p in report.leaders] == ["VER", "LEC", "ANT"]
    assert leaders["VER"].median == Decimal("97.300")
    assert leaders["LEC"].median == Decimal("97.220")
    assert report.reference.driver.code == "LEC"
    # McLaren and Haas (with Bearman) as teams, Russell and Ocon as drivers.
    assert list(favourites) == ["NOR", "PIA", "RUS", "BEA", "OCO"]
    assert {code: report.delta(p) for code, p in favourites.items()} == {
        "NOR": Decimal("0.677"),
        "PIA": Decimal("0.849"),
        "RUS": Decimal("1.527"),
        "BEA": Decimal("1.985"),
        "OCO": Decimal("2.032"),
    }
    # 62 laps - lap 1 - 6 neutralised - pit in and out laps = 53 for a one-stop.
    assert favourites["NOR"].laps == 53
    assert report.delta(report.reference) == Decimal("0")


def test_favourites_can_be_changed() -> None:
    report = build_report(
        singapore_session(),
        openf1_fixture("drivers_11388"),
        openf1_fixture("laps_11388"),
        openf1_fixture("stints_11388"),
        openf1_fixture("race_control_11388"),
        openf1_fixture("session_result_11388"),
        Favourites(
            teams=(Favourite("ferrari", "Ferrari"),),
            drivers=(Favourite("albon", "Alexander Albon", ("Albon",)),),
        ),
    )

    assert [p.driver.code for p in report.favourites] == ["LEC", "HAM", "ALB"]


def test_favourites_are_matched_by_alias_in_openf1_names() -> None:
    from f1brief.pace import favourite_matcher

    is_favourite = favourite_matcher(DEFAULT_FAVOURITES)

    # OpenF1 writes surnames in capitals and calls the team "Haas F1 Team".
    assert is_favourite(Driver(87, "BEA", "Oliver BEARMAN", "Haas F1 Team"))
    assert is_favourite(Driver(63, "RUS", "George RUSSELL", "Mercedes"))
    assert is_favourite(Driver(81, "PIA", "Oscar PIASTRI", "McLaren"))
    assert not is_favourite(Driver(16, "LEC", "Charles LECLERC", "Ferrari"))
    assert not favourite_matcher(Favourites((), ()))(Driver(63, "RUS", "George RUSSELL", "M"))


def test_malformed_data_raises() -> None:
    laps = openf1_fixture("laps_11388")
    laps[0] = {"lap_number": 1}

    with pytest.raises(PaceError):
        build_report(
            singapore_session(),
            openf1_fixture("drivers_11388"),
            laps,
            [],
            [],
            [],
            Favourites((), ()),
        )


def test_last_race_session() -> None:
    sessions = openf1_fixture("sessions_2026_race")

    assert (
        last_race_session(sessions, datetime(2026, 10, 15, tzinfo=UTC))["session_key"] == SINGAPORE
    )
    assert last_race_session(sessions, datetime(2026, 1, 1, tzinfo=UTC)) is None


def test_last_race_session_ignores_cancelled_and_sprints() -> None:
    sessions = openf1_fixture("sessions_2026_race")
    for s in sessions:
        if s["session_key"] == SINGAPORE:
            s["is_cancelled"] = True
    sessions.append({**singapore_session(), "session_key": 1, "session_name": "Sprint"})

    chosen = last_race_session(sessions, datetime(2026, 10, 15, tzinfo=UTC))

    assert chosen["session_key"] not in (SINGAPORE, 1)
    assert chosen["date_end"] < singapore_session()["date_end"]


def test_format_report() -> None:
    text = format_report(singapore_report())

    assert text.splitlines()[0] == "Long-run pace, Singapore (Marina Bay) 2026-10-11"
    assert "Neutralised laps: 8, 9, 10, 39, 40, 41" in text
    assert "Reference: LEC (P2) 1:37.220" in text
    assert "  P4  NOR McLaren          1:37.897   +0.677s  (53 laps)" in text
    assert "  P13 OCO Haas F1 Team     1:39.252   +2.032s  (51 laps)" in text
    assert text.endswith("Source: OpenF1.")


def test_get_rows_retries_after_rate_limit() -> None:
    answers = iter(
        [httpx.Response(429), httpx.Response(429), httpx.Response(200, json=[{"x": 1.5}])]
    )
    slept: list[float] = []
    transport = httpx.MockTransport(lambda request: next(answers))

    with httpx.Client(transport=transport) as client:
        rows = openf1.get_rows("laps", {"session_key": 1}, client, sleep=slept.append)

    assert rows == [{"x": Decimal("1.5")}]
    assert slept == [1.0, 2.0]


def test_get_rows_gives_up_after_retries() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(429))

    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(openf1.OpenF1Error, match="429"),
    ):
        openf1.get_rows("laps", {}, client, sleep=lambda s: None)


def _routes() -> dict[tuple[str, str], Any]:
    return {
        ("sessions", "2026"): openf1_fixture("sessions_2026_race"),
        ("drivers", "11388"): openf1_fixture("drivers_11388"),
        ("laps", "11388"): openf1_fixture("laps_11388"),
        ("stints", "11388"): openf1_fixture("stints_11388"),
        ("race_control", "11388"): openf1_fixture("race_control_11388"),
        ("session_result", "11388"): openf1_fixture("session_result_11388"),
    }


def _transport(routes: dict, seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        key = request.url.params.get("session_key") or request.url.params.get("year")
        seen.append(endpoint)
        if (endpoint, key) in routes:
            return httpx.Response(200, json=routes[(endpoint, key)])
        return httpx.Response(200, json=[])

    return httpx.MockTransport(handler)


def test_fetch_report_for_last_race() -> None:
    seen: list[str] = []

    with httpx.Client(transport=_transport(_routes(), seen)) as client:
        report = fetch_report(datetime(2026, 10, 15, tzinfo=UTC), DEFAULT_FAVOURITES, client=client)

    assert report.session_key == SINGAPORE
    assert seen == ["sessions", "drivers", "laps", "stints", "race_control", "session_result"]


def test_fetch_report_falls_back_to_previous_season_then_fails() -> None:
    seen: list[str] = []

    with (
        httpx.Client(transport=_transport({}, seen)) as client,
        pytest.raises(PaceError, match="no finished race"),
    ):
        fetch_report(datetime(2027, 2, 1, tzinfo=UTC), DEFAULT_FAVOURITES, client=client)

    assert seen == ["sessions", "sessions"]


def test_float_lap_times_are_converted_without_binary_noise() -> None:
    from f1brief.pace import _parse_laps

    (parsed,) = _parse_laps([{"driver_number": 1, "lap_number": 2, "lap_duration": 97.3}])

    assert parsed.duration == Decimal("97.3")
    assert str(parsed.duration) == "97.3"

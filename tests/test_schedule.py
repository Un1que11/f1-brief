import copy
import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from f1brief.schedule import (
    WARSAW,
    ScheduleError,
    SessionKind,
    fetch_schedule,
    format_schedule,
    next_race,
    parse_schedule,
)


def races_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload["MRData"]["RaceTable"]["Races"]


def test_parses_whole_season(schedule_2026: dict[str, Any]) -> None:
    races = parse_schedule(schedule_2026)

    assert len(races) == 23
    assert [r.round for r in races] == list(range(1, 24))
    assert races[0].name == "Australian Grand Prix"
    assert races[-1].name == "Abu Dhabi Grand Prix"


def test_parses_circuit(schedule_2026: dict[str, Any]) -> None:
    circuit = parse_schedule(schedule_2026)[0].circuit

    assert circuit.id == "albert_park"
    assert circuit.locality == "Melbourne"
    assert circuit.country == "Australia"
    assert circuit.latitude == pytest.approx(-37.8497)
    assert circuit.longitude == pytest.approx(144.968)


def test_conventional_weekend_sessions_in_order(schedule_2026: dict[str, Any]) -> None:
    australia = parse_schedule(schedule_2026)[0]

    assert [s.kind for s in australia.sessions] == [
        SessionKind.FP1,
        SessionKind.FP2,
        SessionKind.FP3,
        SessionKind.QUALIFYING,
        SessionKind.RACE,
    ]
    assert not australia.is_sprint_weekend
    assert australia.first_session.start_utc == datetime(2026, 3, 6, 1, 30, tzinfo=UTC)
    assert australia.race.start_utc == datetime(2026, 3, 8, 4, 0, tzinfo=UTC)


def test_sprint_weekend_sessions_sorted_chronologically(schedule_2026: dict[str, Any]) -> None:
    # The API lists Qualifying before Sprint and SprintQualifying; sessions must be re-sorted.
    china = parse_schedule(schedule_2026)[1]

    assert china.is_sprint_weekend
    assert [s.kind for s in china.sessions] == [
        SessionKind.FP1,
        SessionKind.SPRINT_QUALIFYING,
        SessionKind.SPRINT,
        SessionKind.QUALIFYING,
        SessionKind.RACE,
    ]


def test_warsaw_time_in_winter_is_utc_plus_one(schedule_2026: dict[str, Any]) -> None:
    australia = parse_schedule(schedule_2026)[0]

    # FP1 2026-03-06 01:30Z -> 02:30 CET.
    assert australia.first_session.start_in(WARSAW) == datetime(2026, 3, 6, 2, 30, tzinfo=WARSAW)


def test_warsaw_time_across_dst_change(schedule_2026: dict[str, Any]) -> None:
    # Austin 2026: FP1 on 23 Oct is still CEST (UTC+2); the clocks go back on 25 Oct at
    # 01:00 UTC, so the race at 20:00Z is CET (UTC+1).
    austin = parse_schedule(schedule_2026)[17]
    fp1 = austin.first_session.start_in(WARSAW)
    race = austin.race.start_in(WARSAW)

    assert austin.name == "United States Grand Prix"
    assert fp1 is not None and race is not None
    assert (fp1.hour, fp1.minute, fp1.utcoffset().total_seconds()) == (19, 30, 7200)
    assert (race.hour, race.minute, race.utcoffset().total_seconds()) == (21, 0, 3600)


def test_session_without_time_is_kept_with_date_only(schedule_2026: dict[str, Any]) -> None:
    payload = copy.deepcopy(schedule_2026)
    del races_of(payload)[0]["FirstPractice"]["time"]

    fp1 = parse_schedule(payload)[0].first_session

    assert fp1.kind is SessionKind.FP1
    assert fp1.date == date(2026, 3, 6)
    assert fp1.start_utc is None
    assert fp1.start_in(WARSAW) is None


def test_malformed_payload_raises() -> None:
    with pytest.raises(ScheduleError):
        parse_schedule({"MRData": {}})


def test_malformed_race_raises(schedule_2026: dict[str, Any]) -> None:
    payload = copy.deepcopy(schedule_2026)
    del races_of(payload)[0]["Circuit"]

    with pytest.raises(ScheduleError):
        parse_schedule(payload)


@pytest.mark.parametrize(
    ("now", "expected_round"),
    [
        # Before the season starts.
        (datetime(2026, 1, 1, tzinfo=UTC), 1),
        # Today (2026-10-15): Singapore (R17) is done, Austin (R18) is next.
        (datetime(2026, 10, 15, 12, 0, tzinfo=UTC), 18),
        # Mid-weekend in Austin: FP1 is over but the race has not started.
        (datetime(2026, 10, 24, 12, 0, tzinfo=UTC), 18),
        # One minute after the Austin race start: Mexico City (R19) is next.
        (datetime(2026, 10, 25, 20, 1, tzinfo=UTC), 19),
    ],
)
def test_next_race(schedule_2026: dict[str, Any], now: datetime, expected_round: int) -> None:
    race = next_race(parse_schedule(schedule_2026), now)

    assert race is not None
    assert race.round == expected_round


def test_next_race_after_season_end_is_none(schedule_2026: dict[str, Any]) -> None:
    assert next_race(parse_schedule(schedule_2026), datetime(2026, 12, 31, tzinfo=UTC)) is None


def test_next_race_with_unknown_start_time_counts_until_its_date(
    schedule_2026: dict[str, Any],
) -> None:
    payload = copy.deepcopy(schedule_2026)
    del races_of(payload)[-1]["time"]  # Abu Dhabi, 2026-12-06
    races = parse_schedule(payload)

    assert next_race(races, datetime(2026, 12, 6, 22, 0, tzinfo=UTC)).round == 23
    # 2026-12-07 00:30 in Warsaw: the race date has passed.
    assert next_race(races, datetime(2026, 12, 6, 23, 30, tzinfo=UTC)) is None


def test_next_race_requires_aware_datetime(schedule_2026: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        next_race(parse_schedule(schedule_2026), datetime(2026, 10, 15))


def test_format_schedule_in_warsaw_time(schedule_2026: dict[str, Any]) -> None:
    austin = parse_schedule(schedule_2026)[17]

    assert format_schedule(austin) == "\n".join(
        [
            "Round 18: United States Grand Prix (2026)",
            "Circuit of the Americas, Austin, USA",
            "  FP1               Fri 23 Oct 19:30",
            "  FP2               Fri 23 Oct 23:00",
            "  FP3               Sat 24 Oct 19:30",
            "  Qualifying        Sat 24 Oct 23:00",
            "  Race              Sun 25 Oct 21:00",
            "Times: Europe/Warsaw. Source: Jolpica-F1.",
        ]
    )


def test_format_schedule_marks_unconfirmed_times(schedule_2026: dict[str, Any]) -> None:
    payload = copy.deepcopy(schedule_2026)
    del races_of(payload)[0]["ThirdPractice"]["time"]

    assert "  FP3               Sat 07 Mar TBC" in format_schedule(parse_schedule(payload)[0])


def test_fetch_schedule_requests_season_and_parses(schedule_2026: dict[str, Any]) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=schedule_2026)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        races = fetch_schedule(2026, client=client)

    assert len(races) == 23
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/ergast/f1/2026.json"
    assert seen[0].url.params["limit"] == "100"


def test_fetch_schedule_http_error_raises() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(503))

    with httpx.Client(transport=transport) as client, pytest.raises(ScheduleError, match="503"):
        fetch_schedule(client=client)


def test_fetch_schedule_invalid_json_raises() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html>"))

    with httpx.Client(transport=transport) as client, pytest.raises(ScheduleError, match="JSON"):
        fetch_schedule(client=client)


def test_fixture_is_valid_json_with_expected_shape(schedule_2026: dict[str, Any]) -> None:
    # Guards against an accidentally truncated re-recording of the fixture.
    assert json.loads(json.dumps(schedule_2026))["MRData"]["total"] == "23"


def test_unconfirmed_sprint_times_fall_back_to_usual_order(
    schedule_2026: dict[str, Any],
) -> None:
    # China: Sprint and Qualifying are both on Saturday; without times, Sprint comes first.
    payload = copy.deepcopy(schedule_2026)
    china = races_of(payload)[1]
    del china["Sprint"]["time"]
    del china["Qualifying"]["time"]

    kinds = [s.kind for s in parse_schedule(payload)[1].sessions]

    assert kinds == [
        SessionKind.FP1,
        SessionKind.SPRINT_QUALIFYING,
        SessionKind.SPRINT,
        SessionKind.QUALIFYING,
        SessionKind.RACE,
    ]

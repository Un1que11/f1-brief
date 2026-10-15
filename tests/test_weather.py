import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from f1brief.schedule import Race, SessionKind, parse_schedule
from f1brief.weather import (
    ForecastNotAvailable,
    WeatherError,
    describe,
    fetch_forecast,
    format_forecast,
    parse_forecast,
    weekend_dates,
)

FIXTURES = Path(__file__).parent / "fixtures" / "openmeteo"


def austin_payload() -> dict[str, Any]:
    """Real Open-Meteo response for COTA, 23-25 Oct 2026 (recorded 2026-10-15)."""
    return json.loads((FIXTURES / "austin_2026-10-23_25.json").read_text())


def austin(schedule_2026: dict) -> Race:
    return parse_schedule(schedule_2026)[17]


def test_weekend_dates(schedule_2026: dict) -> None:
    assert weekend_dates(austin(schedule_2026)) == (date(2026, 10, 23), date(2026, 10, 25))


def test_parses_local_days(schedule_2026: dict) -> None:
    forecast = parse_forecast(austin_payload(), austin(schedule_2026))

    assert forecast.timezone == "America/Chicago"
    assert [d.date for d in forecast.days] == [
        date(2026, 10, 23),
        date(2026, 10, 24),
        date(2026, 10, 25),
    ]
    friday = forecast.days[0]
    assert (friday.weather_code, friday.description) == (53, "moderate drizzle")
    assert (friday.temperature_min, friday.temperature_max) == (17.1, 23.9)
    assert (friday.precipitation_probability_max, friday.precipitation_sum) == (11, 2.9)


def test_sessions_use_the_hour_containing_the_start(schedule_2026: dict) -> None:
    forecast = parse_forecast(austin_payload(), austin(schedule_2026))
    fp1 = forecast.sessions[0]

    assert [s.kind for s in forecast.sessions] == [
        SessionKind.FP1,
        SessionKind.FP2,
        SessionKind.FP3,
        SessionKind.QUALIFYING,
        SessionKind.RACE,
    ]
    # FP1 17:30 UTC = 12:30 CDT (UTC-5): the 12:00 local hour.
    assert fp1.start_utc == datetime(2026, 10, 23, 17, 30, tzinfo=UTC)
    assert fp1.hour_local == datetime(2026, 10, 23, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
    assert (fp1.temperature, fp1.precipitation_probability, fp1.weather_code) == (21.3, 9, 51)


def _payload(tz: str, hours: dict[str, float]) -> dict[str, Any]:
    return {
        "timezone": tz,
        "daily": {"time": []},
        "hourly": {"time": list(hours), "temperature_2m": list(hours.values())},
    }


def test_hours_are_matched_correctly_across_a_clock_change(schedule_2026: dict) -> None:
    # London changes from BST (UTC+1) to GMT on 25 Oct 2026: 14:00 local is 13:00 UTC on
    # Saturday but 14:00 UTC on Sunday.
    race = austin(schedule_2026)
    race = Race(
        season=race.season,
        round=race.round,
        name="Test GP",
        circuit=race.circuit,
        sessions=(
            race.sessions[0].__class__(
                SessionKind.QUALIFYING,
                date(2026, 10, 24),
                datetime(2026, 10, 24, 13, 0, tzinfo=UTC),
            ),
            race.sessions[0].__class__(
                SessionKind.RACE, date(2026, 10, 25), datetime(2026, 10, 25, 14, 0, tzinfo=UTC)
            ),
        ),
        source_url="",
    )
    payload = _payload("Europe/London", {"2026-10-24T14:00": 11.0, "2026-10-25T14:00": 12.0})

    quali, race_forecast = parse_forecast(payload, race).sessions

    assert (quali.temperature, quali.hour_local.hour) == (11.0, 14)
    assert (race_forecast.temperature, race_forecast.hour_local.hour) == (12.0, 14)


def test_unconfirmed_or_out_of_range_sessions_are_skipped(schedule_2026: dict) -> None:
    payload = austin_payload()
    # Keep only Friday's hours: Saturday and Sunday sessions fall outside.
    payload["hourly"] = {k: v[:24] for k, v in payload["hourly"].items()}

    forecast = parse_forecast(payload, austin(schedule_2026))

    assert [s.kind for s in forecast.sessions] == [SessionKind.FP1, SessionKind.FP2]


def test_malformed_payload_raises(schedule_2026: dict) -> None:
    with pytest.raises(WeatherError):
        parse_forecast({"timezone": "Mars/Olympus"}, austin(schedule_2026))


def test_describe() -> None:
    assert describe(0) == "clear sky"
    assert describe(95) == "thunderstorm"
    assert describe(42) == "weather code 42"
    assert describe(None) == "unknown"


def test_fetch_requests_the_weekend_at_the_circuit(schedule_2026: dict) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=austin_payload())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        forecast = fetch_forecast(austin(schedule_2026), client)

    params = seen[0].url.params
    assert (params["latitude"], params["longitude"]) == ("30.1328", "-97.6411")
    assert (params["start_date"], params["end_date"], params["timezone"]) == (
        "2026-10-23",
        "2026-10-25",
        "auto",
    )
    assert "precipitation_probability" in params["hourly"]
    assert len(forecast.days) == 3


def test_beyond_horizon_is_not_available(schedule_2026: dict) -> None:
    body = json.loads((FIXTURES / "error_out_of_range.json").read_text())
    transport = httpx.MockTransport(lambda request: httpx.Response(400, json=body))

    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(ForecastNotAvailable, match="out of allowed range"),
    ):
        fetch_forecast(austin(schedule_2026), client)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": True, "reason": "Cannot initialize WeatherVariable"}),
        httpx.Response(500, text="<html>oops</html>"),
        httpx.Response(503, json=[]),
    ],
)
def test_other_failures_raise(schedule_2026: dict, response: httpx.Response) -> None:
    transport = httpx.MockTransport(lambda request: response)

    with httpx.Client(transport=transport) as client, pytest.raises(WeatherError) as exc:
        fetch_forecast(austin(schedule_2026), client)
    assert not isinstance(exc.value, ForecastNotAvailable)


def test_format_forecast(schedule_2026: dict) -> None:
    text = format_forecast(parse_forecast(austin_payload(), austin(schedule_2026)))

    assert (
        text.splitlines()[0]
        == "Weather for United States Grand Prix (circuit time: America/Chicago)"
    )
    assert (
        "  Fri 23 Oct: moderate drizzle, 17 to 24°C, rain 11% (2.9 mm), wind up to 14 km/h" in text
    )
    assert (
        "  FP1               Fri 12:30 local (12:00 forecast):"
        " light drizzle, 21°C, rain 9%, wind 11 km/h" in text
    )
    assert text.endswith("Source: Open-Meteo.")

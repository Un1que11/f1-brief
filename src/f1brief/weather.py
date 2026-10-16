"""Weather forecast at the circuit for the race weekend, from Open-Meteo (no API key).

Docs: https://open-meteo.com/en/docs. Forecasts reach about 16 days ahead; asking for days
beyond that is answered with HTTP 400 and a reason, which is reported as "not available yet".
`timezone=auto` makes daily values follow the circuit's local days and returns the circuit's
time zone name, which is used to line hourly values up with the (UTC) session start times.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from f1brief.schedule import Race, SessionKind

BASE_URL = "https://api.open-meteo.com/v1/forecast"
SOURCE_NAME = "Open-Meteo"
DAILY = (
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_probability_max",
    "precipitation_sum",
    "wind_speed_10m_max",
)
HOURLY = (
    "temperature_2m",
    "precipitation_probability",
    "precipitation",
    "weather_code",
    "wind_speed_10m",
)

# WMO weather interpretation codes, as documented by Open-Meteo.
WMO_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snow",
    73: "moderate snow",
    75: "heavy snow",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


def describe(code: int | None) -> str:
    if code is None:
        return "unknown"
    return WMO_CODES.get(code, f"weather code {code}")


class WeatherError(Exception):
    """The forecast could not be fetched or understood."""


class ForecastNotAvailable(WeatherError):
    """The weekend is beyond the forecast horizon (or otherwise outside the allowed range)."""


@dataclass(frozen=True)
class DayForecast:
    date: date  # circuit-local day
    weather_code: int | None
    temperature_max: float | None  # °C
    temperature_min: float | None  # °C
    precipitation_probability_max: int | None  # %
    precipitation_sum: float | None  # mm
    wind_speed_max: float | None  # km/h

    @property
    def description(self) -> str:
        return describe(self.weather_code)


@dataclass(frozen=True)
class SessionForecast:
    kind: SessionKind
    start_utc: datetime
    # The forecast hour containing the session start, in circuit-local time.
    hour_local: datetime
    temperature: float | None
    precipitation_probability: int | None
    precipitation: float | None
    weather_code: int | None
    wind_speed: float | None

    @property
    def description(self) -> str:
        return describe(self.weather_code)


@dataclass(frozen=True)
class WeekendForecast:
    race_name: str
    timezone: str
    days: tuple[DayForecast, ...]
    sessions: tuple[SessionForecast, ...]


def _value(series: dict[str, list[Any]], key: str, index: int) -> Any:
    values = series.get(key) or []
    return values[index] if index < len(values) else None


def parse_forecast(payload: dict[str, Any], race: Race) -> WeekendForecast:
    try:
        tz_name = payload["timezone"]
        tz = ZoneInfo(tz_name)
        daily, hourly = payload["daily"], payload["hourly"]
        days = tuple(
            DayForecast(
                date=date.fromisoformat(day),
                weather_code=_value(daily, "weather_code", i),
                temperature_max=_value(daily, "temperature_2m_max", i),
                temperature_min=_value(daily, "temperature_2m_min", i),
                precipitation_probability_max=_value(daily, "precipitation_probability_max", i),
                precipitation_sum=_value(daily, "precipitation_sum", i),
                wind_speed_max=_value(daily, "wind_speed_10m_max", i),
            )
            for i, day in enumerate(daily["time"])
        )
        # Hourly times are circuit-local wall-clock hours; localise them to compare with UTC.
        hours = {
            datetime.fromisoformat(t).replace(tzinfo=tz).astimezone(UTC): i
            for i, t in enumerate(hourly["time"])
        }
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise WeatherError(f"unexpected Open-Meteo response: {exc!r}") from exc
    # The request is padded by a day on each side (see `fetch_forecast`); keep only the
    # circuit-local days that actually have a session.
    local_days = {
        s.start_utc.astimezone(tz).date() if s.start_utc is not None else s.date
        for s in race.sessions
    }
    days = tuple(d for d in days if d.date in local_days)

    sessions = []
    for session in race.sessions:
        if session.start_utc is None:
            continue  # time not confirmed yet
        hour = session.start_utc.replace(minute=0, second=0, microsecond=0)
        i = hours.get(hour)
        if i is None:
            continue  # outside the returned range
        sessions.append(
            SessionForecast(
                kind=session.kind,
                start_utc=session.start_utc,
                hour_local=hour.astimezone(tz),
                temperature=_value(hourly, "temperature_2m", i),
                precipitation_probability=_value(hourly, "precipitation_probability", i),
                precipitation=_value(hourly, "precipitation", i),
                weather_code=_value(hourly, "weather_code", i),
                wind_speed=_value(hourly, "wind_speed_10m", i),
            )
        )
    return WeekendForecast(
        race_name=race.name, timezone=tz_name, days=days, sessions=tuple(sessions)
    )


def weekend_dates(race: Race) -> tuple[date, date]:
    """First and last day of the weekend, as UTC session dates (from Jolpica)."""
    days = [s.date for s in race.sessions]
    return min(days), max(days)


def fetch_forecast(race: Race, client: httpx.Client | None = None) -> WeekendForecast:
    # Session dates are UTC, but Open-Meteo reads start/end as circuit-local days. They differ
    # for evening/night sessions far from UTC (Las Vegas: FP1 Thu 16:30 PST = Fri 00:30 UTC),
    # so ask for one extra day on each side; `parse_forecast` drops days without a session.
    first, last = weekend_dates(race)
    start, end = first - timedelta(days=1), last + timedelta(days=1)
    params = {
        "latitude": race.circuit.latitude,
        "longitude": race.circuit.longitude,
        "daily": ",".join(DAILY),
        "hourly": ",".join(HOURLY),
        "timezone": "auto",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    owns_client = client is None
    client = client or httpx.Client(timeout=20.0)
    try:
        response = client.get(BASE_URL, params=params)
        body = response.json()
    except httpx.HTTPError as exc:
        raise WeatherError(f"failed to fetch the forecast: {exc}") from exc
    except ValueError as exc:
        raise WeatherError("invalid JSON from Open-Meteo") from exc
    finally:
        if owns_client:
            client.close()
    if isinstance(body, dict) and body.get("error"):
        reason = body.get("reason", "unknown error")
        if "out of allowed range" in reason:
            raise ForecastNotAvailable(f"no forecast yet for {start}..{end}: {reason}")
        raise WeatherError(f"Open-Meteo: {reason}")
    if response.status_code != 200 or not isinstance(body, dict):
        raise WeatherError(f"Open-Meteo answered HTTP {response.status_code}")
    return parse_forecast(body, race)


def _num(value: float | int | None, unit: str, digits: int = 0) -> str:
    return "-" if value is None else f"{value:.{digits}f}{unit}"


def format_forecast(forecast: WeekendForecast) -> str:
    lines = [f"Weather for {forecast.race_name} (circuit time: {forecast.timezone})", ""]
    for d in forecast.days:
        temps = f"{_num(d.temperature_min, '')} to {_num(d.temperature_max, '°C')}"
        rain = (
            f"{_num(d.precipitation_probability_max, '%')} ({_num(d.precipitation_sum, ' mm', 1)})"
        )
        lines.append(
            f"  {d.date:%a %d %b}: {d.description}, {temps}, rain {rain},"
            f" wind up to {_num(d.wind_speed_max, ' km/h')}"
        )
    if forecast.sessions:
        lines += ["", "At session start"]
        tz = ZoneInfo(forecast.timezone)
        for s in forecast.sessions:
            start = s.start_utc.astimezone(tz)
            lines.append(
                f"  {s.kind.value:<17} {start:%a %H:%M} local ({s.hour_local:%H:%M} forecast):"
                f" {s.description}, {_num(s.temperature, '°C')},"
                f" rain {_num(s.precipitation_probability, '%')},"
                f" wind {_num(s.wind_speed, ' km/h')}"
            )
    lines.append(f"Source: {SOURCE_NAME}.")
    return "\n".join(lines)

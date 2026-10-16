"""All collected facts for the next race weekend, as one JSON document with sources.

The digest generator (F13) receives only this document. Every section lists the exact requests
it was built from; a section that could not be collected is `null` and explained in `missing`,
so the generator can say what is unknown instead of guessing.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from decimal import Decimal
from typing import Any

import httpx

from f1brief import (
    drivers,
    form,
    jolpica,
    news,
    openf1,
    pace,
    schedule,
    standings,
    track_history,
    weather,
)
from f1brief.schedule import WARSAW, Race

SCHEMA_VERSION = 1
NEWS_DAYS = 7

_HOSTS = {
    "api.jolpi.ca": jolpica.SOURCE_NAME,
    "api.openf1.org": openf1.SOURCE_NAME,
    "api.open-meteo.com": weather.SOURCE_NAME,
}
_FEEDS = {feed.url: feed.name for feed in news.FEEDS}

# Errors that mean "this section could not be collected"; anything else is a bug and propagates.
_COLLECTION_ERRORS = (
    jolpica.JolpicaError,
    openf1.OpenF1Error,
    news.NewsError,
    weather.WeatherError,
)


@dataclass
class _Recorder:
    """Records every request made through the client, so each section can cite them."""

    urls: list[str] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> None:
        url = str(request.url)
        if url not in self.urls:  # OpenF1 retries after a 429 repeat the same URL
            self.urls.append(url)

    def take(self) -> list[dict[str, str]]:
        sources = [{"name": _source_name(url), "url": url} for url in self.urls]
        self.urls.clear()
        return sources


def _source_name(url: str) -> str:
    if url in _FEEDS:
        return _FEEDS[url]
    return _HOSTS.get(httpx.URL(url).host, httpx.URL(url).host)


def _num(value: Decimal | float | int | None) -> float | int | None:
    """A JSON number. Decimals become int when whole, else float (exact for these sizes)."""
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _time(value: datetime | None, tz: tzinfo = WARSAW) -> str | None:
    return value.astimezone(tz).isoformat() if value else None


def _day(value: date | None) -> str | None:
    return value.isoformat() if value else None


# --- sections ---------------------------------------------------------------------------


def race_facts(race: Race, tz: tzinfo = WARSAW) -> dict[str, Any]:
    return {
        "season": race.season,
        "round": race.round,
        "name": race.name,
        "circuit": {
            "id": race.circuit.id,
            "name": race.circuit.name,
            "locality": race.circuit.locality,
            "country": race.circuit.country,
        },
        "sprint_weekend": race.is_sprint_weekend,
        "sessions": [
            {
                "session": s.kind.value,
                "date": _day(s.date),
                # null while the start time is to be confirmed; only the date is known then.
                "start_warsaw": _time(s.start_utc, tz),
            }
            for s in race.sessions
        ],
    }


def _change(change: standings.StandingChange, favourite: bool) -> dict[str, Any]:
    s = change.current
    row: dict[str, Any] = {
        "position": s.position,
        "position_text": s.position_text,
        "id": s.id,
        "name": s.name,
        "points": _num(s.points),
        "wins": s.wins,
        "points_gained": _num(change.points_gained),
        # Positive = moved up; null when new to the table or a position is unknown.
        "positions_gained": change.positions_gained,
        "new_in_table": change.previous is None,
        "favourite": favourite,
    }
    if s.code is not None or s.teams:
        row["code"] = s.code
        row["teams"] = [t.name for t in s.teams]
    return row


def standings_facts(
    rep: standings.StandingsReport,
    favourite_drivers: Iterable[str] = drivers.FAVOURITES,
    favourite_team: str = track_history.FAVOURITE_TEAM,
) -> dict[str, Any]:
    favourite_drivers = set(favourite_drivers)
    return {
        "season": rep.season,
        "after_round": rep.round,
        "compared_with_round": rep.previous_round,
        "drivers": [_change(c, c.current.id in favourite_drivers) for c in rep.drivers],
        "constructors": [_change(c, c.current.id == favourite_team) for c in rep.constructors],
    }


def _race_result(x: form.RaceResult) -> dict[str, Any]:
    return {
        "driver": x.driver_name,
        "code": x.driver_code,
        "team": x.constructor_name,
        # null = pit-lane start
        "grid": x.grid,
        # A number when classified, else a code: R retired, W withdrawn, D disqualified, ...
        "finish": x.position_text,
        "classified": x.classified,
        "status": x.status,
        "points": _num(x.points),
    }


def _qualifying(q: form.QualifyingResult) -> dict[str, Any]:
    return {
        "driver": q.driver_name,
        "code": q.driver_code,
        "position": q.position,
        "q1": q.q1,
        "q2": q.q2,
        "q3": q.q3,
    }


def team_form_facts(f: form.TeamForm) -> dict[str, Any]:
    return {
        "team_id": f.constructor_id,
        "team": f.constructor_name,
        "points_note": "Grand Prix points only; sprint points are not included.",
        "rounds": [
            {
                "season": r.season,
                "round": r.round,
                "race_name": r.race_name,
                "points": _num(r.points),
                "qualifying": [_qualifying(q) for q in r.qualifying],
                "race": [_race_result(x) for x in r.race],
            }
            for r in f.rounds
        ],
        "summary": {
            "points": _num(f.points),
            "average_best_qualifying": _num(f.average_best_qualifying),
            "average_finish_classified": _num(f.average_finish),
            "not_classified": f.non_classified,
            "drivers": [
                {
                    "driver": d.driver_name,
                    "starts": d.starts,
                    "points": _num(d.points),
                    "average_qualifying": _num(d.average_qualifying),
                    "average_finish_classified": _num(d.average_finish),
                    "not_classified": d.non_classified,
                }
                for d in f.drivers()
            ],
        },
    }


def _head_to_head(h: drivers.HeadToHead) -> dict[str, int]:
    return {"ahead": h.ahead, "behind": h.behind, "undecided": h.undecided}


def driver_facts(f: drivers.DriverForm) -> dict[str, Any]:
    rounds = []
    for r in f.rounds:
        mate = r.teammate_race
        rounds.append(
            {
                "season": r.season,
                "round": r.round,
                "race_name": r.race_name,
                "team": r.constructor_name,
                "qualifying_position": r.qualifying.position if r.qualifying else None,
                "grid": r.race.grid,
                "finish": r.race.position_text,
                "status": r.race.status,
                "points": _num(r.race.points),
                "teammate": mate.driver_name if mate else None,
                "teammate_qualifying_position": (
                    r.teammate_qualifying.position if r.teammate_qualifying else None
                ),
                "teammate_finish": mate.position_text if mate else None,
                "teammate_points": _num(mate.points) if mate else None,
                # null when the comparison cannot be made that round
                "qualified_ahead_of_teammate": r.qualified_ahead,
                "finished_ahead_of_teammate": r.finished_ahead,
            }
        )
    return {
        "driver_id": f.driver_id,
        "driver": f.driver_name,
        "teammates": f.teammates(),
        "points_note": "Grand Prix points only; sprint points are not included.",
        "rounds": rounds,
        "summary": {
            "points": _num(f.points),
            "teammate_points": _num(f.teammate_points),
            "average_qualifying": _num(f.average_qualifying),
            "average_finish_classified": _num(f.average_finish),
            "not_classified": f.non_classified,
            "qualifying_head_to_head": _head_to_head(f.qualifying_head_to_head),
            "race_head_to_head": _head_to_head(f.race_head_to_head),
        },
    }


def _pace(report: pace.PaceReport, p: pace.DriverPace) -> dict[str, Any]:
    return {
        "driver": p.driver.name,
        "code": p.driver.code,
        "team": p.driver.team,
        "finish_position": p.position,
        "median_long_run_lap_s": _num(p.median),
        "clean_laps": p.laps,
        # Seconds per lap slower than the reference (negative = faster).
        "delta_to_reference_s": _num(report.delta(p)),
    }


def pace_facts(report: pace.PaceReport) -> dict[str, Any]:
    return {
        "race_name": report.race_name,
        "date": report.date.date().isoformat(),
        "method": (
            f"Median lap on stints of at least {pace.MIN_STINT_LAPS} laps, without lap 1, pit "
            f"in/out laps, safety car / VSC / red flag laps (plus the lap after) and laps slower "
            f"than {pace.OUTLIER_FACTOR}x the driver's median; at least {pace.MIN_LAPS} clean "
            f"laps needed. Reference: the best such pace among the top {pace.LEADERS} finishers."
        ),
        "neutralised_laps": sorted(report.neutralised_laps),
        "reference": _pace(report, report.reference) if report.reference else None,
        "leaders": [_pace(report, p) for p in report.leaders],
        "favourites": [_pace(report, p) for p in report.favourites],
    }


def track_facts(history: track_history.TrackHistory) -> dict[str, Any]:
    team_points = history.team_points(track_history.FAVOURITE_TEAM)
    return {
        "circuit": {
            "id": history.circuit.id,
            "name": history.circuit.name,
            "locality": history.circuit.locality,
            "country": history.circuit.country,
        },
        "editions": [
            {
                "season": ed.season,
                "round": ed.round,
                "race_name": ed.race_name,
                "favourite_team_points": _num(points),
                "entries": [
                    {
                        **_race_result(e.race),
                        "qualifying_position": e.qualifying.position if e.qualifying else None,
                    }
                    for e in ed.entries
                ],
            }
            for ed, points in zip(history.editions, team_points, strict=True)
        ],
        "drivers": [
            {
                "driver": d.driver_name,
                "teams": list(d.teams),
                "starts": d.starts,
                "points": _num(d.points),
                "best_finish": d.best_finish.position if d.best_finish else None,
                "average_finish_classified": _num(d.average_finish),
                "average_qualifying": _num(d.average_qualifying),
                "not_classified": d.non_classified,
            }
            for d in history.drivers()
        ],
    }


def news_facts(report: news.NewsReport) -> dict[str, Any]:
    return {
        "since": report.since.isoformat(),
        "failed_feeds": list(report.failed_feeds),
        "items": [
            {
                "title": i.title,
                "link": i.link,
                # null when no feed gave a date
                "published": i.published.isoformat() if i.published else None,
                "summary": i.summary,
                "sources": list(i.sources),
                "favourites": sorted(i.favourites),
                "topics": sorted(i.topics),
                "ocon_future": i.ocon_future,
            }
            for i in report.items
        ],
    }


def weather_facts(forecast: weather.WeekendForecast) -> dict[str, Any]:
    return {
        "circuit_timezone": forecast.timezone,
        "days": [
            {
                "date": _day(d.date),
                "description": d.description,
                "temperature_max_c": d.temperature_max,
                "temperature_min_c": d.temperature_min,
                "precipitation_probability_max_pct": d.precipitation_probability_max,
                "precipitation_mm": d.precipitation_sum,
                "wind_speed_max_kmh": d.wind_speed_max,
            }
            for d in forecast.days
        ],
        "sessions": [
            {
                "session": s.kind.value,
                "start_warsaw": _time(s.start_utc),
                "forecast_hour_local": s.hour_local.isoformat(),
                "description": s.description,
                "temperature_c": s.temperature,
                "precipitation_probability_pct": s.precipitation_probability,
                "precipitation_mm": s.precipitation,
                "wind_speed_kmh": s.wind_speed,
            }
            for s in forecast.sessions
        ],
    }


# --- assembly ---------------------------------------------------------------------------


SECTIONS = ("race", "standings", "team_form", "drivers", "pace", "track", "news", "weather")


def collect(
    now: datetime,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Fetches every section for the next race weekend and returns the facts document.

    A section that fails is `null` and listed in `missing`; the others are still collected.
    `now` must be timezone-aware.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    recorder = _Recorder()
    client.event_hooks["request"].append(recorder)
    sections: dict[str, dict[str, Any] | None] = {}
    missing: list[dict[str, str]] = []

    def section(name: str, build: Callable[[], dict[str, Any]]) -> None:
        try:
            data = build()
        except weather.ForecastNotAvailable as exc:
            sections[name] = None
            missing.append({"section": name, "reason": f"beyond the forecast range: {exc}"})
        except _COLLECTION_ERRORS as exc:
            sections[name] = None
            missing.append({"section": name, "reason": str(exc)})
        else:
            sources = recorder.take()
            if name == "news":  # a feed that could not be read is not a source
                sources = [s for s in sources if s["name"] not in data["failed_feeds"]]
            sections[name] = {**data, "sources": sources}
        recorder.take()  # a failed section's requests are not anyone's source

    try:
        race: Race | None = None

        def next_race() -> dict[str, Any]:
            nonlocal race
            race = schedule.next_race(schedule.fetch_schedule(client=client), now)
            if race is None:
                raise schedule.ScheduleError("no upcoming race in the current season's calendar")
            return race_facts(race)

        section("race", next_race)
        upcoming = race
        section("standings", lambda: standings_facts(standings.fetch_report(client=client)))
        section(
            "team_form",
            lambda: team_form_facts(
                form.fetch_team_form(track_history.FAVOURITE_TEAM, client=client)
            ),
        )
        section(
            "drivers",
            lambda: {
                "favourites": [
                    driver_facts(drivers.fetch_driver_form(d, client=client))
                    for d in drivers.FAVOURITES
                ]
            },
        )
        pace_kwargs = {"sleep": sleep} if sleep is not None else {}
        section("pace", lambda: pace_facts(pace.fetch_report(now, client, **pace_kwargs)))
        if upcoming is None:
            for name in ("track", "weather"):
                sections[name] = None
                missing.append({"section": name, "reason": "the next race is unknown"})
        else:
            section(
                "track",
                lambda: track_facts(
                    track_history.fetch_track_history(
                        upcoming.circuit.id, upcoming.race.date, client=client
                    )
                ),
            )
        section(
            "news",
            lambda: news_facts(news.fetch_news(news.default_since(now, NEWS_DAYS), client=client)),
        )
        if upcoming is not None:
            section("weather", lambda: weather_facts(weather.fetch_forecast(upcoming, client)))
    finally:
        client.event_hooks["request"].remove(recorder)
        if owns_client:
            client.close()

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.astimezone(WARSAW).isoformat(),
        "timezone": str(WARSAW),
        "favourites": {
            "team": track_history.FAVOURITE_TEAM,
            "drivers": list(drivers.FAVOURITES),
        },
        "sections": {name: sections[name] for name in SECTIONS},
        "missing": missing,
    }

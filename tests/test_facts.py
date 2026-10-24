import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import facts_transport
import httpx
import pytest

from f1brief import facts
from f1brief.cli import main
from f1brief.config import DEFAULT_FAVOURITES

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
JOLPICA = "https://api.jolpi.ca/ergast/f1"
# No story is followed (see test_digest_stories.py for the section itself).
NO_STORIES = {
    "since": "2026-10-01T14:00:00+02:00",
    "since_last_digest": False,
    "followed": [],
    "sources": [],
}


def collect(
    seen: list[str] | None = None, override: facts_transport.Override | None = None
) -> dict[str, Any]:
    seen = [] if seen is None else seen
    with facts_transport.client(seen, override) as client:
        return facts.collect(
            NOW, DEFAULT_FAVOURITES, client, sleep=lambda _: None, stories=lambda: NO_STORIES
        )


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return collect()


def favourite(rows: list[dict[str, Any]], key: str, value: str) -> dict[str, Any]:
    return next(r for r in rows if r[key] == value)


def test_every_section_is_collected(document: dict[str, Any]) -> None:
    assert document["missing"] == []
    assert list(document["sections"]) == list(facts.SECTIONS)
    assert all(document["sections"].values())
    assert document["schema_version"] == 3
    assert document["sections"]["stories"] == NO_STORIES
    assert document["generated_at"] == "2026-10-15T14:00:00+02:00"
    assert document["favourites"] == {
        "teams": [{"id": "mclaren", "name": "McLaren"}, {"id": "haas", "name": "Haas"}],
        "drivers": [
            {"id": "russell", "name": "George Russell", "track_future": False},
            {"id": "ocon", "name": "Esteban Ocon", "track_future": True},
            {"id": "bearman", "name": "Oliver Bearman", "track_future": False},
        ],
    }


def test_document_is_plain_json(document: dict[str, Any]) -> None:
    # No default= hook: anything that is not a JSON type (Decimal, datetime) would fail here.
    assert json.loads(json.dumps(document)) == document


def test_race_in_warsaw_time_across_the_clock_change(document: dict[str, Any]) -> None:
    race = document["sections"]["race"]

    assert (race["season"], race["round"], race["name"]) == (2026, 18, "United States Grand Prix")
    assert race["circuit"]["name"] == "Circuit of the Americas"
    sessions = {s["session"]: s["start_local"] for s in race["sessions"]}
    # FP1 12:30 CDT = 17:30 UTC = 19:30 CEST; Poland is back on CET (UTC+1) by race day.
    assert sessions["FP1"] == "2026-10-23T19:30:00+02:00"
    assert sessions["Race"] == "2026-10-25T21:00:00+01:00"
    assert race["sources"] == [{"name": "Jolpica-F1", "url": f"{JOLPICA}/current.json?limit=100"}]


def test_standings_mark_favourites_and_changes(document: dict[str, Any]) -> None:
    table = document["sections"]["standings"]

    assert (table["after_round"], table["compared_with_round"]) == (17, 16)
    russell = favourite(table["drivers"], "id", "russell")
    # P9 in Singapore = 2 points; 236 -> 238, still second.
    assert russell | {"teams": None} == {
        "position": 2,
        "position_text": "2",
        "id": "russell",
        "name": "George Russell",
        "points": 238,
        "wins": 3,
        "points_gained": 2,
        "positions_gained": 0,
        "new_in_table": False,
        "favourite": True,
        "code": "RUS",
        "teams": None,
    }
    assert {d["id"] for d in table["drivers"] if d["favourite"]} == {"russell", "ocon", "bearman"}
    mclaren = favourite(table["constructors"], "id", "mclaren")
    # Grand Prix P4 + P5 = 12 + 10, plus one sprint point: standings include sprints.
    assert (mclaren["points_gained"], mclaren["favourite"]) == (23, True)
    assert "code" not in mclaren
    assert {c["id"] for c in table["constructors"] if c["favourite"]} == {"mclaren", "haas"}


def test_team_form_totals(document: dict[str, Any]) -> None:
    team, haas = document["sections"]["team_form"]["teams"]

    assert [r["round"] for r in team["rounds"]] == [13, 14, 15, 16, 17]
    last = team["rounds"][-1]
    assert [(x["code"], x["finish"], x["points"]) for x in last["race"]] == [
        ("NOR", "4", 12),
        ("PIA", "5", 10),
    ]
    assert last["points"] == 22
    assert team["summary"]["points"] == 73
    assert "sprint" in team["points_note"]
    assert (haas["team_id"], haas["team"]) == ("haas", "Haas F1 Team")
    assert [r["round"] for r in haas["rounds"]] == [13, 14, 15, 16, 17]


def test_driver_comparison_with_teammate(document: dict[str, Any]) -> None:
    russell, ocon, _ = document["sections"]["drivers"]["favourites"]

    assert (russell["driver"], ocon["driver"]) == ("George Russell", "Esteban Ocon")
    assert russell["teammates"] == ["Andrea Kimi Antonelli"]
    singapore = russell["rounds"][-1]
    assert singapore | {"race_name": None} == {
        "season": 2026,
        "round": 17,
        "race_name": None,
        "team": "Mercedes",
        "qualifying_position": 6,
        "grid": 19,
        "finish": "9",
        "status": "Finished",
        "points": 2,
        "teammate": "Andrea Kimi Antonelli",
        "teammate_qualifying_position": 4,
        "teammate_finish": "3",
        "teammate_points": 15,
        "qualified_ahead_of_teammate": False,
        "finished_ahead_of_teammate": False,
    }
    assert russell["summary"]["race_head_to_head"] == {"ahead": 1, "behind": 4, "undecided": 0}
    assert ocon["teammates"] == ["Oliver Bearman"]
    assert "head_to_head_note" not in ocon["summary"]


def test_two_favourite_teammates_are_compared_once(document: dict[str, Any]) -> None:
    _, ocon, bearman = document["sections"]["drivers"]["favourites"]

    # Ocon (listed first) has the comparison with Bearman; Bearman's rounds point to it.
    assert ocon["rounds"][-1]["teammate"] == "Oliver Bearman"
    assert "finished_ahead_of_teammate" in ocon["rounds"][-1]
    assert (
        ocon["summary"]["race_head_to_head"]["ahead"]
        + ocon["summary"]["race_head_to_head"]["behind"]
        > 0
    )
    for row in bearman["rounds"]:
        assert row["teammate"] == "Esteban Ocon"
        assert row["head_to_head_in"] == "ocon"
        assert "finished_ahead_of_teammate" not in row
        assert "teammate_finish" not in row
    assert bearman["summary"]["race_head_to_head"] == {"ahead": 0, "behind": 0, "undecided": 0}
    assert "counted once" in bearman["summary"]["head_to_head_note"]


def test_pace_explains_its_method(document: dict[str, Any]) -> None:
    pace = document["sections"]["pace"]

    assert pace["reference"]["code"] == "LEC"
    russell = favourite(pace["favourites"], "code", "RUS")
    # 98.747 - 97.220 s per lap, exact (no float noise).
    assert (russell["median_long_run_lap_s"], russell["delta_to_reference_s"]) == (98.747, 1.527)
    assert pace["neutralised_laps"] == [8, 9, 10, 39, 40, 41]
    assert "at least 8 laps" in pace["method"]
    assert "1.07x" in pace["method"]


def test_track_history_at_the_next_circuit(document: dict[str, Any]) -> None:
    track = document["sections"]["track"]

    assert track["circuit"]["id"] == "americas"
    mclaren = [
        (
            e["season"],
            [p["points"] for p in e["favourite_teams_points"] if p["team_id"] == "mclaren"],
        )
        for e in track["editions"]
    ]
    assert mclaren == [(2023, [18]), (2024, [22]), (2025, [28])]
    assert [p["team"] for p in track["editions"][0]["favourite_teams_points"]] == [
        "McLaren",
        "Haas",
    ]
    ocon = favourite(track["drivers"], "driver", "Esteban Ocon")
    assert ocon["teams"] == ["Alpine F1 Team", "Alpine F1 Team", "Haas F1 Team"]


def test_news_keeps_links_and_drops_failed_feeds_from_sources(document: dict[str, Any]) -> None:
    section = document["sections"]["news"]

    assert section["since"] == "2026-10-08T12:00:00+00:00"
    assert section["failed_feeds"] == ["Autosport", "RaceFans"]
    assert [i["link"] for i in section["items"]] == [
        "https://example.test/f1/ocon-2027",
        "https://example.test/ms/russell-penalty",
        "https://example.test/f1/mclaren-floor",
    ]
    assert section["items"][0]["sources"] == ["Formula 1", "BBC Sport"]
    assert section["items"][0]["future"] is True
    assert section["items"][0]["favourites"] == ["Esteban Ocon", "Haas"]
    assert [s["name"] for s in section["sources"]] == ["Formula 1", "Motorsport.com", "BBC Sport"]


def test_weather_per_session(document: dict[str, Any]) -> None:
    section = document["sections"]["weather"]

    assert section["circuit_timezone"] == "America/Chicago"
    fp1 = section["sessions"][0]
    assert (fp1["session"], fp1["forecast_hour_local"]) == ("FP1", "2026-10-23T12:00:00-05:00")
    assert fp1["temperature_c"] == 21.3
    (url,) = [s["url"] for s in section["sources"]]
    assert url.startswith("https://api.open-meteo.com/v1/forecast?latitude=30.1328")


def test_sources_are_exactly_the_requests_made() -> None:
    seen: list[str] = []
    document = collect(seen)

    cited = [s["url"] for section in document["sections"].values() for s in section["sources"]]
    failed_feeds = [
        "https://www.autosport.com/rss/f1/news/",
        "https://www.racefans.net/feed/",
    ]
    # Every request is cited by exactly one section, except feeds that could not be read.
    # (Ocon and Bearman both need the Haas rounds: one request each, one citation.)
    assert sorted(cited) == sorted({u for u in seen if u not in failed_feeds})
    assert len(cited) == len(set(cited))


def _fail(host: str) -> facts_transport.Override:
    def override(request: httpx.Request) -> httpx.Response | None:
        return httpx.Response(500) if request.url.host == host else None

    return override


def test_a_failed_source_only_removes_its_section() -> None:
    seen: list[str] = []
    document = collect(seen, _fail("api.openf1.org"))

    assert document["sections"]["pace"] is None
    (gap,) = document["missing"]
    assert gap["section"] == "pace"
    assert "500" in gap["reason"]
    assert all(v for k, v in document["sections"].items() if k != "pace")
    # The failed OpenF1 request is nobody's source.
    cited = {s["url"] for v in document["sections"].values() if v for s in v["sources"]}
    assert not any("openf1" in u for u in cited)


def test_without_the_calendar_track_and_weather_are_missing_too() -> None:
    def override(request: httpx.Request) -> httpx.Response | None:
        return httpx.Response(503) if request.url.path == "/ergast/f1/current.json" else None

    document = collect(override=override)

    assert [g["section"] for g in document["missing"]] == ["race", "track", "weather"]
    assert document["missing"][1]["reason"] == "the next race is unknown"
    assert document["sections"]["standings"] is not None
    assert document["sections"]["news"] is not None


def test_weather_beyond_the_horizon_is_explained() -> None:
    body = json.loads(
        (facts_transport.FIXTURES / "openmeteo" / "error_out_of_range.json").read_text()
    )

    def override(request: httpx.Request) -> httpx.Response | None:
        return httpx.Response(400, json=body) if request.url.host == "api.open-meteo.com" else None

    document = collect(override=override)

    (gap,) = document["missing"]
    assert gap["section"] == "weather"
    assert gap["reason"].startswith("beyond the forecast range")


def test_now_must_be_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        facts.collect(datetime(2026, 10, 15), DEFAULT_FAVOURITES)


def test_caller_client_is_left_open_without_the_hook() -> None:
    with facts_transport.client([]) as client:
        facts.collect(NOW, DEFAULT_FAVOURITES, client, sleep=lambda _: None)
        assert client.event_hooks["request"] == []
        assert not client.is_closed


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("25"), 25),
        (Decimal("25.0"), 25),
        (Decimal("0.5"), 0.5),
        (Decimal("97.220"), 97.22),
        (None, None),
        (3, 3),
        (21.3, 21.3),
    ],
)
def test_numbers(value: Any, expected: Any) -> None:
    result = facts._num(value)

    assert result == expected
    assert type(result) is type(expected)


def test_cli_writes_the_document(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from f1brief import cli

    stub = {
        "sections": {"race": {"round": 18}, "weather": None},
        "missing": [{"section": "weather", "reason": "beyond the forecast range"}],
    }
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    monkeypatch.setattr(facts, "collect", lambda now, favourites, stories=None, tz=None: stub)
    target = tmp_path / "facts.json"

    assert main(["facts", "--output", str(target)]) == 0
    assert json.loads(target.read_text(encoding="utf-8")) == stub
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "warning: no weather facts: beyond the forecast range\n"

    assert main(["facts"]) == 0
    assert json.loads(capsys.readouterr().out) == stub


def test_cli_fails_when_nothing_was_collected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = {
        "sections": {"race": None},
        "missing": [{"section": "race", "reason": "network down"}],
    }
    monkeypatch.setattr(facts, "collect", lambda now, favourites, stories=None, tz=None: stub)

    assert main(["facts"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: race: network down\n"

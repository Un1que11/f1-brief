"""The message after qualifying (F19): when it is due, and its facts."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import load_fixture
from facts_transport import client as fixture_client

from f1brief import after_qualifying as aq
from f1brief import schedule
from f1brief.schedule import Session, SessionKind
from f1brief.send_log import SendLog

QUALI_START = datetime(2026, 10, 24, 21, 0, tzinfo=UTC)  # round 18, Austin
RACE_START = datetime(2026, 10, 25, 20, 0, tzinfo=UTC)


@pytest.fixture
def races(schedule_2026: dict[str, Any]) -> list[schedule.Race]:
    return schedule.parse_schedule(schedule_2026)


@pytest.fixture
def austin(races: list[schedule.Race]) -> schedule.Race:
    return next(r for r in races if r.round == 18)


@pytest.fixture
def singapore(races: list[schedule.Race]) -> schedule.Race:
    return next(r for r in races if r.round == 17)


@pytest.fixture
def log(tmp_path: Path):
    with SendLog(tmp_path / "state.db") as opened:
        yield opened


@pytest.fixture
def singapore_results(singapore: schedule.Race) -> list:
    """GET /ergast/f1/2026/17/qualifying.json, recorded 2026-10-16."""
    return aq.parse_classification(load_fixture("jolpica/qualifying_2026_r17.json"), singapore)


# --- the window -------------------------------------------------------------------------


def test_window_opens_after_qualifying_and_closes_at_the_race(austin: schedule.Race) -> None:
    w = aq.window(austin)
    assert w is not None
    assert (w.opens, w.closes) == (QUALI_START + aq.OPENS_AFTER, RACE_START)


def test_window_with_times_to_be_confirmed(austin: schedule.Race) -> None:
    tbc = replace(
        austin,
        sessions=tuple(
            replace(s, start_utc=None)
            if s.kind in (SessionKind.QUALIFYING, SessionKind.RACE)
            else s
            for s in austin.sessions
        ),
    )
    w = aq.window(tbc)
    assert w is not None
    # Midnight Warsaw (UTC+2 in October) after the qualifying day, and before the race day:
    # the same moment, so nothing is sent.
    assert w.opens == w.closes == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)


def test_no_window_without_qualifying(austin: schedule.Race) -> None:
    no_quali = replace(
        austin, sessions=tuple(s for s in austin.sessions if s.kind is not SessionKind.QUALIFYING)
    )
    assert aq.window(no_quali) is None


# --- the decision -----------------------------------------------------------------------


def test_before_the_weekend_it_is_not_due_nor_worth_showing(
    races: list[schedule.Race], log: SendLog
) -> None:
    decision = aq.decide(races, datetime(2026, 10, 21, 8, tzinfo=UTC), log)
    assert (decision.due, decision.under_way) == (False, False)
    assert decision.race is not None and decision.race.round == 18


def test_after_fp1_it_waits_for_qualifying(races: list[schedule.Race], log: SendLog) -> None:
    decision = aq.decide(races, datetime(2026, 10, 24, 12, tzinfo=UTC), log)
    assert (decision.due, decision.under_way) == (False, True)
    assert aq.describe(decision) == (
        "After qualifying (round 18 of 2026): not yet: due from Sun 25 Oct 01:00."
    )


def test_due_between_qualifying_and_the_race(races: list[schedule.Race], log: SendLog) -> None:
    decision = aq.decide(races, datetime(2026, 10, 25, 7, tzinfo=UTC), log)
    assert decision.due
    assert decision.reason == "due: the race starts Sun 25 Oct 21:00"


def test_not_due_once_sent_or_claimed(races: list[schedule.Race], log: SendLog) -> None:
    now = datetime(2026, 10, 25, 7, tzinfo=UTC)
    assert log.claim_qualifying(2026, 18, now)
    decision = aq.decide(races, now, log)
    assert not decision.due
    assert "not resent automatically" in decision.reason
    log.mark_qualifying_sent(2026, 18, now, 1)
    # Summer time ended at 01:00 UTC that morning: Warsaw is UTC+1 again.
    assert aq.decide(races, now, log).reason == "already sent at Sun 25 Oct 08:00"


def test_after_the_race_starts_the_next_weekend_is_considered(
    races: list[schedule.Race], log: SendLog
) -> None:
    decision = aq.decide(races, RACE_START, log)
    assert not decision.due and not decision.under_way
    assert decision.race is not None and decision.race.round == 19


def test_the_digest_log_does_not_count(races: list[schedule.Race], log: SendLog) -> None:
    now = datetime(2026, 10, 25, 7, tzinfo=UTC)
    assert log.claim(2026, 18, now)
    log.mark_sent(2026, 18, now, 3)
    assert aq.decide(races, now, log).due


def test_naive_now_is_rejected(races: list[schedule.Race], log: SendLog) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        aq.decide(races, datetime(2026, 10, 25, 7), log)


# --- the classification -----------------------------------------------------------------


def test_parses_the_recorded_classification(singapore_results: list) -> None:
    assert len(singapore_results) == 22
    pole = singapore_results[0]
    assert (pole.position, pole.driver_id, pole.q3) == (1, "max_verstappen", "1:31.373")
    # Hadjar set no time: Jolpica gives an empty Q1.
    assert (singapore_results[-1].driver_id, singapore_results[-1].q1) == ("hadjar", None)


def test_an_unpublished_classification_is_empty(austin: schedule.Race) -> None:
    payload = load_fixture("jolpica/qualifying_2026_r18_not_yet.json")
    assert aq.parse_classification(payload, austin) == []


def test_another_rounds_classification_is_not_used(
    austin: schedule.Race, singapore_results: list
) -> None:
    assert aq.parse_classification(load_fixture("jolpica/qualifying_2026_r17.json"), austin) == []


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("1:31.712", Decimal("91.712")), ("59.5", Decimal("59.5")), ("", None), (None, None)],
)
def test_lap_seconds(text: str | None, seconds: Decimal | None) -> None:
    assert aq.lap_seconds(text) == seconds


def test_an_unreadable_lap_time_is_none() -> None:
    assert aq.lap_seconds("1:3x.000") is None


def test_last_part(singapore_results: list) -> None:
    by_id = {q.driver_id: q for q in singapore_results}
    assert [aq.last_part(by_id[d]) for d in ("russell", "ocon", "alonso", "hadjar")] == [
        "Q3",
        "Q2",
        "Q1",
        None,
    ]


def test_fetch_classification_asks_for_the_round(singapore: schedule.Race) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=load_fixture("jolpica/qualifying_2026_r17.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert len(aq.fetch_classification(singapore, client)) == 22
    assert seen == ["https://api.jolpi.ca/ergast/f1/2026/17/qualifying.json?limit=100"]


def test_a_failed_request_is_a_form_error(singapore: schedule.Race) -> None:
    with (
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client,
        pytest.raises(aq.form.FormError),
    ):
        aq.fetch_classification(singapore, client)


# --- the qualifying section -------------------------------------------------------------


def test_favourites_are_drivers_and_teams_from_the_arguments(
    singapore: schedule.Race, singapore_results: list
) -> None:
    section = aq.qualifying_facts(
        singapore, singapore_results, ["russell", "ocon", "bearman"], ["mclaren"]
    )
    favourites = {f["driver_id"]: f for f in section["favourites"]}
    assert list(favourites) == ["norris", "russell", "piastri", "bearman", "ocon"]
    marked = [r["driver_id"] for r in section["classification"] if r["favourite"]]
    assert marked == list(favourites)
    # Two favourites who are teammates are each compared with the other.
    assert favourites["bearman"] == {
        "driver_id": "bearman",
        "driver": "Oliver Bearman",
        "team": "Haas F1 Team",
        "position": 12,
        "teammate": "Esteban Ocon",
        "teammate_position": 13,
        "ahead_of_teammate": True,
    }
    assert favourites["ocon"]["ahead_of_teammate"] is False
    assert favourites["russell"]["teammate"] == "Andrea Kimi Antonelli"
    assert section["favourite_drivers_not_classified"] == []
    assert section["sources"] == [
        {"name": "Jolpica-F1", "url": "https://api.jolpi.ca/ergast/f1/2026/17/qualifying.json"}
    ]


def test_no_favourites_means_none_marked(singapore: schedule.Race, singapore_results: list) -> None:
    section = aq.qualifying_facts(singapore, singapore_results, [], [])
    assert section["favourites"] == []
    assert not any(r["favourite"] for r in section["classification"])


def test_gaps_are_to_the_fastest_time_of_the_last_part_reached(
    singapore: schedule.Race, singapore_results: list
) -> None:
    rows = {
        r["driver_id"]: r
        for r in aq.qualifying_facts(singapore, singapore_results, [], [])["classification"]
    }
    assert (rows["russell"]["last_part"], rows["russell"]["gap_to_fastest_in_part_s"]) == (
        "Q3",
        0.374,  # 1:31.747 against Verstappen's 1:31.373
    )
    assert (rows["ocon"]["last_part"], rows["ocon"]["gap_to_fastest_in_part_s"]) == (
        "Q2",
        1.794,  # 1:33.323 against Hamilton's 1:31.529
    )
    assert rows["max_verstappen"]["gap_to_fastest_in_part_s"] == 0
    assert (rows["hadjar"]["last_part"], rows["hadjar"]["gap_to_fastest_in_part_s"]) == (
        None,
        None,
    )


def test_a_favourite_missing_from_the_classification_is_listed(
    singapore: schedule.Race, singapore_results: list
) -> None:
    section = aq.qualifying_facts(singapore, singapore_results, ["russell", "doohan"], [])
    assert section["favourite_drivers_not_classified"] == ["doohan"]


def test_a_driver_without_a_teammate_in_the_classification(
    singapore: schedule.Race, singapore_results: list
) -> None:
    alone = [q for q in singapore_results if q.driver_id != "antonelli"]
    (russell,) = aq.qualifying_facts(singapore, alone, ["russell"], [])["favourites"]
    assert (russell["teammate"], russell["ahead_of_teammate"]) == (None, None)


# --- the facts document -----------------------------------------------------------------

NOW = datetime(2026, 10, 10, 16, 0, tzinfo=UTC)  # Singapore, after qualifying


def test_collect_builds_every_section_with_sources(
    singapore: schedule.Race, singapore_results: list
) -> None:
    seen: list[str] = []
    with fixture_client(seen) as client:
        document = aq.collect(
            singapore, singapore_results, NOW, ["russell", "ocon"], ["mclaren"], client
        )

    assert document["kind"] == "after_qualifying"
    assert document["favourites"] == {"teams": ["mclaren"], "drivers": ["russell", "ocon"]}
    sections = document["sections"]
    assert list(sections) == list(aq.SECTIONS)
    assert sections["race"]["round"] == 17
    assert sections["qualifying"]["favourites"][0]["driver_id"] == "norris"
    assert [d["driver_id"] for d in sections["drivers"]["favourites"]] == ["russell", "ocon"]
    # The recorded results include the Singapore race itself; its form stops before it.
    for driver in sections["drivers"]["favourites"]:
        assert driver["rounds"] and all(r["round"] < 17 for r in driver["rounds"])
    assert all(s["name"] == "Jolpica-F1" for s in sections["drivers"]["sources"])
    news = sections["news"]
    assert news["since"] == "2026-10-09T08:30:00+00:00"  # FP1 in Singapore
    assert set(news["failed_feeds"]) == {"Autosport", "RaceFans"}
    assert {s["name"] for s in news["sources"]} <= {"Formula 1", "Motorsport.com", "BBC Sport"}
    assert document["missing"] == []
    # Only Jolpica and the feeds: no Telegram, no OpenF1, no weather.
    assert {httpx.URL(u).host for u in seen} <= {
        "api.jolpi.ca",
        "www.formula1.com",
        "www.motorsport.com",
        "www.autosport.com",
        "www.racefans.net",
        "feeds.bbci.co.uk",
    }


def test_a_failed_section_is_null_and_explained(
    singapore: schedule.Race, singapore_results: list
) -> None:
    def no_drivers(request: httpx.Request) -> httpx.Response | None:
        return httpx.Response(500) if "/drivers/" in request.url.path else None

    with fixture_client([], no_drivers) as client:
        document = aq.collect(singapore, singapore_results, NOW, ["russell"], [], client)

    assert document["sections"]["drivers"] is None
    assert [m["section"] for m in document["missing"]] == ["drivers"]
    assert document["sections"]["news"] is not None
    # The failed section's requests are not the next section's sources.
    assert all("jolpi" not in s["url"] for s in document["sections"]["news"]["sources"])


def test_news_since_falls_back_without_an_fp1_time(singapore: schedule.Race) -> None:
    fp1 = Session(SessionKind.FP1, date(2026, 10, 9), None)
    tbc = replace(singapore, sessions=(fp1, *singapore.sessions[1:]))
    assert aq.news_since(tbc, NOW) == NOW - aq.NEWS_FALLBACK


def test_collect_needs_an_aware_now(singapore: schedule.Race) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        aq.collect(singapore, [], datetime(2026, 10, 10), [], [])

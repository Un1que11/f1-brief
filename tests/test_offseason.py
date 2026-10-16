"""The off-season decision and the monthly summary's facts (F18): synthetic dates around the
recorded 2026 calendar (FP1 in Melbourne on 6 March, the last race in Abu Dhabi on 6 December)."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from f1brief import news, offseason, schedule
from f1brief.news import NewsItem
from f1brief.schedule import WARSAW, Race, Session, SessionKind
from f1brief.send_log import SendLog


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.fixture
def calendar(schedule_2026: dict[str, Any]) -> list[Race]:
    return schedule.parse_schedule(schedule_2026)


@pytest.fixture
def log(tmp_path: Path):
    with SendLog(tmp_path / "state.db") as opened:
        yield opened


def shifted(race: Race, days: int, season: int) -> Race:
    """`race` moved by `days` into `season` (synthetic: a calendar that is not out yet)."""

    def move(s: Session) -> Session:
        start = s.start_utc + timedelta(days=days) if s.start_utc else None
        return replace(s, date=s.date + timedelta(days=days), start_utc=start)

    return replace(race, season=season, sessions=tuple(move(s) for s in race.sessions))


class NextSeason:
    def __init__(self, races: list[Race] | None = None) -> None:
        self.races = races or []
        self.calls = 0

    def __call__(self) -> list[Race]:
        self.calls += 1
        return self.races


# --- is it the off-season --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "off"),
    [
        (date(2026, 1, 15), True),
        (date(2026, 3, 5), True),
        (date(2026, 3, 6), False),  # FP1 in Melbourne
        (date(2026, 4, 15), False),  # a month without a race is still the season
        (date(2026, 12, 6), False),  # the last race
        (date(2026, 12, 7), True),
    ],
)
def test_off_season_is_before_the_first_fp1_or_after_the_last_race(
    calendar: list[Race], day: date, off: bool
) -> None:
    assert offseason.is_off_season(calendar, day) is off


def test_a_year_without_a_calendar_is_off_season() -> None:
    assert offseason.is_off_season([], date(2027, 1, 15)) is True


def test_the_first_fp1_counts_by_its_warsaw_date(calendar: list[Race]) -> None:
    first = calendar[0]
    # 23:30 UTC on 5 March is 00:30 on 6 March in Warsaw.
    fp1 = Session(SessionKind.FP1, date(2026, 3, 5), utc(2026, 3, 5, 23, 30))
    moved = [replace(first, sessions=(fp1, *first.sessions[1:])), *calendar[1:]]
    assert offseason.is_off_season(moved, date(2026, 3, 5)) is True
    assert offseason.is_off_season(moved, date(2026, 3, 6)) is False


def test_period_starts_on_the_first_of_the_previous_month() -> None:
    assert offseason.period_start(date(2026, 1, 3)) == datetime(2025, 12, 1, tzinfo=WARSAW)
    assert offseason.period_start(date(2026, 2, 1)) == datetime(2026, 1, 1, tzinfo=WARSAW)


# --- the decision ------------------------------------------------------------------------------


def test_due_early_in_an_off_season_month(calendar: list[Race], log: SendLog) -> None:
    next_season = NextSeason()
    decision = offseason.decide(calendar, utc(2026, 1, 3, 8), log, next_season)

    assert (decision.off_season, decision.due, decision.month) == (True, True, "2026-01")
    assert decision.since == datetime(2025, 12, 1, tzinfo=WARSAW)
    assert decision.next_race is not None and decision.next_race.round == 1
    assert decision.reason == "due: the 2026-01 summary"
    # Before the first race the next season's calendar is not needed.
    assert next_season.calls == 0


def test_the_month_follows_warsaw_time(calendar: list[Race], log: SendLog) -> None:
    # 23:30 UTC on 31 January is already 1 February in Warsaw.
    decision = offseason.decide(calendar, utc(2026, 1, 31, 23, 30), log, NextSeason())
    assert (decision.due, decision.month) == (True, "2026-02")


def test_not_due_in_season(calendar: list[Race], log: SendLog) -> None:
    next_season = NextSeason()
    decision = offseason.decide(calendar, utc(2026, 4, 2, 8), log, next_season)
    assert (decision.off_season, decision.due) == (False, False)
    assert next_season.calls == 0


def test_not_due_after_the_first_days_of_the_month(calendar: list[Race], log: SendLog) -> None:
    decision = offseason.decide(calendar, utc(2026, 1, 8, 8), log, NextSeason())
    assert (decision.off_season, decision.due) == (True, False)
    assert decision.reason == "the monthly summary goes out in the first 7 days of a month"


def test_not_due_when_the_season_starts_soon(calendar: list[Race], log: SendLog) -> None:
    decision = offseason.decide(calendar, utc(2026, 3, 1, 8), log, NextSeason())
    assert decision.due is False
    assert decision.reason == (
        "Australian Grand Prix starts within 14 days; its digest comes instead"
    )


def test_not_due_right_after_the_last_race(calendar: list[Race], log: SendLog) -> None:
    next_season = NextSeason()
    decision = offseason.decide(calendar, utc(2026, 12, 7, 8), log, next_season)
    assert (decision.off_season, decision.due) == (True, False)
    assert decision.reason == "the season ended less than 14 days ago"
    assert next_season.calls == 1


def test_after_an_early_finale_the_next_season_is_looked_up(
    calendar: list[Race], log: SendLog
) -> None:
    # The season ends with Las Vegas on 22 November: 2 December is 10 days later, 7 December 15.
    early = [r for r in calendar if r.round <= 21]
    first_2027 = shifted(calendar[0], 364, 2027)
    next_season = NextSeason([first_2027])

    assert offseason.decide(early, utc(2026, 12, 2, 8), log, next_season).due is False
    decision = offseason.decide(early, utc(2026, 12, 7, 8), log, next_season)
    assert (decision.due, decision.month, decision.next_race) == (True, "2026-12", first_2027)


def test_the_next_season_may_be_unknown(calendar: list[Race], log: SendLog) -> None:
    early = [r for r in calendar if r.round <= 21]
    decision = offseason.decide(early, utc(2026, 12, 7, 8), log, NextSeason())
    assert (decision.due, decision.next_race) == (True, None)
    decision = offseason.decide([], utc(2027, 1, 2, 8), log, NextSeason())
    assert (decision.due, decision.next_race) == (True, None)


def test_sent_once_per_month(calendar: list[Race], log: SendLog) -> None:
    log.claim_summary("2026-01", utc(2026, 1, 2, 8))
    log.mark_summary_sent("2026-01", utc(2026, 1, 2, 8, 1), 1)

    decision = offseason.decide(calendar, utc(2026, 1, 3, 8), log, NextSeason())
    assert decision.due is False
    assert decision.reason == "the 2026-01 summary was already sent at Fri 02 Jan 09:00"
    assert offseason.decide(calendar, utc(2026, 2, 1, 8), log, NextSeason()).due is True


def test_a_partial_summary_is_not_repeated(calendar: list[Race], log: SendLog) -> None:
    log.claim_summary("2026-01", utc(2026, 1, 2, 8))
    decision = offseason.decide(calendar, utc(2026, 1, 3, 8), log, NextSeason())
    assert decision.due is False
    assert "not resent automatically" in decision.reason


def test_now_must_be_aware(calendar: list[Race], log: SendLog) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        offseason.decide(calendar, datetime(2026, 1, 3, 8), log, NextSeason())


def test_describe(calendar: list[Race], log: SendLog) -> None:
    decision = offseason.decide(calendar, utc(2026, 1, 3, 8), log, NextSeason())
    assert offseason.describe(decision) == (
        "Off-season: monthly summary for 2026-01\nDue: the 2026-01 summary."
    )


# --- the facts document --------------------------------------------------------------------


SINCE = datetime(2025, 12, 1, tzinfo=WARSAW)
NOW = utc(2026, 1, 3, 8)


def item(title: str, link: str, published: datetime | None, source: str, **tags: Any) -> NewsItem:
    return NewsItem(
        title=title,
        link=link,
        published=published,
        summary="",
        sources=(source,),
        favourites=frozenset(tags.get("favourites", ())),
        topics=frozenset(tags.get("topics", ())),
        ocon_future=tags.get("ocon_future", False),
    )


MOVE = item(
    "Ocon signs a new contract",
    "https://example.com/ocon-contract",
    utc(2025, 12, 10, 9),
    "BBC Sport",
    favourites={"Ocon"},
    topics={"contracts"},
    ocon_future=True,
)
TEST = item(
    "McLaren completes its shakedown",
    "https://example.com/shakedown",
    utc(2026, 1, 2, 9),
    "Motorsport.com",
    favourites={"McLaren"},
    topics={"testing"},
)


def test_collect_merges_archived_and_fresh_news(calendar: list[Race]) -> None:
    same_story = replace(MOVE, sources=("Autosport",))
    old = replace(
        TEST, title="An older test", link="https://example.com/old", published=utc(2025, 11, 20)
    )
    unrelated = item("A quiet week", "https://example.com/quiet", utc(2026, 1, 1), "BBC Sport")

    document = offseason.collect(
        NOW, SINCE, calendar[0], [MOVE, TEST, same_story, old, unrelated], ["RaceFans"]
    )

    assert document["kind"] == "monthly_summary"
    assert document["period"] == {"from": "2025-12-01", "to": "2026-01-03"}
    assert document["missing"] == []
    section = document["sections"]["news"]
    assert [i["link"] for i in section["items"]] == [TEST.link, MOVE.link]
    assert section["items"][1]["sources"] == ["BBC Sport", "Autosport"]
    assert section["items"][1]["ocon_future"] is True
    assert section["failed_feeds"] == ["RaceFans"]
    assert {s["name"] for s in section["sources"]} == {"BBC Sport", "Autosport", "Motorsport.com"}
    assert all(s["url"].startswith("https://") for s in section["sources"])
    # No default= hook: anything that is not a JSON type would fail here.
    assert json.loads(json.dumps(document)) == document


def test_collect_names_the_start_of_the_next_season(calendar: list[Race]) -> None:
    document = offseason.collect(NOW, SINCE, calendar[0], [MOVE])
    start = document["sections"]["season_start"]
    assert (start["season"], start["round"], start["name"]) == (2026, 1, "Australian Grand Prix")
    fp1 = start["sessions"][0]
    assert fp1["session"] == "FP1" and fp1["start_warsaw"].startswith("2026-03-06T")
    assert start["sources"] == [
        {"name": "Jolpica-F1", "url": "https://api.jolpi.ca/ergast/f1/2026.json?limit=100"}
    ]


def test_collect_says_what_is_missing() -> None:
    every_feed = [f.name for f in news.FEEDS]
    document = offseason.collect(NOW, SINCE, None, [], every_feed)
    assert document["sections"] == {"season_start": None, "news": None}
    assert [m["section"] for m in document["missing"]] == ["news", "season_start"]

    # No news at all is not missing news, as long as some feed was read.
    quiet = offseason.collect(NOW, SINCE, None, [], every_feed[1:])
    assert quiet["sections"]["news"]["items"] == []

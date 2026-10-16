from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from f1brief import schedule
from f1brief.schedule import Race, Session, SessionKind
from f1brief.send_log import SendLog
from f1brief.send_window import Window, decide, describe, window

CHICAGO = ZoneInfo("America/Chicago")
TOKYO = ZoneInfo("Asia/Tokyo")


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.fixture
def races(schedule_2026: dict[str, Any]) -> list[Race]:
    return schedule.parse_schedule(schedule_2026)


@pytest.fixture
def log(tmp_path: Path):
    with SendLog(tmp_path / "state.db") as opened:
        yield opened


def by_round(races: list[Race], round_: int) -> Race:
    return next(r for r in races if r.round == round_)


def with_fp1(race: Race, day: date, start_utc: datetime | None) -> Race:
    """`race` with its FP1 moved (synthetic, for edge cases the calendar does not have)."""
    fp1 = Session(SessionKind.FP1, day, start_utc)
    return replace(race, sessions=(fp1, *race.sessions[1:]))


# --- the window ----------------------------------------------------------------------------


def test_austin_window_opens_two_days_before_fp1_in_summer_time(races: list[Race]) -> None:
    # FP1 Fri 23 Oct 17:30 UTC (19:30 CEST): opens Wed 21 Oct 00:00 CEST.
    assert window(by_round(races, 18)) == Window(
        opens=utc(2026, 10, 20, 22, 0), closes=utc(2026, 10, 23, 17, 30)
    )


def test_mexico_window_after_the_autumn_clock_change(races: list[Race]) -> None:
    # Warsaw is back on CET (UTC+1) from 25 Oct: opens Wed 28 Oct 00:00 CET.
    assert window(by_round(races, 19)).opens == utc(2026, 10, 27, 23, 0)


def test_window_uses_the_warsaw_date_of_fp1_not_the_track_date(races: list[Race]) -> None:
    # Las Vegas FP1 is Thu 19 Nov 16:30 local, but Fri 20 Nov 01:30 in Warsaw.
    vegas = by_round(races, 21)
    assert vegas.first_session.start_utc == utc(2026, 11, 20, 0, 30)
    assert window(vegas).opens == utc(2026, 11, 17, 23, 0)  # Wed 18 Nov 00:00 CET


def test_fp1_late_in_the_utc_evening_is_the_next_warsaw_day(races: list[Race]) -> None:
    # 23:30 UTC on Thu 2 Jul is 01:30 CEST on Fri 3 Jul: opens Wed 1 Jul, not Tue 30 Jun.
    race = with_fp1(by_round(races, 9), date(2026, 7, 2), utc(2026, 7, 2, 23, 30))
    assert window(race).opens == utc(2026, 6, 30, 22, 0)


def test_window_across_the_spring_clock_change(races: list[Race]) -> None:
    # FP1 Mon 30 Mar 10:00 CEST; the window opens Sat 28 Mar 00:00, still CET (UTC+1).
    race = with_fp1(by_round(races, 3), date(2026, 3, 30), utc(2026, 3, 30, 8, 0))
    assert window(race).opens == utc(2026, 3, 27, 23, 0)


def test_window_across_the_autumn_clock_change(races: list[Race]) -> None:
    # FP1 Tue 27 Oct 11:00 CET; three days before is Sat 24 Oct 00:00, still CEST (UTC+2).
    race = with_fp1(by_round(races, 19), date(2026, 10, 27), utc(2026, 10, 27, 10, 0))
    assert window(race, days_before=3).opens == utc(2026, 10, 23, 22, 0)


def test_window_while_the_fp1_time_is_unconfirmed(races: list[Race]) -> None:
    race = with_fp1(by_round(races, 18), date(2026, 10, 23), None)
    # Closes at the start of FP1's day in Warsaw.
    assert window(race) == Window(opens=utc(2026, 10, 20, 22, 0), closes=utc(2026, 10, 22, 22, 0))


def test_days_before(races: list[Race]) -> None:
    austin = by_round(races, 18)
    assert window(austin, days_before=0).opens == utc(2026, 10, 22, 22, 0)
    assert window(austin, days_before=5).opens == utc(2026, 10, 17, 22, 0)
    with pytest.raises(ValueError, match="negative"):
        window(austin, days_before=-1)


# --- the decision --------------------------------------------------------------------------


def test_too_early(races: list[Race], log: SendLog) -> None:
    decision = decide(races, utc(2026, 10, 16, 6, 0), log)
    assert not decision.due
    assert decision.race is not None and decision.race.round == 18
    assert decision.reason == "too early: the window opens Wed 21 Oct 00:00"


@pytest.mark.parametrize(
    ("now", "due"),
    [
        (utc(2026, 10, 20, 21, 59, 59), False),
        (utc(2026, 10, 20, 22, 0), True),  # Wed 21 Oct 00:00 in Warsaw
        (utc(2026, 10, 23, 17, 29, 59), True),
    ],
)
def test_the_window_edges(races: list[Race], log: SendLog, now: datetime, due: bool) -> None:
    decision = decide(races, now, log)
    assert decision.due is due
    assert decision.race is not None and decision.race.round == 18


def test_the_time_zone_of_now_does_not_matter(races: list[Race], log: SendLog) -> None:
    # The same instants as above, given in Chicago and Tokyo time.
    before = datetime(2026, 10, 20, 16, 59, 59, tzinfo=CHICAGO)
    after = datetime(2026, 10, 21, 7, 0, tzinfo=TOKYO)
    assert decide(races, before, log).due is False
    assert decide(races, after, log).due is True


def test_due_once_inside_the_window(races: list[Race], log: SendLog) -> None:
    decision = decide(races, utc(2026, 10, 21, 8, 0), log)
    assert decision.due
    assert decision.reason == "due: FP1 starts Fri 23 Oct 19:30"


def test_once_fp1_has_started_the_next_round_is_considered(races: list[Race], log: SendLog) -> None:
    decision = decide(races, utc(2026, 10, 23, 17, 30), log)
    assert decision.race is not None and decision.race.round == 19
    assert decision.reason == "too early: the window opens Wed 28 Oct 00:00"


def test_already_sent(races: list[Race], log: SendLog) -> None:
    log.claim(2026, 18, utc(2026, 10, 21, 8, 0))
    log.mark_sent(2026, 18, utc(2026, 10, 21, 8, 1), 2)

    decision = decide(races, utc(2026, 10, 22, 8, 0), log)
    assert not decision.due
    assert decision.reason == "already sent at Wed 21 Oct 10:00 (2 message(s))"


def test_a_partial_or_unfinished_send_is_not_repeated(races: list[Race], log: SendLog) -> None:
    now = utc(2026, 10, 22, 8, 0)
    log.claim(2026, 18, utc(2026, 10, 21, 8, 0))
    decision = decide(races, now, log)
    assert not decision.due
    assert decision.reason.startswith("a send started at Wed 21 Oct 10:00 and did not finish")

    log.mark_partial(2026, 18, utc(2026, 10, 21, 8, 1), 1)
    decision = decide(races, now, log)
    assert not decision.due
    assert decision.reason.startswith("only partly sent at Wed 21 Oct 10:00 (1 message(s)")


def test_a_record_for_another_round_does_not_count(races: list[Race], log: SendLog) -> None:
    log.claim(2026, 17, utc(2026, 10, 7, 8, 0))
    log.mark_sent(2026, 17, utc(2026, 10, 7, 8, 1), 2)
    assert decide(races, utc(2026, 10, 21, 8, 0), log).due


def test_after_the_last_round(races: list[Race], log: SendLog) -> None:
    decision = decide(races, utc(2026, 12, 10, 8, 0), log)
    assert decision == decision.__class__(
        due=False, reason="no upcoming race weekend in the calendar"
    )


def test_now_must_be_aware(races: list[Race], log: SendLog) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        decide(races, datetime(2026, 10, 21, 8, 0), log)


def test_describe(races: list[Race], log: SendLog) -> None:
    assert describe(decide(races, utc(2026, 10, 16, 6, 0), log)) == (
        "Round 18: United States Grand Prix (2026)\n"
        "Send window: Wed 21 Oct 00:00 - Fri 23 Oct 19:30 (Europe/Warsaw)\n"
        "Too early: the window opens Wed 21 Oct 00:00."
    )
    assert describe(decide(races, utc(2026, 12, 10, 8, 0), log)) == (
        "No upcoming race weekend in the calendar."
    )

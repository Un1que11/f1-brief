"""Whether today's run should send the digest: once per round, from a few days before FP1.

The window for a round opens at local midnight `days_before` days before the local date of
FP1 and closes when FP1 starts (at midnight of that date while the start time is to be
confirmed). Local is the account's time zone (F32), Warsaw by default. The daily job sends
on its first run inside the window, unless the send log already has a row for the round.
Every function takes the current time as an argument.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from f1brief.schedule import WARSAW, Race
from f1brief.send_log import SendLog, SendRecord, Status

DEFAULT_DAYS_BEFORE = 2


@dataclass(frozen=True)
class Window:
    opens: datetime  # UTC
    closes: datetime  # UTC

    def contains(self, moment: datetime) -> bool:
        return self.opens <= moment < self.closes


@dataclass(frozen=True)
class Decision:
    due: bool
    reason: str
    race: Race | None = None
    window: Window | None = None


def _local_midnight(day: date, tz: tzinfo) -> datetime:
    # zoneinfo picks the offset in force on that day, so DST changes need no special case.
    return datetime.combine(day, time(), tzinfo=tz).astimezone(UTC)


def window(race: Race, days_before: int = DEFAULT_DAYS_BEFORE, tz: tzinfo = WARSAW) -> Window:
    if days_before < 0:
        raise ValueError("days_before must not be negative")
    fp1 = race.first_session
    fp1_start = fp1.start_utc
    fp1_day = fp1_start.astimezone(tz).date() if fp1_start else fp1.date
    closes = fp1_start or _local_midnight(fp1_day, tz)
    return Window(opens=_local_midnight(fp1_day - timedelta(days=days_before), tz), closes=closes)


def _when(moment: datetime, tz: tzinfo = WARSAW) -> str:
    return moment.astimezone(tz).strftime("%a %d %b %H:%M")


def _already(record: SendRecord, tz: tzinfo = WARSAW) -> str:
    started = _when(record.started_at, tz)
    if record.status is Status.SENT:
        return f"already sent at {started} ({record.messages} message(s))"
    if record.status is Status.PARTIAL:
        return (
            f"only partly sent at {started} ({record.messages} message(s) "
            "delivered); not resent automatically, check the chat"
        )
    return (
        f"a send started at {started} and did not finish; not resent automatically, check the chat"
    )


def decide(
    races: list[Race],
    now: datetime,
    log: SendLog,
    days_before: int = DEFAULT_DAYS_BEFORE,
    tz: tzinfo = WARSAW,
) -> Decision:
    """The decision for the first round whose window (in `tz`) has not closed yet, for the
    log's account. `now` must be timezone-aware."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    upcoming = [
        (race, window(race, days_before, tz))
        for race in sorted(races, key=lambda r: (r.season, r.round))
    ]
    pending = [(race, w) for race, w in upcoming if now < w.closes]
    if not pending:
        return Decision(due=False, reason="no upcoming race weekend in the calendar")
    race, w = pending[0]
    record = log.get(race.season, race.round)
    if record is not None:
        return Decision(due=False, reason=_already(record, tz), race=race, window=w)
    if now < w.opens:
        reason = f"too early: the window opens {_when(w.opens, tz)}"
        return Decision(due=False, reason=reason, race=race, window=w)
    return Decision(due=True, reason=f"due: FP1 starts {_when(w.closes, tz)}", race=race, window=w)


def describe(decision: Decision, tz: tzinfo = WARSAW) -> str:
    """A few lines for the console."""
    lines = []
    if decision.race is not None and decision.window is not None:
        race, w = decision.race, decision.window
        lines.append(f"Round {race.round}: {race.name} ({race.season})")
        lines.append(f"Send window: {_when(w.opens, tz)} - {_when(w.closes, tz)} ({tz})")
    lines.append(decision.reason[0].upper() + decision.reason[1:] + ".")
    return "\n".join(lines)

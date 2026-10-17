"""The off-season (F18): instead of digests, a short summary once a month.

The off-season is any day, Warsaw time, before the Warsaw date of the first FP1 of its year's
calendar or after the date of that calendar's last race (or any day of a year without a
calendar yet). A month gets its summary on the first run in its first `SUMMARY_DAYS` days
that falls in the off-season, unless a race weekend starts within `LEAD_DAYS` days (its digest
comes first) or the season ended less than `LEAD_DAYS` days ago. The summary covers the news
since the start of the previous month, read back from the archive that every off-season run
fills (`news_archive`), plus the start of the next season when its calendar is out. Every
function takes the current time as an argument.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

from f1brief import facts, jolpica, news
from f1brief.config import Favourites
from f1brief.schedule import WARSAW, Race, Session
from f1brief.send_log import SendLog, Status, SummaryRecord

# 2 (F20): favourites from the config (several teams), news `future`.
SCHEMA_VERSION = 2
SUMMARY_DAYS = 7
LEAD_DAYS = 14


def _day(session: Session, tz: tzinfo) -> date:
    return session.start_utc.astimezone(tz).date() if session.start_utc else session.date


def is_off_season(calendar: list[Race], today: date, tz: tzinfo = WARSAW) -> bool:
    """`calendar` is the season of `today`'s year (empty when it is not published yet)."""
    if not calendar:
        return True
    ordered = sorted(calendar, key=lambda r: r.round)
    first, last = ordered[0], ordered[-1]
    return today < _day(first.first_session, tz) or today > _day(last.race, tz)


def next_weekend(calendars: Iterable[Race], today: date, tz: tzinfo = WARSAW) -> Race | None:
    """The first race weekend whose FP1 is on `today` or later."""
    ahead = [r for r in calendars if _day(r.first_session, tz) >= today]
    return min(ahead, key=lambda r: (r.season, r.round), default=None)


def month_key(day: date) -> str:
    return f"{day:%Y-%m}"


def period_start(today: date, tz: tzinfo = WARSAW) -> datetime:
    """Midnight on the first day of the month before `today`'s, in `tz`."""
    previous = today.replace(day=1) - timedelta(days=1)
    return datetime.combine(previous.replace(day=1), time(), tzinfo=tz)


@dataclass(frozen=True)
class Decision:
    off_season: bool
    due: bool
    reason: str
    month: str
    since: datetime | None = None
    next_race: Race | None = None


def _already(record: SummaryRecord) -> str:
    when = record.started_at.astimezone(WARSAW).strftime("%a %d %b %H:%M")
    if record.status is Status.SENT:
        return f"the {record.month} summary was already sent at {when}"
    return (
        f"the {record.month} summary was (maybe) partly sent at {when}; not resent "
        "automatically, check the chat"
    )


def decide(
    calendar: list[Race],
    now: datetime,
    log: SendLog,
    next_season: Callable[[], list[Race]],
    tz: tzinfo = WARSAW,
) -> Decision:
    """Whether to send this month's summary now. `calendar` is the season of the current
    year in `tz`; `next_season` fetches the following one, only when it is needed (after the
    last race of the year). `now` must be timezone-aware."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    today = now.astimezone(tz).date()
    month = month_key(today)
    if not is_off_season(calendar, today, tz):
        return Decision(False, False, "in season: no monthly summary", month)
    upcoming = list(calendar)
    finale = _day(max(calendar, key=lambda r: r.round).race, tz) if calendar else None
    if finale is not None and today > finale:
        upcoming = next_season()
    race = next_weekend(upcoming, today, tz)
    since = period_start(today, tz)

    def no(reason: str) -> Decision:
        return Decision(True, False, reason, month, since, race)

    if race is not None and (_day(race.first_session, tz) - today).days < LEAD_DAYS:
        return no(f"{race.name} starts within {LEAD_DAYS} days; its digest comes instead")
    if finale is not None and today > finale and (today - finale).days < LEAD_DAYS:
        return no(f"the season ended less than {LEAD_DAYS} days ago")
    if today.day > SUMMARY_DAYS:
        return no(f"the monthly summary goes out in the first {SUMMARY_DAYS} days of a month")
    record = log.get_summary(month)
    if record is not None:
        return no(_already(record))
    return Decision(True, True, f"due: the {month} summary", month, since, race)


def describe(decision: Decision) -> str:
    lines = []
    if decision.off_season:
        lines.append(f"Off-season: monthly summary for {decision.month}")
    lines.append(decision.reason[0].upper() + decision.reason[1:] + ".")
    return "\n".join(lines)


# --- the facts document -----------------------------------------------------------------


def season_start_facts(race: Race) -> dict[str, Any]:
    url = f"{jolpica.BASE_URL}/{race.season}.json?limit={jolpica.PAGE_LIMIT}"
    return {
        **facts.race_facts(race),
        "sources": [{"name": jolpica.SOURCE_NAME, "url": url}],
    }


def news_section(report: news.NewsReport) -> dict[str, Any]:
    """`facts.news_facts`, with the feeds the items came from as sources."""
    used = {source for item in report.items for source in item.sources}
    feeds = [{"name": f.name, "url": f.url} for f in news.FEEDS if f.name in used]
    return {**facts.news_facts(report), "sources": feeds}


def collect(
    now: datetime,
    since: datetime,
    next_race: Race | None,
    items: Iterable[news.NewsItem],
    favourites: Favourites,
    failed_feeds: Iterable[str] = (),
) -> dict[str, Any]:
    """The facts document for the monthly summary: the news items published since `since`
    (archived and freshly fetched ones, merged here) and the start of the next season.
    `failed_feeds` are the feeds that could not be read today. `now` must be aware."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    failed = tuple(failed_feeds)
    selected = tuple(news.select(items, since))
    missing: list[dict[str, str]] = []
    news_data = None
    if selected or len(failed) < len(news.FEEDS):
        news_data = news_section(news.NewsReport(since, selected, failed))
    else:
        missing.append({"section": "news", "reason": "no feed could be read and none archived"})
    if next_race is None:
        missing.append(
            {"section": "season_start", "reason": "the next season's calendar is not available"}
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "monthly_summary",
        "generated_at": now.astimezone(WARSAW).isoformat(),
        "timezone": str(WARSAW),
        "period": {
            "from": since.astimezone(WARSAW).date().isoformat(),
            "to": now.astimezone(WARSAW).date().isoformat(),
        },
        "favourites": facts.favourites_facts(favourites),
        "sections": {
            "season_start": season_start_facts(next_race) if next_race else None,
            "news": news_data,
        },
        "missing": missing,
    }

"""The texts of the Telegram bot (F23, F40), its story alerts (F24) and the offer to unfollow
a quiet story (F26), from the message catalog (F30, `f1brief.messages`).

`texts_for(language)` gives a `Texts`: the catalog of that language (plain texts as attributes,
statuses, session names, date and number formats, plural forms) plus the few texts that are
put together from several entries. Times are the account's local time (Warsaw by default,
F32), 24-hour. Until F34 the language is
the global `[digest] language` from `config.toml`. The texts are plain: the bot escapes them for
Telegram HTML (F41); only `next_race` lays its text out as `telegram_html` blocks, and the
icons below are the same in every language.
"""

from collections.abc import Sequence
from datetime import tzinfo
from functools import cache

from f1brief import jolpica
from f1brief import telegram_html as th
from f1brief.messages import DEFAULT_LANGUAGE, Catalog
from f1brief.schedule import WARSAW, Race
from f1brief.stories import StoryStatus

# The status chips of a story (alerts, the timeline) and the cards' icons (F41).
STATUS_ICONS = {
    StoryStatus.RUMOR: "🟡",
    StoryStatus.DEVELOPING: "🟠",
    StoryStatus.CONFIRMED: "🟢",
    StoryStatus.DENIED: "🔴",
    StoryStatus.RESOLVED: "⚪",
}
ICON_STORY = "📰"
ICON_ALERT = "🔔"
ICON_RACE = "🏁"
NO_TIME = "--:--"


def _is_warsaw(tz: tzinfo) -> bool:
    return str(tz) == str(WARSAW)


class Texts(Catalog):
    """One language's catalog with the bot's composed texts."""

    def start(self, days_before: int, next_digest: str) -> str:
        """The greeting for /start: what the bot does and when the next digest comes."""
        if days_before == 0:
            when = self.start_same_day
        else:
            when = self.start_days_before.format(days=self.days(days_before))
        return self.greeting.format(when=when, next_digest=next_digest)

    def button_mute(self, days: int) -> str:
        return self.button_mute_days.format(days=self.days(days))

    def by_zone(self, tz: tzinfo) -> str:
        """ "по Варшаве", or "по времени America/New_York" for another zone."""
        return self.zone_by_warsaw if _is_warsaw(tz) else self.zone_by_other.format(tz=tz)

    def zone_note(self, tz: tzinfo) -> str:
        """ "время варшавское", or "время America/New_York"."""
        return self.zone_note_warsaw if _is_warsaw(tz) else self.zone_note_other.format(tz=tz)

    def status_chip(self, status: StoryStatus) -> str:
        """ "🟡 слух"."""
        return f"{STATUS_ICONS[status]} {self.status(status)}"

    def next_race(self, race: Race, digest: str, tz: tzinfo = WARSAW) -> list[th.Block]:
        """Round and circuit, the session times in local time as an aligned table, then the
        line about the digest and the source."""
        title = self.race_title.format(round=race.round, name=race.name, season=race.season)
        top = th.Block(
            (
                th.Line((th.Span(f"{ICON_RACE} "), th.Span(title, bold=True))),
                th.plain(f"{race.circuit.name}, {race.circuit.locality}, {race.circuit.country}"),
            )
        )
        rows: list[str] = []
        for session in race.sessions:
            if session.start_utc is not None:
                when = self.day_time(session.start_utc, tz)
            else:
                when = f"{self.day(session.date)} {NO_TIME}"
            rows.append(f"{when}  {self.session(session.kind)}")
        lines = [
            th.plain(self.race_schedule.format(zone=self.zone_note(tz))),
            th.Line((th.Span("\n".join(rows), pre=True),)),
        ]
        if any(session.start_utc is None for session in race.sessions):
            lines.append(th.plain(self.race_time_tbc.format(no_time=NO_TIME)))
        bottom = th.Block(
            (th.plain(digest), th.plain(self.race_source.format(source=jolpica.SOURCE_NAME)))
        )
        return [top, th.Block(tuple(lines)), bottom]

    def favourites(self, teams: Sequence[str], drivers: Sequence[str]) -> str:
        return "\n".join(
            [
                self.favourites_title,
                "",
                self.favourite_teams.format(names=", ".join(teams) or self.none),
                self.favourite_drivers.format(names=", ".join(drivers) or self.none),
                "",
                self.favourites_hint,
            ]
        )


@cache
def texts_for(language: str = DEFAULT_LANGUAGE) -> Texts:
    """The bot's texts in `language`, loaded once."""
    return Texts(language)

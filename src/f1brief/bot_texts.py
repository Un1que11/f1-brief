"""The texts of the Telegram bot (F23, F40), its story alerts (F24) and the offer to unfollow
a quiet story (F26), from the message catalog (F30, `f1brief.messages`).

`texts_for(language)` gives a `Texts`: the catalog of that language (plain texts as attributes,
statuses, session names, date and number formats, plural forms) plus the few texts that are
put together from several entries. Times are Warsaw time, 24-hour. Until F34 the language is
the global `[digest] language` from `config.toml`.
"""

from collections.abc import Sequence
from functools import cache

from f1brief import jolpica
from f1brief.messages import DEFAULT_LANGUAGE, Catalog
from f1brief.schedule import Race


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

    def next_race(self, race: Race, digest: str) -> str:
        """Round, circuit and session times in Warsaw, then the line about the digest."""
        lines = [
            self.race_title.format(round=race.round, name=race.name, season=race.season),
            f"{race.circuit.name}, {race.circuit.locality}, {race.circuit.country}",
            "",
            self.race_schedule,
        ]
        for session in race.sessions:
            if session.start_utc is not None:
                when = self.day_time(session.start_utc)
            else:
                when = self.race_session_unknown_time.format(day=self.day(session.date))
            lines.append(self.race_session.format(when=when, session=self.session(session.kind)))
        lines += ["", digest, self.race_source.format(source=jolpica.SOURCE_NAME)]
        return "\n".join(lines)

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

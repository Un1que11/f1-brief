"""The message catalog (F30): every text the bot, the story alerts, the offer to unfollow a
quiet story and the archive page show, in each supported language (`ru`, `en`, `pl`).

The texts live in `<language>.toml` next to this module, one file per language with the same
keys and, in each template, the same `str.format` fields (a test checks both). A `Catalog`
gives one language's texts: the plain ones as attributes (`texts.menu_back`), the tables
through methods (`status`, `session`, `entity_kind`, `commands`), and the formats that depend
on the language: dates and times (always 24-hour, in Warsaw time until F32 brings per-user
time zones), numbers (decimal comma for ru / pl) and plural forms (CLDR categories).

Until F34 every text uses the global `[digest] language` from `config.toml`.
"""

import tomllib
from collections.abc import Callable
from datetime import date as Date
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from importlib import resources
from typing import Any

from f1brief.config import LANGUAGES
from f1brief.schedule import WARSAW, SessionKind
from f1brief.stories import EntityKind, StoryStatus

DEFAULT_LANGUAGE = "ru"


def _slavic(one_is_only_1: bool) -> Callable[[int], str]:
    """The CLDR integer plural rule of ru (one: 1, 21, 31, ...) or pl (one: only 1); both
    have few for 2-4, 22-24, ... (not 12-14) and many for the rest."""

    def rule(n: int) -> str:
        n = abs(n)
        if (n == 1) if one_is_only_1 else (n % 10 == 1 and n % 100 != 11):
            return "one"
        if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            return "few"
        return "many"

    return rule


# The plural category of an integer in each language (CLDR, integers only).
PLURAL_RULES: dict[str, Callable[[int], str]] = {
    "ru": _slavic(one_is_only_1=False),
    "en": lambda n: "one" if n == 1 else "other",
    "pl": _slavic(one_is_only_1=True),
}
PLURAL_CATEGORIES = {
    "ru": ("one", "few", "many"),
    "en": ("one", "other"),
    "pl": ("one", "few", "many"),
}


class CatalogError(ValueError):
    """An unsupported language or a key the catalog does not have."""


def load(language: str) -> dict[str, Any]:
    """The raw catalog of `language` as parsed from its TOML file."""
    if language not in LANGUAGES:
        raise CatalogError(f"no message catalog for language {language!r}")
    path = resources.files(__name__).joinpath(f"{language}.toml")
    return tomllib.loads(path.read_text(encoding="utf-8"))


class Catalog:
    """The texts and formats of one language."""

    def __init__(self, language: str) -> None:
        raw = load(language)
        self.language = language
        self._text: dict[str, str] = raw["text"]
        self._format: dict[str, Any] = raw["format"]
        self._plural: dict[str, dict[str, str]] = raw["plural"]
        self._status: dict[str, str] = raw["status"]
        self._sessions: dict[str, str] = raw["sessions"]
        self._kinds: dict[str, str] = raw["entity_kinds"]
        self._commands: dict[str, str] = raw["commands"]

    def __repr__(self) -> str:
        return f"Catalog({self.language!r})"

    def __getattr__(self, key: str) -> str:
        # Only called for names that are not regular attributes: the plain texts.
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            return self._text[key]
        except KeyError:
            raise AttributeError(f"no text {key!r} in the {self.language} catalog") from None

    # --- tables ---------------------------------------------------------------------------

    def status(self, status: StoryStatus) -> str:
        return self._status[status.value]

    def session(self, kind: SessionKind) -> str:
        return self._sessions[kind.name.lower()]

    def entity_kind(self, kind: EntityKind) -> str:
        return self._kinds[kind.value]

    def commands(self) -> tuple[tuple[str, str], ...]:
        """(command, description) for `setMyCommands`, in the catalog's order."""
        return tuple(self._commands.items())

    # --- formats --------------------------------------------------------------------------

    def plural(self, key: str, n: int) -> str:
        """`n` with the word in the right form: "3 дня", "5 dni", "1 day"."""
        forms = self._plural[key]
        return forms[PLURAL_RULES[self.language](n)].format(n=n)

    def days(self, n: int) -> str:
        return self.plural("days", n)

    def weekday(self, day: Date) -> str:
        return self._format["weekdays"][day.weekday()]

    def _fields(self, moment: datetime | Date) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "day": moment.day,
            "month": moment.month,
            "year": moment.year,
            "weekday": self._format["weekdays"][moment.weekday()],
            "month_name": self._format["months"][moment.month - 1],
        }
        if isinstance(moment, datetime):
            fields |= {"hour": moment.hour, "minute": moment.minute}
        return fields

    def date(self, moment: datetime) -> str:
        """The Warsaw date: "19.10.2026" (ru, pl), "19 Oct 2026" (en)."""
        return self._format["date"].format(**self._fields(moment.astimezone(WARSAW)))

    def date_time(self, moment: datetime) -> str:
        """The Warsaw date and 24-hour time: "19.10.2026 14:05"."""
        return self._format["date_time"].format(**self._fields(moment.astimezone(WARSAW)))

    def day_time(self, moment: datetime) -> str:
        """The weekday, day and 24-hour time in Warsaw: "Пт 24.10 19:30", "Fri 24 Oct 19:30"."""
        return self._format["day_time"].format(**self._fields(moment.astimezone(WARSAW)))

    def day(self, day: Date) -> str:
        """A calendar day with its weekday: "Пт 24.10", "Fri 24 Oct"."""
        return self._format["day"].format(**self._fields(day))

    def number(self, value: float | int | Decimal, digits: int = 0) -> str:
        """`value` rounded half up to `digits` decimals with the language's separators:
        "1 234,5" (ru, pl; the space is a no-break one), "1,234.5" (en)."""
        quantum = Decimal(1).scaleb(-digits)
        rounded = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
        text = f"{rounded:,.{digits}f}"
        return (
            text.replace(",", "\0")
            .replace(".", self._format["decimal_separator"])
            .replace("\0", self._format["group_separator"])
        )


@cache
def catalog(language: str = DEFAULT_LANGUAGE) -> Catalog:
    """The catalog of `language`, loaded once."""
    return Catalog(language)

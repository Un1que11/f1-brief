"""Settings from `config.toml`: favourites, digest language, send lead time, time zone and
the story alerts' daily cap and quiet hours.

The file is optional. Without it every setting has the default below, which matches the
checked-in `config.toml`; a file that exists but is malformed is an error, never ignored.
Since F32 these are the defaults of every account (`users`): an account's own changes, made
from the bot, live in the state file. `add_favourites` and `remove_favourite` edit the file
itself, keeping its comments; the bot no longer calls them.
"""

import json
import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_PATH = Path("config.toml")

LANGUAGES = ("ru", "en", "pl")  # languages that have prompts (src/f1brief/prompts/<language>)


class ConfigError(Exception):
    """The config file is missing (when asked for explicitly), unreadable or invalid."""


@dataclass(frozen=True)
class Favourite:
    id: str  # Jolpica constructorId or driverId
    name: str
    aliases: tuple[str, ...] = ()  # words that mark a news item as about them
    track_future: bool = False  # also follow news about their future in F1
    names: tuple[tuple[str, str], ...] = ()  # (language, name form) pairs

    def __post_init__(self) -> None:
        if not self.aliases:
            object.__setattr__(self, "aliases", (self.name,))

    def name_in(self, language: str) -> str | None:
        """The name form configured for `language`, or None (the digest then uses the names
        its facts give)."""
        return dict(self.names).get(language)


@dataclass(frozen=True)
class Favourites:
    teams: tuple[Favourite, ...]
    drivers: tuple[Favourite, ...]

    @property
    def team_ids(self) -> tuple[str, ...]:
        return tuple(t.id for t in self.teams)

    @property
    def driver_ids(self) -> tuple[str, ...]:
        return tuple(d.id for d in self.drivers)

    def only(
        self, team_ids: Sequence[str] | None = None, driver_ids: Sequence[str] | None = None
    ) -> "Favourites":
        """These favourites with the teams or drivers replaced by the given ids (a command's
        `--team` / `--driver`); None keeps the list. An id not in the config is named by it."""

        def pick(ids: Sequence[str] | None, entries: tuple[Favourite, ...]) -> tuple:
            if ids is None:
                return entries
            known = {f.id: f for f in entries}
            return tuple(known.get(i) or Favourite(i, i) for i in dict.fromkeys(ids))

        return Favourites(pick(team_ids, self.teams), pick(driver_ids, self.drivers))


DEFAULT_FAVOURITES = Favourites(
    teams=(Favourite("mclaren", "McLaren"), Favourite("haas", "Haas")),
    drivers=(
        Favourite("russell", "George Russell", ("Russell",)),
        Favourite("ocon", "Esteban Ocon", ("Ocon",), track_future=True),
        Favourite("bearman", "Oliver Bearman", ("Bearman",)),
    ),
)


@dataclass(frozen=True)
class Config:
    favourites: Favourites = DEFAULT_FAVOURITES
    language: str = "ru"
    days_before_fp1: int = 2
    timezone: str = "Europe/Warsaw"
    # Story alerts (F24): at most this many a day, none from quiet_start to quiet_end (local
    # wall-clock time; equal times mean no quiet hours).
    max_alerts_per_day: int = 3
    quiet_start: time = time(23)
    quiet_end: time = time(8)
    source: Path | None = field(default=None, compare=False)  # the file it was read from

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load(path: str | Path | None = None) -> Config:
    """Read `path`, or `./config.toml` when no path is given (defaults if that is absent)."""
    explicit = path is not None
    file = Path(path) if explicit else DEFAULT_PATH
    try:
        raw = file.read_bytes()
    except FileNotFoundError:
        if explicit:
            raise ConfigError(f"{file}: no such file") from None
        return Config()
    except OSError as exc:
        raise ConfigError(f"{file}: {exc.strerror or exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{file}: not valid TOML: {exc}") from exc
    try:
        return parse(data, source=file)
    except ConfigError as exc:
        raise ConfigError(f"{file}: {exc}") from exc


def parse(data: Mapping[str, Any], source: Path | None = None) -> Config:
    """Validate an already-decoded TOML document. Missing keys take their defaults."""
    _no_unknown(data, ("digest", "favourites", "alerts"), "top level")
    digest = _table(data, "digest")
    _no_unknown(digest, ("language", "days_before_fp1", "timezone"), "[digest]")
    defaults = Config()

    language = _get(digest, "language", str, "digest.language", defaults.language)
    if language not in LANGUAGES:
        raise ConfigError(
            f"digest.language: {language!r} is not supported (supported: {', '.join(LANGUAGES)})"
        )

    days = _get(digest, "days_before_fp1", int, "digest.days_before_fp1", defaults.days_before_fp1)
    if days < 0:
        raise ConfigError("digest.days_before_fp1: must be a whole number, 0 or more")

    timezone = _get(digest, "timezone", str, "digest.timezone", defaults.timezone)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"digest.timezone: unknown time zone {timezone!r}") from None

    alerts = _table(data, "alerts")
    _no_unknown(alerts, ("max_per_day", "quiet_start", "quiet_end"), "[alerts]")
    cap = _get(alerts, "max_per_day", int, "alerts.max_per_day", defaults.max_alerts_per_day)
    if cap < 0:
        raise ConfigError("alerts.max_per_day: must be a whole number, 0 or more")
    quiet_start = _clock(alerts, "quiet_start", defaults.quiet_start)
    quiet_end = _clock(alerts, "quiet_end", defaults.quiet_end)

    favourites = defaults.favourites
    if "favourites" in data:
        table = _table(data, "favourites")
        _no_unknown(table, ("teams", "drivers"), "[favourites]")
        favourites = Favourites(
            teams=_favourites(table, "teams", defaults.favourites.teams),
            drivers=_favourites(table, "drivers", defaults.favourites.drivers),
        )
    return Config(favourites, language, days, timezone, cap, quiet_start, quiet_end, source)


_CLOCK = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


def _clock(table: Mapping[str, Any], key: str, default: time) -> time:
    # "23:00": TOML's own local time (23:00:00) would also do, but a string reads plainer.
    value = _get(table, key, str, f"alerts.{key}", default.strftime("%H:%M"))
    match = _CLOCK.fullmatch(value)
    if match is None:
        raise ConfigError(f"alerts.{key}: {value!r} is not a time of day (HH:MM, 24-hour)")
    return time(int(match.group(1)), int(match.group(2)))


def _favourites(
    table: Mapping[str, Any], key: str, default: tuple[Favourite, ...]
) -> tuple[Favourite, ...]:
    where = f"favourites.{key}"
    if key not in table:
        return default
    entries = table[key]
    if not isinstance(entries, list):
        raise ConfigError(f"{where}: must be a list of tables ([[{where}]])")
    result: list[Favourite] = []
    seen: set[str] = set()
    for n, entry in enumerate(entries, start=1):
        at = f"{where}[{n}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{at}: must be a table with id and name")
        _no_unknown(entry, ("id", "name", "aliases", "track_future", "names"), at)
        fav_id = _required_text(entry, "id", at)
        if fav_id != fav_id.lower() or " " in fav_id:
            raise ConfigError(f"{at}.id: {fav_id!r} is not a Jolpica id (lowercase, no spaces)")
        if fav_id in seen:
            raise ConfigError(f"{at}.id: {fav_id!r} is listed twice")
        seen.add(fav_id)
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list) or not all(
            isinstance(a, str) and a.strip() for a in aliases
        ):
            raise ConfigError(f"{at}.aliases: must be a list of non-empty strings")
        track_future = _get(entry, "track_future", bool, f"{at}.track_future", False)
        result.append(
            Favourite(
                fav_id,
                _required_text(entry, "name", at),
                tuple(a.strip() for a in aliases),
                track_future,
                _names(entry, at),
            )
        )
    return tuple(result)


def _names(entry: Mapping[str, Any], at: str) -> tuple[tuple[str, str], ...]:
    # `names = { ru = "Джордж Расселл", pl = "George Russell" }`: the form for each language.
    names = _get(entry, "names", dict, f"{at}.names", {})
    for language, form in names.items():
        if language not in LANGUAGES:
            raise ConfigError(
                f"{at}.names: {language!r} is not a supported language "
                f"(supported: {', '.join(LANGUAGES)})"
            )
        if not isinstance(form, str) or not form.strip():
            raise ConfigError(f"{at}.names.{language}: must be a non-empty string")
    return tuple((language, form.strip()) for language, form in names.items())


def _toml_entry(key: str, favourite: Favourite) -> str:
    # JSON string escapes are valid in TOML basic strings.
    lines = [
        f"[[favourites.{key}]]",
        f"id = {json.dumps(favourite.id, ensure_ascii=False)}",
        f"name = {json.dumps(favourite.name, ensure_ascii=False)}",
    ]
    if favourite.aliases != (favourite.name,):
        aliases = ", ".join(json.dumps(a, ensure_ascii=False) for a in favourite.aliases)
        lines.append(f"aliases = [{aliases}]")
    if favourite.track_future:
        lines.append("track_future = true")
    if favourite.names:
        forms = ", ".join(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in favourite.names)
        lines.append(f"names = {{ {forms} }}")
    return "\n".join(lines)


def add_favourites(
    path: str | Path,
    teams: Sequence[Favourite] = (),
    drivers: Sequence[Favourite] = (),
    write: bool = True,
) -> Config:
    """Appends the teams and drivers that are not favourites yet to the config file (created
    when absent) and returns the settings it then holds. The rest of the file, comments
    included, is kept as it is. A list the file leaves out means the defaults, so those are
    written out first; otherwise the appended entries would replace them. With `write=False`
    nothing is written: the result is what the file would hold."""
    file = Path(path)
    text = _read(file)
    data, current = _parsed(file, text)
    listed = data.get("favourites", {})
    blocks: list[str] = []
    for key, new, existing in (
        ("teams", teams, current.favourites.teams),
        ("drivers", drivers, current.favourites.drivers),
    ):
        known = {f.id for f in existing}
        fresh = [f for f in {f.id: f for f in new}.values() if f.id not in known]
        if not fresh:
            continue
        kept = () if key in listed else existing
        if key in listed and not listed[key]:
            # A list emptied with `<key> = []` (remove_favourite) cannot also take blocks.
            text = _without_empty_list(text, key)
        blocks += [_toml_entry(key, f) for f in (*kept, *fresh)]
    if not blocks:
        return current
    updated_text = _append(text, blocks)
    _, updated = _parsed(file, updated_text)
    if write:
        _write(file, updated_text)
    return updated


FAVOURITE_KINDS = ("teams", "drivers")
_ENTRY_HEADER = re.compile(r"\s*\[\[\s*favourites\s*\.\s*(teams|drivers)\s*\]\]\s*(#.*)?")
_TABLE_HEADER = re.compile(r"\s*\[")


def remove_favourite(path: str | Path, kind: str, fav_id: str, write: bool = True) -> Config:
    """Removes one team (`kind` "teams") or driver ("drivers") from the favourites in the config
    file and returns the settings it then holds; an id that is not a favourite changes
    nothing. Only that entry's `[[favourites.<kind>]]` block goes, the rest of the file
    (comments included) is kept. A list the file leaves out means the defaults, so the
    remaining defaults are written out; an emptied list is written as `<kind> = []`, since a
    missing one would bring the defaults back. With `write=False` nothing is written."""
    if kind not in FAVOURITE_KINDS:
        raise ValueError(f"kind must be one of {FAVOURITE_KINDS}, not {kind!r}")
    file = Path(path)
    text = _read(file)
    data, current = _parsed(file, text)
    existing: tuple[Favourite, ...] = getattr(current.favourites, kind)
    ids = [f.id for f in existing]
    if fav_id not in ids:
        return current
    remaining = [f for f in existing if f.id != fav_id]
    if kind in data.get("favourites", {}):
        updated_text = _without_entry(text, kind, ids.index(fav_id))
        if not remaining:
            updated_text = _with_empty_list(updated_text, kind)
    elif remaining:
        updated_text = _append(text, [_toml_entry(kind, f) for f in remaining])
    else:
        updated_text = _with_empty_list(text, kind)
    _, updated = _parsed(file, updated_text)
    expected = Favourites(**{**vars(current.favourites), kind: tuple(remaining)})
    if updated.favourites != expected:
        raise ConfigError(f"{file}: cannot remove {fav_id!r} from the favourites; edit it by hand")
    if write:
        _write(file, updated_text)
    return updated


def _without_entry(text: str, kind: str, index: int) -> str:
    """`text` without the `index`-th `[[favourites.<kind>]]` block. The block ends at the next
    table header; comments and blank lines right before that header stay with it."""
    lines = text.split("\n")
    headers = [
        n
        for n, line in enumerate(lines)
        if (match := _ENTRY_HEADER.fullmatch(line)) and match.group(1) == kind
    ]
    if index >= len(headers):
        return text  # not written as a block: the check after parsing reports it
    start = headers[index]
    end = next(
        (n for n in range(start + 1, len(lines)) if _TABLE_HEADER.match(lines[n])), len(lines)
    )
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip()[:1] == "#"):
        end -= 1
    if start > 0 and not lines[start - 1].strip() and end < len(lines) and not lines[end].strip():
        end += 1  # one blank line between the blocks around it, not two
    return "\n".join(lines[:start] + lines[end:]).rstrip("\n") + "\n"


_FAVOURITES_HEADER = re.compile(r"\s*\[\s*favourites\s*\]\s*(#.*)?")
_EMPTY_LIST = re.compile(r"\s*(teams|drivers)\s*=\s*\[\s*\]\s*(#.*)?")


def _with_empty_list(text: str, kind: str) -> str:
    """`text` with `<kind> = []` in its `[favourites]` table, which is added when absent. A
    second `[favourites]` header would make the file invalid TOML."""
    lines = text.split("\n")
    header = next((n for n, line in enumerate(lines) if _FAVOURITES_HEADER.fullmatch(line)), None)
    if header is None:
        return _append(text, [f"[favourites]\n{kind} = []"])
    lines.insert(header + 1, f"{kind} = []")
    return "\n".join(lines)


def _without_empty_list(text: str, kind: str) -> str:
    """`text` without the `<kind> = []` of its `[favourites]` table, so that
    `[[favourites.<kind>]]` blocks can be appended (TOML does not allow both). A comment on
    that line stays as a line of its own. Only the one-line form is found: an empty list
    written over several lines is left alone, and the parse check then refuses the edit."""
    lines = text.split("\n")
    in_favourites = False
    for n, line in enumerate(lines):
        if _TABLE_HEADER.match(line):
            in_favourites = bool(_FAVOURITES_HEADER.fullmatch(line))
        elif in_favourites and (match := _EMPTY_LIST.fullmatch(line)) and match.group(1) == kind:
            comment = [match.group(2)] if match.group(2) else []
            return "\n".join(lines[:n] + comment + lines[n + 1 :])
    return text


def _read(file: Path) -> str:
    try:
        return file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ConfigError(f"{file}: {exc.strerror or exc}") from exc


def _parsed(file: Path, source: str) -> tuple[dict[str, Any], Config]:
    try:
        data = tomllib.loads(source)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{file}: not valid TOML: {exc}") from exc
    try:
        return data, parse(data, source=file)
    except ConfigError as exc:
        raise ConfigError(f"{file}: {exc}") from exc


def _append(text: str, blocks: Sequence[str]) -> str:
    head = text.rstrip("\n") + "\n\n" if text.strip() else ""
    return head + "\n\n".join(blocks) + "\n"


def _write(file: Path, text: str) -> None:
    temporary = file.with_name(f".{file.name}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, file)
    except OSError as exc:
        raise ConfigError(f"{file}: {exc.strerror or exc}") from exc


def _table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}]: must be a table")
    return value


def _no_unknown(table: Mapping[str, Any], known: tuple[str, ...], where: str) -> None:
    # A typo ("days_befor_fp1") would otherwise silently fall back to the default.
    unknown = sorted(set(table) - set(known))
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _get(table: Mapping[str, Any], key: str, kind: type, where: str, default: Any) -> Any:
    value = table.get(key, default)
    # bool is a subclass of int in Python; `days_before_fp1 = true` must not pass as 1.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ConfigError(f"{where}: must be {_KIND_NAMES[kind]}")
    return value


def _required_text(table: Mapping[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key}: required, a non-empty string")
    return value.strip()


_KIND_NAMES = {str: "a string", int: "a whole number", bool: "true or false", dict: "a table"}

# Russian and Polish labels: letters that look like Latin ones are intended here.
# ruff: noqa: RUF001, RUF002
"""Designed Telegram cards (F41) for the generated messages: the digest, the message after
qualifying and the off-season summary.

`claude -p` returns structured JSON (sections of items, each item with the sources it came
from) instead of plain text; `from_model` parses it, keeps only sources that appear in the
facts document the text was written from, and adds what code knows better than the model:
the header's dates and the session schedule in Warsaw time. `render` turns a card into
Telegram HTML messages (`telegram_html`):

    🏁 <b>Гран-при США</b>
    23–25.10.2026

    <pre>Пт 23.10 19:30  Практика 1 …</pre>

    <b>📊 Зачёт</b>
    • Russell <b>P4</b>, … <a href="…">Jolpica-F1</a>

A long section is folded into `<blockquote expandable>`. A card is saved as JSON (`to_dict`,
`from_dict`), so `digest` can write one and `format` / `send` can read it back.
"""

import json
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from f1brief.telegram_html import (
    MAX_LENGTH,
    Block,
    Line,
    Span,
    is_web_link,
    pack,
    plain,
    text_line,
)

DIGEST, QUALI, SUMMARY = "digest", "quali", "summary"
KINDS = (DIGEST, QUALI, SUMMARY)
HEADER_EMOJI = {DIGEST: "🏁", QUALI: "⏱", SUMMARY: "🗓"}

# Section ids the prompt templates ask for, in order, with the emoji of their heading. A
# digest has one `team:<Jolpica constructor id>` section per favourite team.
SECTIONS: dict[str, dict[str, str]] = {
    DIGEST: {
        "race": "🗓",
        "standings": "📊",
        "team": "🏎",
        "drivers": "👤",
        "stories": "📰",
        "track": "🛣",
        "weather": "🌦",
        "expect": "🔮",
    },
    QUALI: {"qualifying": "⏱", "outlook": "🔮"},
    SUMMARY: {"moves": "🔄", "testing": "🧪", "favourites": "⭐", "next_season": "📅"},
}
TEAM_EMOJI = {
    "mclaren": "🧡",
    "haas": "🤍",
    "ferrari": "❤️",
    "mercedes": "🩵",
    "red_bull": "💙",
    "rb": "💙",
    "williams": "💙",
    "aston_martin": "💚",
    "sauber": "💚",
    "audi": "🖤",
    "alpine": "🩷",
    "cadillac": "🤍",
}
OTHER_EMOJI = "📌"

# A section is folded into an expandable blockquote when its items are longer than this
# (rendered HTML characters), so the message stays short until it is opened.
FOLD_CHARS = 600
BULLET = "• "
SOURCE_SEP = " · "

LABELS: dict[str, dict[str, Any]] = {
    "ru": {
        "weekdays": ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"),
        "sessions": {
            "FP1": "Практика 1",
            "FP2": "Практика 2",
            "FP3": "Практика 3",
            "Sprint Qualifying": "Спринт-квалификация",
            "Sprint": "Спринт",
            "Qualifying": "Квалификация",
            "Race": "Гонка",
        },
        "schedule": "Расписание, время варшавское",
        "tbc": "время уточняется",
        "race_at": "Гонка: {when} по Варшаве",
        "round": "Этап {round}",
    },
    "en": {
        "weekdays": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        "sessions": {
            "FP1": "Practice 1",
            "FP2": "Practice 2",
            "FP3": "Practice 3",
            "Sprint Qualifying": "Sprint Qualifying",
            "Sprint": "Sprint",
            "Qualifying": "Qualifying",
            "Race": "Race",
        },
        "schedule": "Schedule, Warsaw time",
        "tbc": "time to be confirmed",
        "race_at": "Race: {when}, Warsaw time",
        "round": "Round {round}",
    },
    "pl": {
        "weekdays": ("Pn", "Wt", "Śr", "Cz", "Pt", "Sb", "Nd"),
        "sessions": {
            "FP1": "Trening 1",
            "FP2": "Trening 2",
            "FP3": "Trening 3",
            "Sprint Qualifying": "Kwalifikacje do sprintu",
            "Sprint": "Sprint",
            "Qualifying": "Kwalifikacje",
            "Race": "Wyścig",
        },
        "schedule": "Harmonogram, czas warszawski",
        "tbc": "godzina do potwierdzenia",
        "race_at": "Wyścig: {when} czasu warszawskiego",
        "round": "Runda {round}",
    },
}
NO_TIME = "--:--"


class CardError(ValueError):
    """The model's answer is not a usable card."""


@dataclass(frozen=True)
class Source:
    name: str
    url: str


@dataclass(frozen=True)
class Item:
    text: str
    sources: tuple[Source, ...] = ()
    # Says that something is unknown (from `missing` or a `null` value): needs no source.
    unknown: bool = False


@dataclass(frozen=True)
class Section:
    id: str
    heading: str
    items: tuple[Item, ...]


@dataclass(frozen=True)
class Card:
    kind: str
    language: str
    title: str
    subtitle: str = ""
    # (when, session) rows of the weekend's schedule, in Warsaw time.
    schedule: tuple[tuple[str, str], ...] = ()
    sections: tuple[Section, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


# --- the facts ---------------------------------------------------------------------------


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v)


def facts_strings(document: Mapping[str, Any]) -> frozenset[str]:
    """Every string in the facts document: a source's name and URL must be among them."""
    return frozenset(_strings(document))


def labels(language: str) -> dict[str, Any]:
    return LABELS.get(language, LABELS["ru"])


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _when(session: Mapping[str, Any], language: str) -> str | None:
    """ "Пт 23.10 19:30" (the time as the document has it: Warsaw), "--:--" when unknown."""
    start = _parse_time(session.get("start_warsaw"))
    day = start.date() if start else _parse_day(session.get("date"))
    if day is None:
        return None
    weekday = labels(language)["weekdays"][day.weekday()]
    return f"{weekday} {day:%d.%m} {f'{start:%H:%M}' if start else NO_TIME}"


def schedule_rows(race: Mapping[str, Any] | None, language: str) -> tuple[tuple[str, str], ...]:
    """The weekend's sessions from a `race` facts section, as (when, session) rows."""
    rows: list[tuple[str, str]] = []
    names = labels(language)["sessions"]
    for session in (race or {}).get("sessions") or []:
        when = _when(session, language)
        if when is not None:
            kind = str(session.get("session"))
            rows.append((when, names.get(kind, kind)))
    return tuple(rows)


def date_range(first: date, last: date) -> str:
    """ "23–25.10.2026", "30.10–01.11.2026", "28.12.2026–03.01.2027"."""
    if first == last:
        return f"{first:%d.%m.%Y}"
    if first.year != last.year:
        return f"{first:%d.%m.%Y}–{last:%d.%m.%Y}"
    if first.month != last.month:
        return f"{first:%d.%m}–{last:%d.%m.%Y}"
    return f"{first:%d}–{last:%d.%m.%Y}"


def _round_and_circuit(race: Mapping[str, Any] | None, language: str) -> str:
    """ "Этап 18 · Circuit of the Americas, Austin": digest section 1 asks for the round and
    the track, and the prompt leaves them to code."""
    race = race or {}
    parts: list[str] = []
    if isinstance(race.get("round"), int):
        parts.append(labels(language)["round"].format(round=race["round"]))
    circuit = race.get("circuit") or {}
    place = ", ".join(p for p in (circuit.get("name"), circuit.get("locality")) if p)
    if place:
        parts.append(place)
    return " · ".join(parts)


def _weekend_dates(race: Mapping[str, Any] | None) -> str:
    days = [_parse_day(s.get("date")) for s in (race or {}).get("sessions") or []]
    found = sorted(d for d in days if d is not None)
    return date_range(found[0], found[-1]) if found else ""


def _race_start(race: Mapping[str, Any] | None, language: str) -> str:
    for session in (race or {}).get("sessions") or []:
        if session.get("session") == "Race":
            when = _when(session, language)
            if when:
                return labels(language)["race_at"].format(when=when)
    return ""


def _period(document: Mapping[str, Any]) -> str:
    period = document.get("period") or {}
    first, last = _parse_day(period.get("from")), _parse_day(period.get("to"))
    return date_range(first, last) if first and last else ""


def header(kind: str, document: Mapping[str, Any], language: str) -> tuple[str, tuple]:
    """The subtitle and the schedule rows code adds to a card of `kind`."""
    race = (document.get("sections") or {}).get("race")
    if kind == DIGEST:
        parts = (_round_and_circuit(race, language), _weekend_dates(race))
        return " · ".join(p for p in parts if p), schedule_rows(race, language)
    if kind == QUALI:
        return _race_start(race, language), ()
    return _period(document), ()


# --- the model's answer ------------------------------------------------------------------

_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def parse_json(raw: str) -> dict[str, Any]:
    """The JSON object in the model's answer (a ```json fence around it is tolerated)."""
    text = _FENCE.sub("", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise CardError("the answer holds no JSON object")
    try:
        value = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise CardError(f"the answer is not valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise CardError("the answer is not a JSON object")
    return value


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def section_kind(section_id: str) -> str:
    """ "team:mclaren" -> "team"."""
    return section_id.split(":", 1)[0]


def _sources(
    raw: Any, known: frozenset[str] | None, where: str, warn: Callable[[str], None]
) -> tuple[Source, ...]:
    sources: list[Source] = []
    for entry in raw if isinstance(raw, list) else []:
        name = _text(entry.get("name")) if isinstance(entry, dict) else ""
        url = entry.get("url") if isinstance(entry, dict) else None
        url = url.strip() if isinstance(url, str) else ""
        if not name or not is_web_link(url):
            warn(f"{where}: dropped a source without a name or a web link: {entry!r}")
            continue
        if known is not None and (name not in known or url not in known):
            warn(f"{where}: dropped a source that is not in the facts: {name} {url}")
            continue
        if Source(name, url) not in sources:
            sources.append(Source(name, url))
    return tuple(sources)


def _items(
    raw: Any, known: frozenset[str] | None, where: str, warn: Callable[[str], None]
) -> tuple[Item, ...]:
    items: list[Item] = []
    for n, entry in enumerate(raw if isinstance(raw, list) else [], 1):
        text = _text(entry.get("text")) if isinstance(entry, dict) else ""
        if not text:
            warn(f"{where}, item {n}: dropped an item without text")
            continue
        assert isinstance(entry, dict)
        sources = _sources(entry.get("sources"), known, f"{where}, item {n}", warn)
        unknown = entry.get("unknown") is True
        if not sources and not unknown:
            warn(f"{where}, item {n}: dropped an item without a source in the facts: {text}")
            continue
        items.append(Item(text, sources, unknown))
    return tuple(items)


def sections_from(
    kind: str,
    raw: Any,
    known: frozenset[str] | None,
    warn: Callable[[str], None] = _warn,
) -> tuple[Section, ...]:
    """The sections of the answer. With `known` (the facts' strings), a source that is not
    among them is dropped, and so is an item left without a source unless it says that
    something is unknown. A section left without items is dropped."""
    sections: list[Section] = []
    for n, entry in enumerate(raw if isinstance(raw, list) else [], 1):
        if not isinstance(entry, dict):
            warn(f"section {n}: not an object")
            continue
        section_id = _text(entry.get("id")) or f"section-{n}"
        if section_kind(section_id) not in SECTIONS[kind]:
            warn(f"section {section_id}: not a {kind} section id; shown with a plain marker")
        heading = _text(entry.get("heading"))
        items = _items(entry.get("items"), known, f"section {section_id}", warn)
        if items:
            sections.append(Section(section_id, heading, items))
        else:
            warn(f"section {section_id}: no items left; dropped")
    return tuple(sections)


def from_model(
    kind: str,
    raw: str,
    document: Mapping[str, Any],
    language: str,
    warn: Callable[[str], None] = _warn,
) -> Card:
    """The card for the model's answer `raw` to a `kind` prompt over `document`. Raises
    CardError when nothing usable is left."""
    if kind not in KINDS:
        raise ValueError(f"unknown card kind {kind!r}")
    answer = parse_json(raw)
    title = _text(answer.get("title"))
    if not title:
        raise CardError("the answer has no title")
    sections = sections_from(kind, answer.get("sections"), facts_strings(document), warn)
    if not sections:
        raise CardError("no section with a sourced item is left in the answer")
    subtitle, schedule = header(kind, document, language)
    return Card(kind, language, title, subtitle, schedule, sections)


def from_dict(value: Mapping[str, Any]) -> Card:
    """A card saved with `to_dict` (by `digest`, `summary` or `quali`)."""
    kind = value.get("kind")
    if kind not in KINDS:
        raise CardError(f"not a saved card (kind {kind!r})")
    title = _text(value.get("title"))
    if not title:
        raise CardError("the card has no title")

    def ignore(message: str) -> None:
        pass

    sections = sections_from(kind, value.get("sections"), None, ignore)
    if not sections:
        raise CardError("the card has no sections")
    schedule = tuple(
        (str(row[0]), str(row[1]))
        for row in value.get("schedule") or []
        if isinstance(row, list | tuple) and len(row) == 2
    )
    language = str(value.get("language") or "ru")
    return Card(kind, language, title, _text(value.get("subtitle")), schedule, sections)


# --- rendering ---------------------------------------------------------------------------


def section_emoji(kind: str, section_id: str) -> str:
    base = section_kind(section_id)
    if base == "team":
        return TEAM_EMOJI.get(section_id.partition(":")[2], SECTIONS[DIGEST]["team"])
    return SECTIONS.get(kind, {}).get(base, OTHER_EMOJI)


def source_spans(sources: tuple[Source, ...]) -> list[Span]:
    """Short linked labels: one per source name (the first link of that name)."""
    spans: list[Span] = []
    seen: set[str] = set()
    for source in sources:
        if source.name in seen:
            continue
        seen.add(source.name)
        spans += [Span(SOURCE_SEP if not spans else ", "), Span(source.name, href=source.url)]
    return spans


def item_line(item: Item) -> Line:
    line = text_line(BULLET + item.text)
    return Line((*line.spans, *source_spans(item.sources)))


def schedule_block(rows: tuple[tuple[str, str], ...], language: str) -> Block:
    width = max(len(when) for when, _ in rows)
    table = "\n".join(f"{when.ljust(width)}  {name}" for when, name in rows)
    lines = [plain(labels(language)["schedule"]), Line((Span(table, pre=True),))]
    if any(NO_TIME in when for when, _ in rows):
        lines.append(plain(f"{NO_TIME} — {labels(language)['tbc']}"))
    return Block(tuple(lines))


def blocks(card: Card) -> list[Block]:
    top = [Line((Span(f"{HEADER_EMOJI[card.kind]} "), Span(card.title, bold=True)))]
    if card.subtitle:
        top.append(plain(card.subtitle))
    result = [Block(tuple(top))]
    if card.schedule:
        result.append(schedule_block(card.schedule, card.language))
    for section in card.sections:
        emoji = section_emoji(card.kind, section.id)
        heading = Line((Span(f"{emoji} {section.heading}".rstrip(), bold=True),))
        lines = tuple(item_line(item) for item in section.items)
        body = sum(len(line.render()) for line in lines)
        result.append(Block(lines, heading, quote=body > FOLD_CHARS))
    return result


def render(card: Card, limit: int = MAX_LENGTH) -> list[str]:
    """The card as Telegram HTML messages, each at most `limit` characters long."""
    return pack(blocks(card), limit)

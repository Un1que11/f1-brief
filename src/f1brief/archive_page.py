# Russian UI texts: Cyrillic letters that look like Latin ones are intended here.
# ruff: noqa: RUF001
"""The story archive as one static HTML page (F28), generated locally from the story store.

Every story with its timeline, newest first, and three filters: the stories followed now, all
of them, and the resolved ones. The filters are radio buttons styled with CSS, so the page has
no script at all and its Content-Security-Policy forbids any: it works when opened from a
file, in the browser or in Telegram's file preview. Every text comes from the store and is
HTML-escaped; links lead only to the sources' http(s) URLs (`stories.check_update` refuses any
other) and open without a referrer.

The page is written by `f1brief archive-page` and sent as a file with the bot's `/archive`
reply. It is Russian, like the bot.
"""

from dataclasses import dataclass
from datetime import datetime
from html import escape

from f1brief.schedule import WARSAW
from f1brief.stories import (
    STATUS_RU,
    Entity,
    EntityKind,
    Story,
    StoryStatus,
    StoryStore,
    StoryUpdate,
    Subscription,
)

DEFAULT_OUTPUT = "archive.html"

KIND_RU = {
    EntityKind.DRIVER: "пилот",
    EntityKind.TEAM: "команда",
    EntityKind.PERSON: "персона",
}

# (filter id, label); the order is the order of the filter buttons.
FILTERS = (
    ("followed", "Слежу"),
    ("all", "Все"),
    ("resolved", "Завершённые"),
)
EMPTY_RU = {
    "followed": "Ты пока не следишь ни за одним сюжетом.",
    "all": "Сюжетов пока нет.",
    "resolved": "Завершённых сюжетов нет.",
}


@dataclass(frozen=True)
class Entry:
    """One story as the page shows it."""

    story: Story
    entities: tuple[Entity, ...]
    timeline: tuple[StoryUpdate, ...]
    subscription: Subscription | None

    @property
    def followed(self) -> bool:
        return self.subscription is not None and self.subscription.active

    @property
    def resolved(self) -> bool:
        return self.story.status is StoryStatus.RESOLVED


def collect(store: StoryStore) -> list[Entry]:
    """Every story in the store, the most recently updated first."""
    return [
        Entry(
            story=story,
            entities=tuple(store.entities(story.id)),
            timeline=tuple(store.timeline(story.id)),
            subscription=store.subscription(story.id),
        )
        for story in store.stories()
    ]


def _date(moment: datetime) -> str:
    return moment.astimezone(WARSAW).strftime("%d.%m.%Y")


def _e(text: str) -> str:
    return escape(text, quote=True)


def counts(entries: list[Entry]) -> dict[str, int]:
    """How many stories each filter shows."""
    return {
        "followed": sum(e.followed for e in entries),
        "all": len(entries),
        "resolved": sum(e.resolved for e in entries),
    }


def default_filter(entries: list[Entry]) -> str:
    """The filter the page opens with: the followed stories, or all when none is followed."""
    return "followed" if any(e.followed for e in entries) else "all"


def _update(entry: StoryUpdate) -> str:
    update = entry.update
    when = _date(update.published_at or entry.recorded_at)
    status = (
        f'\n      <p class="moved">Статус → {_e(STATUS_RU[entry.status])}</p>'
        if entry.status
        else ""
    )
    return (
        "    <li>\n"
        f'      <p class="meta"><time>{when}</time> · {_e(update.source)}</p>\n'
        f'      <p class="headline"><a href="{_e(update.url)}" target="_blank" '
        f'rel="noopener noreferrer">{_e(update.headline)}</a></p>\n'
        f"      <p>{_e(update.what_changed)}</p>{status}\n"
        "    </li>"
    )


def _story(entry: Entry, now: datetime) -> str:
    story = entry.story
    classes = ["story"]
    if entry.followed:
        classes.append("followed")
    if entry.resolved:
        classes.append("resolved")
    status = _e(STATUS_RU[story.status])
    badges = [f'<span class="badge status-{story.status.value}">{status}</span>']
    if entry.followed:
        badges.append('<span class="badge follow">следишь</span>')
        sub = entry.subscription
        if sub is not None and sub.muted_until is not None and sub.muted(now):
            badges.append(
                f'<span class="badge muted">без уведомлений до {_date(sub.muted_until)}</span>'
            )
    entities = ", ".join(f"{_e(e.ref)} ({KIND_RU[e.kind]})" for e in entry.entities)
    who = f'\n  <p class="entities">Кто: {entities}</p>' if entities else ""
    updates = "\n".join(_update(u) for u in entry.timeline)
    return (
        f'<article class="{" ".join(classes)}" id="story-{story.id}">\n'
        f"  <h2>#{story.id} {_e(story.title)}</h2>\n"
        f'  <p class="badges">{" ".join(badges)} '
        f'<span class="updated">обновлён {_date(story.last_update_at)}</span></p>\n'
        f'  <p class="summary">{_e(story.summary)}</p>{who}\n'
        f"  <details open>\n"
        f"   <summary>Хронология ({len(entry.timeline)})</summary>\n"
        f'   <ol class="timeline">\n{updates}\n   </ol>\n'
        f"  </details>\n"
        f"</article>"
    )


_CSS = """
:root { color-scheme: light dark; --bg: #fff; --fg: #1b1b1f; --muted: #5f6368;
  --line: #dadce0; --card: #f8f9fa; --accent: #ff8000; --link: #0b57d0; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #141416; --fg: #e8eaed; --muted: #9aa0a6; --line: #3c4043;
    --card: #1f1f23; --link: #8ab4f8; }
}
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 46rem; padding: 1rem; background: var(--bg);
  color: var(--fg); font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
h2 { font-size: 1.1rem; margin: 0 0 .4rem; }
p { margin: .25rem 0; }
a { color: var(--link); }
.generated, .meta, .updated, .entities, summary { color: var(--muted); font-size: .9rem; }
.filters { position: absolute; opacity: 0; pointer-events: none; }
nav { display: flex; gap: .5rem; flex-wrap: wrap; margin: 1rem 0; }
nav label { border: 1px solid var(--line); border-radius: 999px; padding: .3rem .9rem;
  cursor: pointer; user-select: none; }
.story { background: var(--card); border: 1px solid var(--line); border-radius: .75rem;
  padding: .9rem 1rem; margin: 0 0 1rem; }
.badge { display: inline-block; border-radius: .4rem; padding: 0 .45rem; font-size: .8rem;
  border: 1px solid var(--line); }
.badge.follow { border-color: var(--accent); color: var(--accent); }
.status-confirmed { border-color: #188038; }
.status-denied { border-color: #d93025; }
.timeline { list-style: none; margin: .5rem 0 0; padding: 0 0 0 .9rem;
  border-left: 2px solid var(--line); }
.timeline li { margin: 0 0 .9rem; }
.headline { font-weight: 600; }
.moved { font-weight: 600; color: var(--accent); }
.empty { display: none; color: var(--muted); }
footer { color: var(--muted); font-size: .8rem; margin-top: 2rem; }
#f-followed:checked ~ main .story:not(.followed),
#f-resolved:checked ~ main .story:not(.resolved) { display: none; }
#f-followed:checked ~ nav label[for=f-followed],
#f-all:checked ~ nav label[for=f-all],
#f-resolved:checked ~ nav label[for=f-resolved] { background: var(--accent); color: #fff;
  border-color: var(--accent); }
#f-followed:checked ~ main .empty-followed,
#f-all:checked ~ main .empty-all,
#f-resolved:checked ~ main .empty-resolved { display: block; }
"""


def render(entries: list[Entry], now: datetime) -> str:
    """The archive page for `entries` (see `collect`), generated at `now`."""
    numbers = counts(entries)
    start = default_filter(entries)
    radios = "\n".join(
        f'<input class="filters" type="radio" name="filter" id="f-{key}"'
        f"{' checked' if key == start else ''}>"
        for key, _ in FILTERS
    )
    labels = "\n".join(
        f'  <label for="f-{key}">{label} ({numbers[key]})</label>' for key, label in FILTERS
    )
    empties = "\n".join(
        f'<p class="empty empty-{key}">{EMPTY_RU[key]}</p>'
        for key, _ in FILTERS
        if numbers[key] == 0
    )
    body = "\n".join(_story(e, now) for e in entries)
    generated = now.astimezone(WARSAW).strftime("%d.%m.%Y %H:%M")
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<meta name="referrer" content="no-referrer">
<title>F1 Brief — архив сюжетов</title>
<style>{_CSS}</style>
</head>
<body>
<h1>Архив сюжетов</h1>
<p class="generated">Сформирован {generated} (Варшава) · сюжетов: {numbers["all"]}</p>
{radios}
<nav>
{labels}
</nav>
<main>
{empties}
{body}
</main>
<footer>Каждое обновление ведёт на статью-источник. Слухи помечены статусом «слух».</footer>
</body>
</html>
"""


def build(store: StoryStore, now: datetime) -> str:
    """The archive page of every story in the store."""
    return render(collect(store), now)


def filename(now: datetime) -> str:
    """The name the page is sent under: f1-archive-<Warsaw date>.html."""
    return f"f1-archive-{now.astimezone(WARSAW):%Y-%m-%d}.html"

# Russian UI texts: Cyrillic letters that look like Latin ones are intended here.
# ruff: noqa: RUF001
"""Every text the Telegram bot (F23, F40) shows: replies, button labels, the short answers to
pressed buttons and the command menu, with the date formats they use.

They live here, apart from the bot's logic, so the message catalog (F30) can take them over
as they are: plain strings, `str.format` templates with named fields, and a few small
functions where a text depends on a number or a date. Russian only for now; times are Warsaw
time, 24-hour. The texts are plain: the bot escapes them for Telegram HTML (F41); only
`next_race` lays its text out as `telegram_html` blocks.
"""

from collections.abc import Sequence
from datetime import datetime

from f1brief import jolpica
from f1brief import telegram_html as th
from f1brief.schedule import WARSAW, Race, SessionKind
from f1brief.stories import StoryStatus

# --- formats ------------------------------------------------------------------------------

WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

SESSIONS = {
    SessionKind.FP1: "Практика 1",
    SessionKind.FP2: "Практика 2",
    SessionKind.FP3: "Практика 3",
    SessionKind.SPRINT_QUALIFYING: "Спринт-квалификация",
    SessionKind.SPRINT: "Спринт",
    SessionKind.QUALIFYING: "Квалификация",
    SessionKind.RACE: "Гонка",
}


def date(moment: datetime) -> str:
    """ "19.10.2026", in Warsaw time."""
    return moment.astimezone(WARSAW).strftime("%d.%m.%Y")


def date_time(moment: datetime) -> str:
    """ "19.10.2026 14:05", in Warsaw time."""
    return moment.astimezone(WARSAW).strftime("%d.%m.%Y %H:%M")


def day_time(moment: datetime) -> str:
    """ "Пт 24.10 19:30", in Warsaw time."""
    local = moment.astimezone(WARSAW)
    return f"{WEEKDAYS[local.weekday()]} {local:%d.%m %H:%M}"


def days_ru(n: int) -> str:
    """ "1 день", "3 дня", "60 дней"."""
    if n % 10 == 1 and n % 100 != 11:
        word = "день"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        word = "дня"
    else:
        word = "дней"
    return f"{n} {word}"


# --- the main menu ------------------------------------------------------------------------

MENU_STORIES = "📰 Сюжеты"
MENU_ARCHIVE = "🗂 Архив"
MENU_NEXT = "🏁 Следующая гонка"
MENU_FAVOURITES = "⭐ Избранное"
MENU_HELP = "❓ Помощь"
MENU_BACK = "← Меню"

MENU_PROMPT = "Я понимаю кнопки и команды. Что показать?"


def start(days_before: int, next_digest: str) -> str:
    """The greeting for /start: what the bot does and when the next digest comes."""
    if days_before == 0:
        when = "в день первой практики"
    else:
        when = f"за {days_ru(days_before)} до первой практики"
    return "\n".join(
        [
            f"Привет! Я присылаю F1-дайджест {when}: что изменилось с прошлой гонки, как дела "
            "у твоих команд и гонщиков и чего ждать на уикенде.",
            "После квалификации — короткое сообщение о том, как она прошла у избранных.",
            "Новости по сюжетам, за которыми ты следишь (контракты, переходы, слухи), приходят "
            "сразу, а ночью (23:00–08:00) — утром.",
            next_digest,
        ]
    )


HELP = (
    "Команды:\n"
    "/start — что умеет бот и меню\n"
    "/stories — сюжеты, за которыми ты следишь, и недавние\n"
    "/story <номер> — хронология сюжета с источниками\n"
    "/follow <номер> — следить за сюжетом\n"
    "/unfollow <номер> — отписаться (сюжет останется в архиве)\n"
    "/mute <номер> <дни> — без уведомлений на N дней\n"
    "/archive — все сюжеты, включая завершённые\n"
    "/next — следующая гонка и когда придёт дайджест\n"
    "/favourites — избранные команды и гонщики\n"
    "\n"
    "Всё это есть и на кнопках: /start."
)

UNKNOWN_COMMAND = "Не знаю команду /{name}."

# The commands under the "/" button in Telegram (setMyCommands): (command, description).
COMMANDS = (
    ("start", "Что умеет бот и меню"),
    ("stories", "Сюжеты: твои и недавние"),
    ("archive", "Все сюжеты, включая завершённые"),
    ("next", "Следующая гонка и дайджест к ней"),
    ("favourites", "Избранные команды и гонщики"),
    ("help", "Все команды"),
)

# --- the next race and the digest ---------------------------------------------------------

CALENDAR_UNAVAILABLE = "Календарь сейчас недоступен, попробуй позже."
NO_RACES = "В календаре сезона больше нет гонок."
NO_DIGEST_PLANNED = "Следующий дайджест — когда в календаре появится новая гонка."
NEXT_DIGEST_AT = "Следующий дайджест придёт {when} по Варшаве — к этапу «{race}»."
NEXT_DIGEST_SOON = "Дайджест к этапу «{race}» придёт в течение часа."
DIGEST_SENT = "Дайджест к этому этапу отправлен {when}."
DIGEST_AT = "Дайджест к этому этапу придёт {when} по Варшаве."
DIGEST_SOON = "Дайджест к этому этапу придёт в течение часа."
DIGEST_NOT_SENT = "Дайджест к этому этапу не отправлялся."


NO_TIME = "--:--"
SCHEDULE = "Расписание, время варшавское:"
TIME_TBC = f"{NO_TIME} — время уточняется"
SOURCE = "Источник: {source}."


def next_race(race: Race, digest: str) -> list[th.Block]:
    """Round and circuit, the session times in Warsaw as an aligned table, then the line
    about the digest and the source."""
    title = f"Этап {race.round}: {race.name} ({race.season})"
    top = th.Block(
        (
            th.Line((th.Span("🏁 "), th.Span(title, bold=True))),
            th.plain(f"{race.circuit.name}, {race.circuit.locality}, {race.circuit.country}"),
        )
    )
    rows: list[str] = []
    for session in race.sessions:
        if session.start_utc is not None:
            when = day_time(session.start_utc)
        else:
            when = f"{WEEKDAYS[session.date.weekday()]} {session.date:%d.%m} {NO_TIME}"
        rows.append(f"{when}  {SESSIONS[session.kind]}")
    lines = [th.plain(SCHEDULE), th.Line((th.Span("\n".join(rows), pre=True),))]
    if any(session.start_utc is None for session in race.sessions):
        lines.append(th.plain(TIME_TBC))
    bottom = th.Block((th.plain(digest), th.plain(SOURCE.format(source=jolpica.SOURCE_NAME))))
    return [top, th.Block(tuple(lines)), bottom]


# --- favourites ---------------------------------------------------------------------------

FAVOURITES_TITLE = "⭐ Избранное — по нему собирается дайджест."
FAVOURITE_TEAMS = "Команды: {names}"
FAVOURITE_DRIVERS = "Гонщики: {names}"
NONE = "нет"
FAVOURITES_HINT = (
    "Убрать — кнопкой ниже. Добавить можно из сюжета: после «Следить» появится кнопка "
    "«Следить за …»."
)
REMOVE_BUTTON = "✕ {name}"
REMOVED = "Убрал из избранного: {name}. Вернуть можно в config.toml или кнопкой из сюжета."
ALREADY_REMOVED = "{name} уже нет в избранном."
NOT_REMOVED = "Не получилось убрать из избранного: {error}"
ANSWER_REMOVED = "Убрано"


def favourites(teams: Sequence[str], drivers: Sequence[str]) -> str:
    return "\n".join(
        [
            FAVOURITES_TITLE,
            "",
            FAVOURITE_TEAMS.format(names=", ".join(teams) or NONE),
            FAVOURITE_DRIVERS.format(names=", ".join(drivers) or NONE),
            "",
            FAVOURITES_HINT,
        ]
    )


# --- stories ------------------------------------------------------------------------------

NO_STORIES = "Сюжетов пока нет. Все сюжеты: /archive"
FOLLOWING = "Ты следишь:"
FOLLOWING_NONE = "Ты пока не следишь ни за одним сюжетом."
RECENT = "Недавние сюжеты:"
MUTED_UNTIL = " (без уведомлений до {date})"
STORIES_HINT = "Нажми на сюжет, чтобы открыть хронологию."
STORY_LINE = "{title} — {status}, {date}"
STORY_LINE_NUMBERED = "#{id} {title} — {status}, {date}"
ICON_FOLLOWED = "🔔"
ICON_MUTED = "🔕"

ARCHIVE_EMPTY = "Архив пуст: сюжетов пока нет."
ARCHIVE_TITLE = "Все сюжеты ({count}):"
ARCHIVE_FOLLOWED = " — следишь"
ARCHIVE_HINT = "Подробнее: /story <номер>"
ARCHIVE_CAPTION = (
    "Архив страницей: открой файл в браузере. Фильтры: слежу / все / завершённые, "
    "у каждого обновления — ссылка на источник."
)

# The status chips of a story (alerts, the timeline).
STATUS_ICONS = {
    StoryStatus.RUMOR: "🟡",
    StoryStatus.DEVELOPING: "🟠",
    StoryStatus.CONFIRMED: "🟢",
    StoryStatus.DENIED: "🔴",
    StoryStatus.RESOLVED: "⚪",
}
ICON_STORY = "📰"
ICON_ALERT = "🔔"
ALERT_STATUS_CHANGED = "{chip} (статус изменился)"
BUTTON_TIMELINE = "Хронология"

STORY_TITLE = "#{id} {title}"
STORY_STATUS = "Статус: {status} · {following}"
STORY_FOLLOWED = "ты следишь"
STORY_NOT_FOLLOWED = "не следишь"
TIMELINE = "Хронология:"
TIMELINE_ENTRY = "{date} — {headline} ({source})"
STATUS_CHANGE = "Статус → {status}"

STORY_NUMBER_MISSING = "Укажи номер сюжета, например: /{command} 3"
STORY_UNKNOWN = "Нет сюжета {arg}. Все сюжеты: /archive"

FOLLOWED = "Теперь ты следишь за сюжетом #{id} «{title}»."
ALREADY_FOLLOWED = "Ты уже следишь за сюжетом #{id} «{title}»."
UNFOLLOWED = "Ты больше не следишь за сюжетом #{id} «{title}». Он остаётся в архиве: /story {id}"
NOT_FOLLOWED = "Ты не следишь за сюжетом #{id}."
MUTE_DAYS = "Укажи число дней от 1 до {max}, например: /mute {id} 7"
MUTE_NOT_FOLLOWED = "Ты не следишь за сюжетом #{id}. Сначала: /follow {id}"
MUTED = "Сюжет #{id} «{title}» без уведомлений до {until}."

BUTTON_FOLLOW = "Следить"
BUTTON_UNFOLLOW = "Отписаться"
BUTTON_FOLLOW_ENTITIES = "Следить за {names}"


def button_mute(days: int) -> str:
    return f"Без уведомлений на {days_ru(days)}"


ADDED = "Добавил в избранное: {names}. Они появятся в следующем дайджесте."
NOT_ADDED = "Не получилось добавить в избранное: {error}"

# Short answers shown in the client when a button is pressed.
ANSWER_GONE = "Этой кнопки больше нет"
ANSWER_FOLLOWED = "Следишь"
ANSWER_UNFOLLOWED = "Отписка"
ANSWER_MUTED = "Без уведомлений"
ANSWER_ALREADY_FAVOURITE = "Уже в избранном"
ANSWER_ADDED = "Добавлено"
ANSWER_FAILED = "Не получилось"

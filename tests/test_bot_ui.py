# The bot's texts are Russian: Cyrillic letters that look like Latin ones are intended here.
# ruff: noqa: RUF001
"""The bot UI that explains itself (F40): /start, the main menu, story buttons, the next race,
favourites, the command menu, the ignored-chat log and `bot --check`. Synthetic update
payloads, the recorded 2026 calendar and a mock Bot API: nothing reaches Telegram."""

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import load_fixture
from test_bot import (
    BACK,
    CHAT,
    NOW,
    OTHER_CHAT,
    TOKEN,
    Favourites,
    MockTelegram,
    api,
    buttons_of,
    message,
    press,
    seed,
    text_of,
)

from f1brief import bot, bot_texts, cli, config, schedule
from f1brief import telegram_send as ts
from f1brief.bot import Answer, Bot, Button, Document, Reply
from f1brief.cli import main
from f1brief.schedule import ScheduleError, SessionKind
from f1brief.send_log import StateError
from f1brief.stories import StoryStore

# NOW is Monday 19.10.2026 11:00 in Warsaw: the next round is the United States GP (round 18,
# FP1 Fri 23.10 19:30 Warsaw), whose send window opens Wed 21.10 00:00 Warsaw.
USA_WINDOW_OPENS = datetime(2026, 10, 20, 22, tzinfo=UTC)
USA_FP1 = datetime(2026, 10, 23, 17, 30, tzinfo=UTC)


@pytest.fixture
def races() -> list[schedule.Race]:
    return schedule.parse_schedule(load_fixture("jolpica/schedule_2026.json"))


class Removals:
    """Records what the bot asks to remove, and keeps the result like the config would."""

    def __init__(self, favourites: config.Favourites = config.DEFAULT_FAVOURITES) -> None:
        self.current = favourites
        self.calls: list[tuple[str, str]] = []

    def __call__(self, kind: str, fav_id: str) -> config.Favourites:
        self.calls.append((kind, fav_id))
        kept = tuple(f for f in getattr(self.current, kind) if f.id != fav_id)
        self.current = config.Favourites(**{**vars(self.current), kind: kept})
        return self.current


@pytest.fixture
def removals() -> Removals:
    return Removals()


@pytest.fixture
def warnings() -> list[str]:
    return []


@pytest.fixture
def ui(
    store: StoryStore,
    races: list[schedule.Race],
    removals: Removals,
    warnings: list[str],
) -> Bot:
    return Bot(
        store,
        CHAT,
        config.DEFAULT_FAVOURITES,
        Favourites(),
        remove_favourite=removals,
        calendar=lambda: races,
        warn=warnings.append,
    )


@pytest.fixture
def store(tmp_path: Path):
    with StoryStore(tmp_path / "state.db") as opened:
        yield opened


# --- /start and the main menu -----------------------------------------------------------------


def test_start_explains_the_bot_and_when_the_next_digest_comes(ui: Bot) -> None:
    [reply] = ui.handle(message("/start"), NOW)

    lines = reply.text.splitlines()
    assert len(lines) == 4
    assert lines[0].startswith("Привет! Я присылаю F1-дайджест за 2 дня до первой практики")
    assert "После квалификации" in lines[1]
    assert "23:00–08:00" in lines[2]
    assert lines[3] == (
        "Следующий дайджест придёт Ср 21.10 00:00 по Варшаве — к этапу «United States Grand Prix»."
    )
    assert reply.buttons == bot.MAIN_MENU


def test_the_main_menu_has_the_five_screens() -> None:
    labels = [b.text for row in bot.MAIN_MENU for b in row]
    assert labels == ["📰 Сюжеты", "🗂 Архив", "🏁 Следующая гонка", "⭐ Избранное", "❓ Помощь"]


def test_start_names_the_following_round_once_the_digest_went_out(
    ui: Bot, races: list[schedule.Race]
) -> None:
    ui.digest_sent = lambda race: NOW if race.round == 18 else None
    text = text_of(ui.handle(message("/start"), NOW))
    assert "Следующий дайджест придёт Ср 28.10 00:00 по Варшаве" in text
    assert "«Mexico City Grand Prix»" in text


def test_start_inside_the_send_window(ui: Bot) -> None:
    text = text_of(ui.handle(message("/start"), USA_WINDOW_OPENS + timedelta(hours=2)))
    assert "Дайджест к этапу «United States Grand Prix» придёт в течение часа." in text


def test_start_with_another_lead_time(ui: Bot) -> None:
    ui.days_before = 0
    text = text_of(ui.handle(message("/start"), NOW))
    assert "F1-дайджест в день первой практики" in text
    assert "придёт Пт 23.10 00:00 по Варшаве" in text
    ui.days_before = 5
    assert "за 5 дней до первой практики" in text_of(ui.handle(message("/start"), NOW))


def test_start_without_the_calendar_still_answers(ui: Bot, warnings: list[str]) -> None:
    def unavailable() -> list[schedule.Race]:
        raise ScheduleError("Jolpica: HTTP 503")

    ui.calendar = unavailable
    [reply] = ui.handle(message("/start"), NOW)
    assert reply.text.endswith("Календарь сейчас недоступен, попробуй позже.")
    assert reply.buttons == bot.MAIN_MENU
    assert warnings == ["calendar: Jolpica: HTTP 503"]


def test_start_when_the_send_log_fails(ui: Bot, warnings: list[str]) -> None:
    def broken(race: schedule.Race) -> datetime | None:
        raise StateError("state.db: disk I/O error")

    ui.digest_sent = broken
    assert "Календарь сейчас недоступен" in text_of(ui.handle(message("/start"), NOW))
    assert warnings == ["send log: state.db: disk I/O error"]


def test_start_after_the_last_race(ui: Bot) -> None:
    text = text_of(ui.handle(message("/start"), datetime(2026, 12, 31, tzinfo=UTC)))
    assert text.endswith("Следующий дайджест — когда в календаре появится новая гонка.")


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("menu:stories", "Сюжетов пока нет"),
        ("menu:archive", "Архив пуст"),
        ("menu:next", "🏁 Этап 18: United States Grand Prix (2026)"),
        ("menu:favs", "⭐ Избранное"),
        ("menu:help", "/stories — сюжеты"),
    ],
)
def test_each_menu_button_opens_its_screen_with_a_way_back(
    ui: Bot, data: str, expected: str
) -> None:
    actions = ui.handle(press(data), NOW)
    assert actions[0] == Answer("cb1")
    assert expected in text_of(actions)
    assert buttons_of(actions)[-1] == BACK


def test_the_menu_button_shows_the_menu(ui: Bot) -> None:
    actions = ui.handle(press("menu:main"), NOW)
    assert actions == [Answer("cb1"), Reply(bot_texts.MENU_PROMPT, bot.MAIN_MENU)]


def test_the_archive_button_sends_the_page_too(store: StoryStore, ui: Bot) -> None:
    seed(store)
    actions = ui.handle(press("menu:archive"), NOW)
    assert isinstance(actions[-1], Document)


@pytest.mark.parametrize("data", ["menu:", "menu:settings", "unfav:", "unfav:people:x"])
def test_an_unknown_menu_or_favourite_button(ui: Bot, data: str) -> None:
    assert ui.handle(press(data), NOW) == [Answer("cb1", "Этой кнопки больше нет")]


def test_an_unknown_command_keeps_the_way_back(ui: Bot) -> None:
    actions = ui.handle(message("/subscribe"), NOW)
    assert "Не знаю команду /subscribe." in text_of(actions)
    assert buttons_of(actions) == [BACK]


# --- stories as buttons -----------------------------------------------------------------------


def test_a_story_button_opens_its_timeline(store: StoryStore, ui: Bot) -> None:
    story = seed(store)
    actions = ui.handle(press(f"story:{story.id}"), NOW)
    assert actions[0] == Answer("cb1")
    assert text_of(actions).startswith(f"#{story.id} Бирман в Ferrari в 2028?\nСтатус: слух")
    assert buttons_of(actions) == [Button("Следить", f"follow:{story.id}"), BACK]


def test_a_followed_story_shows_a_check_mark_and_a_long_title_is_cut(
    store: StoryStore, ui: Bot
) -> None:
    story = seed(store, title="Очень длинный заголовок сюжета, " * 4)
    store.follow(story.id, NOW)
    [button, back] = buttons_of(ui.handle(message("/stories"), NOW))
    assert button.text.startswith("🔔 Очень длинный")
    assert button.text.endswith("…")
    assert len(button.text) == len("🔔 ") + bot.BUTTON_TITLE
    assert back == BACK


def test_the_mute_button_mutes_a_followed_story_for_7_days(store: StoryStore, ui: Bot) -> None:
    story = seed(store)
    store.follow(story.id, NOW)

    actions = ui.handle(press(f"mute:{story.id}:7"), NOW)

    assert actions[0] == Answer("cb1", "Без уведомлений")
    # Seven days later Warsaw is on winter time: 09:00 UTC is 10:00 there.
    assert "без уведомлений до 26.10.2026 10:00" in text_of(actions)
    assert store.subscription(story.id).muted_until == NOW + timedelta(days=7)
    assert Button("Отписаться", f"unfollow:{story.id}") in buttons_of(actions)


def test_the_mute_button_on_a_story_not_followed(store: StoryStore, ui: Bot) -> None:
    story = seed(store)
    actions = ui.handle(press(f"mute:{story.id}:7"), NOW)
    assert f"Ты не следишь за сюжетом #{story.id}. Сначала: /follow {story.id}" in text_of(actions)
    assert store.subscription(story.id) is None


@pytest.mark.parametrize("data", ["mute:1", "mute:1:0", "mute:1:x"])
def test_a_mute_button_without_valid_days(store: StoryStore, ui: Bot, data: str) -> None:
    story = seed(store)
    store.follow(story.id, NOW)
    assert "Укажи число дней" in text_of(ui.handle(press(data), NOW))


def test_typed_commands_with_ids_keep_working(store: StoryStore, ui: Bot) -> None:
    story = seed(store)
    assert "Теперь ты следишь" in text_of(ui.handle(message(f"/follow {story.id}"), NOW))
    assert "без уведомлений до" in text_of(ui.handle(message(f"/mute {story.id} 3"), NOW))
    assert "Хронология:" in text_of(ui.handle(message(f"/story {story.id}"), NOW))
    assert "больше не следишь" in text_of(ui.handle(message(f"/unfollow {story.id}"), NOW))


# --- the next race ----------------------------------------------------------------------------


def test_next_shows_the_round_the_sessions_in_warsaw_and_the_digest(ui: Bot) -> None:
    actions = ui.handle(message("/next"), NOW)

    lines = text_of(actions).splitlines()
    assert lines[:5] == [
        "🏁 Этап 18: United States Grand Prix (2026)",
        "Circuit of the Americas, Austin, USA",
        "",
        "Расписание (время варшавское):",
        "Пт 23.10 19:30 — Практика 1",
    ]
    assert lines[-3:] == [
        "",
        "Дайджест к этому этапу придёт Ср 21.10 00:00 по Варшаве.",
        "Источник: Jolpica-F1.",
    ]
    assert buttons_of(actions) == [BACK]


def test_next_names_every_session_in_russian_with_the_dst_change(ui: Bot) -> None:
    sessions = text_of(ui.handle(message("/next"), NOW)).splitlines()[4:9]
    assert sessions == [
        "Пт 23.10 19:30 — Практика 1",
        "Пт 23.10 23:00 — Практика 2",
        "Сб 24.10 19:30 — Практика 3",
        "Сб 24.10 23:00 — Квалификация",
        # Warsaw leaves summer time on 25.10: the race is at 20:00 UTC, 21:00 Warsaw.
        "Вс 25.10 21:00 — Гонка",
    ]


@pytest.mark.parametrize(
    ("now", "sent", "line"),
    [
        (USA_WINDOW_OPENS + timedelta(hours=1), None, "придёт в течение часа"),
        (USA_FP1 + timedelta(hours=1), None, "не отправлялся"),
        (
            USA_FP1 + timedelta(hours=1),
            datetime(2026, 10, 20, 22, 5, tzinfo=UTC),
            "отправлен 21.10.2026 00:05",
        ),
    ],
)
def test_next_says_what_happened_to_the_digest(
    ui: Bot, now: datetime, sent: datetime | None, line: str
) -> None:
    ui.digest_sent = lambda race: sent
    assert f"Дайджест к этому этапу {line}" in text_of(ui.handle(message("/next"), now))


def test_next_with_a_session_time_to_be_confirmed(ui: Bot, races: list[schedule.Race]) -> None:
    race = races[17]
    fp1 = race.sessions[0]
    tbc = schedule.Session(fp1.kind, fp1.date, None)
    ui.calendar = lambda: [type(race)(**{**vars(race), "sessions": (tbc, *race.sessions[1:])})]
    text = text_of(ui.handle(message("/next"), NOW))
    assert "Пт 23.10, время уточняется — Практика 1" in text


def test_next_without_the_calendar_or_races(ui: Bot) -> None:
    ui.calendar = None
    assert (
        text_of(ui.handle(message("/next"), NOW)) == "Календарь сейчас недоступен, попробуй позже."
    )
    ui.calendar = list
    assert text_of(ui.handle(message("/next"), NOW)) == "В календаре сезона больше нет гонок."


def test_every_session_kind_has_a_russian_name() -> None:
    assert set(bot_texts.SESSIONS) == set(SessionKind)


# --- favourites -------------------------------------------------------------------------------


def test_favourites_lists_teams_and_drivers_with_remove_buttons(ui: Bot) -> None:
    actions = ui.handle(message("/favourites"), NOW)

    text = text_of(actions)
    assert "Команды: McLaren, Haas" in text
    assert "Гонщики: George Russell, Esteban Ocon, Oliver Bearman" in text
    assert buttons_of(actions) == [
        Button("✕ McLaren", "unfav:teams:mclaren"),
        Button("✕ Haas", "unfav:teams:haas"),
        Button("✕ George Russell", "unfav:drivers:russell"),
        Button("✕ Esteban Ocon", "unfav:drivers:ocon"),
        Button("✕ Oliver Bearman", "unfav:drivers:bearman"),
        BACK,
    ]


def test_a_remove_button_takes_the_favourite_out(ui: Bot, removals: Removals) -> None:
    actions = ui.handle(press("unfav:drivers:ocon"), NOW)

    assert removals.calls == [("drivers", "ocon")]
    assert actions[0] == Answer("cb1", "Убрано")
    text = text_of(actions)
    assert text.startswith("Убрал из избранного: Esteban Ocon.")
    assert "Гонщики: George Russell, Oliver Bearman" in text
    assert Button("✕ Esteban Ocon", "unfav:drivers:ocon") not in buttons_of(actions)
    assert ui.favourites.driver_ids == ("russell", "bearman")
    # Pressed again (an old message): nothing to remove.
    again = ui.handle(press("unfav:drivers:ocon", update_id=2), NOW)
    assert again[0] == Answer("cb2")
    assert text_of(again).startswith("ocon уже нет в избранном.")
    assert removals.calls == [("drivers", "ocon")]


def test_removing_every_team_shows_none(ui: Bot) -> None:
    ui.handle(press("unfav:teams:mclaren"), NOW)
    actions = ui.handle(press("unfav:teams:haas", update_id=2), NOW)
    assert "Команды: нет" in text_of(actions)


def test_a_config_error_on_removal_is_shown(ui: Bot) -> None:
    def broken(kind: str, fav_id: str) -> config.Favourites:
        raise config.ConfigError("config.toml: not valid TOML: line 3")

    ui.remove_favourite = broken
    actions = ui.handle(press("unfav:teams:haas"), NOW)
    assert actions[0] == Answer("cb1", "Не получилось")
    assert "Не получилось убрать из избранного: config.toml: not valid TOML" in text_of(actions)
    assert ui.favourites == config.DEFAULT_FAVOURITES


def test_without_a_way_to_remove_there_are_no_remove_buttons(ui: Bot) -> None:
    ui.remove_favourite = None
    assert buttons_of(ui.handle(message("/favourites"), NOW)) == [BACK]


# --- the ignored-chat log ---------------------------------------------------------------------


def test_another_chat_is_logged_once_per_chat(ui: Bot, warnings: list[str]) -> None:
    assert ui.handle(message("/start", chat=OTHER_CHAT), NOW) == []
    assert ui.handle(press("menu:main", chat=OTHER_CHAT, update_id=2), NOW) == []
    assert ui.handle(message("/stories", chat="616161", update_id=3), NOW) == []
    assert warnings == [
        f"ignored chat {OTHER_CHAT}; TELEGRAM_CHAT_ID is {CHAT}",
        f"ignored chat 616161; TELEGRAM_CHAT_ID is {CHAT}",
    ]


def test_the_configured_chat_is_never_logged(ui: Bot, warnings: list[str]) -> None:
    ui.handle(message("/start"), NOW)
    assert warnings == []


# --- the command menu and --check -------------------------------------------------------------


def test_the_command_menu_is_registered_with_russian_descriptions() -> None:
    mock = MockTelegram()
    assert bot.register_commands(api(mock)) is True
    [sent] = mock.sent("setMyCommands")
    commands = [c["command"] for c in sent["commands"]]
    assert commands == ["start", "stories", "archive", "next", "favourites", "help"]
    assert sent["commands"][0]["description"] == "Что умеет бот и меню"
    assert all(1 <= len(c["description"]) <= 256 for c in sent["commands"])


def test_a_failed_command_menu_is_reported_and_not_fatal() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": f"Bad Request bot{TOKEN}"})

    warnings: list[str] = []
    client = httpx.Client(transport=httpx.MockTransport(refuse))
    telegram = bot.BotApi(ts.Credentials(TOKEN, CHAT), client)
    assert bot.register_commands(telegram, warnings.append) is False
    assert warnings == ["command menu not registered: setMyCommands: Bad Request bot<redacted>"]


def test_a_button_answer_without_text_sends_none() -> None:
    mock = MockTelegram()
    api(mock).send(Answer("cb7"))
    assert mock.sent("answerCallbackQuery") == [{"callback_query_id": "cb7"}]


def telegram_info(webhook_url: str) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        results = {
            "getMe": {"id": 777000, "is_bot": True, "username": "test_f1_bot"},
            "getWebhookInfo": {"url": webhook_url, "pending_update_count": 3},
        }
        return httpx.Response(200, json={"ok": True, "result": results[method]})

    return httpx.MockTransport(answer)


@pytest.mark.parametrize(
    ("url", "line"),
    [
        ("", "webhook: none (polling works)"),
        ("https://example.com/hook", "webhook: set (3 pending update(s)); it blocks polling"),
    ],
)
def test_check_reports_the_bot_the_webhook_and_the_chat(url: str, line: str) -> None:
    client = httpx.Client(transport=telegram_info(url))
    lines = bot.check(bot.BotApi(ts.Credentials(TOKEN, CHAT), client), CHAT)
    assert lines[0] == "bot: @test_f1_bot"
    assert lines[1].startswith(line)
    assert lines[2] == f"TELEGRAM_CHAT_ID: {CHAT}"
    assert "example.com" not in "\n".join(lines)  # the webhook URL may hold a secret


def test_bot_check_prints_and_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ts.TOKEN_VAR, TOKEN)
    monkeypatch.setenv(ts.CHAT_ID_VAR, CHAT)
    monkeypatch.setattr(bot, "check", lambda telegram, chat: ["bot: @test_f1_bot", chat])

    assert main(["bot", "--check"]) == 0
    assert capsys.readouterr().out == f"bot: @test_f1_bot\n{CHAT}\n"


def test_bot_check_reports_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ts.TOKEN_VAR, raising=False)
    monkeypatch.delenv(ts.CHAT_ID_VAR, raising=False)
    assert main(["bot", "--check", "--env-file", "missing"]) == 1
    assert "not set" in capsys.readouterr().err

    monkeypatch.setenv(ts.TOKEN_VAR, TOKEN)
    monkeypatch.setenv(ts.CHAT_ID_VAR, CHAT)

    def unauthorized(telegram: bot.BotApi, chat: str) -> list[str]:
        raise bot.BotError("getMe: Unauthorized", 401)

    monkeypatch.setattr(bot, "check", unauthorized)
    assert main(["bot", "--check"]) == 1
    assert "getMe: Unauthorized" in capsys.readouterr().err


def test_check_takes_no_other_bot_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["bot", "--check", "--dry-run", "--updates", "x.json"]) == 2
    assert "--check takes no other bot options" in capsys.readouterr().err


# --- the CLI's calendar and dry run -----------------------------------------------------------


def test_the_bot_reads_the_calendar_at_most_once_an_hour(
    monkeypatch: pytest.MonkeyPatch, races: list[schedule.Race]
) -> None:
    fetched: list[int] = []
    clock = iter([0.0, 0.0, 1800.0, 3600.0, 3600.0, 3700.0])

    def fetch(season: str = "current") -> list[schedule.Race]:
        fetched.append(1)
        return races

    monkeypatch.setattr(schedule, "fetch_schedule", fetch)
    calendar = cli._cached_calendar(clock=lambda: next(clock))
    assert calendar() is races  # fetched at 0
    assert calendar() is races  # 1800 s later: cached
    assert calendar() is races  # 3600 s: fetched again
    assert calendar() is races  # 3700 s: cached
    assert len(fetched) == 2


@pytest.fixture
def cli_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ts.TOKEN_VAR, raising=False)
    monkeypatch.delenv(ts.CHAT_ID_VAR, raising=False)
    (tmp_path / "config.toml").write_text(
        (Path(__file__).parents[1] / "config.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return tmp_path


def test_dry_run_start_next_and_removal_keep_the_config(
    cli_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    races: list[schedule.Race],
) -> None:
    monkeypatch.setattr(schedule, "fetch_schedule", lambda season="current": races)
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    updates = [
        message("/start"),
        press("menu:next", update_id=2),
        press("unfav:teams:haas", update_id=3),
    ]
    (cli_dir / "updates.json").write_text(json.dumps(updates), encoding="utf-8")
    before = (cli_dir / "config.toml").read_text(encoding="utf-8")

    assert main(["bot", "--dry-run", "--updates", "updates.json", "--chat-id", CHAT]) == 0

    out, err = capsys.readouterr()
    assert "Следующий дайджест придёт Ср 21.10 00:00 по Варшаве" in out
    assert "[📰 Сюжеты] [🗂 Архив]" in out
    assert "🏁 Этап 18: United States Grand Prix (2026)" in out
    assert "Убрал из избранного: Haas." in out
    assert "would remove haas" in err
    assert (cli_dir / "config.toml").read_text(encoding="utf-8") == before


def test_the_digest_send_log_feeds_the_next_race(
    cli_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    races: list[schedule.Race],
) -> None:
    from f1brief import send_log

    with send_log.SendLog(cli_dir / "state.db") as log:
        assert log.claim(2026, 18, datetime(2026, 10, 20, 22, 5, tzinfo=UTC))
    monkeypatch.setattr(schedule, "fetch_schedule", lambda season="current": races)
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 10, 21, 9, tzinfo=UTC))
    (cli_dir / "next.json").write_text(json.dumps([message("/next")]), encoding="utf-8")

    assert main(["bot", "--dry-run", "--updates", "next.json", "--chat-id", CHAT]) == 0
    assert "Дайджест к этому этапу отправлен 21.10.2026 00:05." in capsys.readouterr().out


# --- texts live in one module -----------------------------------------------------------------


def _docstrings(tree: ast.Module) -> set[int]:
    nodes = [tree, *(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.ClassDef))]
    return {
        id(node.body[0].value)
        for node in nodes
        if node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def test_the_bot_keeps_every_text_in_bot_texts() -> None:
    """F30's message catalog takes the texts over from `bot_texts`: bot.py has none."""
    source = Path(bot.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    skip = _docstrings(tree)
    cyrillic = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
        and any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in node.value)
    ]
    assert cyrillic == []


def test_every_callback_fits_telegrams_64_bytes(store: StoryStore, ui: Bot) -> None:
    story = seed(store)
    store.follow(story.id, NOW)
    keyboards: list[Any] = [bot.MAIN_MENU]
    for update in (message("/stories"), message("/favourites"), message(f"/story {story.id}")):
        keyboards += [r.buttons for r in ui.handle(update, NOW) if isinstance(r, Reply)]
    data = [b.data for keyboard in keyboards for row in keyboard for b in row]
    assert data and all(len(d.encode("utf-8")) <= bot.MAX_CALLBACK_DATA for d in data)
    assert all(d.isascii() for d in data)  # language-independent


# --- config.remove_favourite ------------------------------------------------------------------

CHECKED_IN_CONFIG = Path(__file__).parents[1] / "config.toml"


def test_remove_favourite_takes_out_one_block_and_keeps_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    before = CHECKED_IN_CONFIG.read_text(encoding="utf-8")
    path.write_text(before, encoding="utf-8")

    result = config.remove_favourite(path, "drivers", "ocon")

    after = path.read_text(encoding="utf-8")
    block = '[[favourites.drivers]]\nid = "ocon"\nname = "Esteban Ocon"\naliases = ["Ocon"]\n'
    block += "track_future = true\n\n"
    assert block in before
    assert after == before.replace(block, "")  # comments and the other entries untouched
    assert config.load(path) == result
    assert result.favourites.driver_ids == ("russell", "bearman")
    assert result.favourites.teams == config.DEFAULT_FAVOURITES.teams


def test_remove_the_first_and_the_last_block(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(CHECKED_IN_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    config.remove_favourite(path, "teams", "mclaren")
    result = config.remove_favourite(path, "drivers", "bearman")
    text = path.read_text(encoding="utf-8")
    assert "\n\n\n" not in text
    assert text.endswith("track_future = true\n")
    assert result.favourites.team_ids == ("haas",)
    assert result.favourites.driver_ids == ("russell", "ocon")


def test_removing_the_last_entry_writes_an_empty_list(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(CHECKED_IN_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    config.remove_favourite(path, "teams", "mclaren")
    result = config.remove_favourite(path, "teams", "haas")
    # Without `teams = []` the default teams would come back.
    assert result.favourites.teams == ()
    assert config.load(path).favourites.teams == ()
    assert config.load(path).favourites.driver_ids == ("russell", "ocon", "bearman")


def test_add_favourites_after_a_list_was_emptied(tmp_path: Path) -> None:
    # The remove button wrote `teams = []`; "Следить за …" must still add a team after that.
    path = tmp_path / "config.toml"
    path.write_text(CHECKED_IN_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    config.remove_favourite(path, "teams", "mclaren")
    config.remove_favourite(path, "teams", "haas")
    result = config.add_favourites(path, teams=[config.Favourite("ferrari", "Ferrari")])
    assert result.favourites.team_ids == ("ferrari",)
    assert result.favourites.driver_ids == ("russell", "ocon", "bearman")
    assert config.load(path) == result
    assert "# Favourites are lists" in path.read_text(encoding="utf-8")  # comments kept


def test_add_favourites_after_emptying_a_list_of_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config.remove_favourite(path, "drivers", "russell")
    config.remove_favourite(path, "drivers", "ocon")
    config.remove_favourite(path, "drivers", "bearman")
    result = config.add_favourites(path, drivers=[config.Favourite("hamilton", "Lewis Hamilton")])
    assert result.favourites.driver_ids == ("hamilton",)
    assert result.favourites.teams == config.DEFAULT_FAVOURITES.teams
    assert config.load(path) == result


def test_empty_both_lists(tmp_path: Path) -> None:
    # The second emptied list goes into the same [favourites] table, not a second one.
    path = tmp_path / "config.toml"
    path.write_text(CHECKED_IN_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    for kind, ids in (("teams", ("mclaren", "haas")), ("drivers", ("russell", "ocon", "bearman"))):
        for fav_id in ids:
            result = config.remove_favourite(path, kind, fav_id)
    assert result.favourites.teams == ()
    assert result.favourites.drivers == ()
    assert config.load(path) == result
    assert path.read_text(encoding="utf-8").count("[favourites]") == 1


def test_remove_from_a_default_list_the_file_leaves_out(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[digest]\nlanguage = "ru"\n', encoding="utf-8")
    result = config.remove_favourite(path, "drivers", "russell")
    assert result.favourites.driver_ids == ("ocon", "bearman")
    assert config.load(path).favourites.drivers[0].track_future  # Ocon keeps his settings
    assert result.favourites.teams == config.DEFAULT_FAVOURITES.teams


def test_remove_every_default_from_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config.remove_favourite(path, "teams", "mclaren")
    result = config.remove_favourite(path, "teams", "haas")
    assert result.favourites.teams == ()
    assert config.load(path) == result


def test_remove_a_favourite_that_is_not_one(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("# only a comment\n", encoding="utf-8")
    result = config.remove_favourite(path, "teams", "ferrari")
    assert result.favourites == config.DEFAULT_FAVOURITES
    assert path.read_text(encoding="utf-8") == "# only a comment\n"


def test_remove_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    before = CHECKED_IN_CONFIG.read_text(encoding="utf-8")
    path.write_text(before, encoding="utf-8")
    result = config.remove_favourite(path, "teams", "haas", write=False)
    assert result.favourites.team_ids == ("mclaren",)
    assert path.read_text(encoding="utf-8") == before


def test_remove_refuses_a_file_it_cannot_edit_safely(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = '[favourites]\nteams = [{ id = "haas", name = "Haas" }, { id = "x", name = "X" }]\n'
    path.write_text(original, encoding="utf-8")
    with pytest.raises(config.ConfigError, match="edit it by hand"):
        config.remove_favourite(path, "teams", "haas")
    assert path.read_text(encoding="utf-8") == original


def test_remove_needs_a_known_kind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="kind"):
        config.remove_favourite(tmp_path / "config.toml", "people", "x")

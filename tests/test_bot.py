# The bot's texts are Russian: Cyrillic letters that look like Latin ones are intended here.
# ruff: noqa: RUF001
"""The Telegram bot (F23) on synthetic Bot API update payloads (no real bot, chat or text) and
a mock Bot API transport: nothing reaches Telegram."""

import json
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FIXTURES

from f1brief import bot, config
from f1brief import telegram_send as ts
from f1brief.bot import Answer, Bot, Button, Reply
from f1brief.cli import main
from f1brief.config import Favourite
from f1brief.send_log import StateError
from f1brief.stories import Entity, EntityKind, StoryStatus, StoryStore, Update

NOW = datetime(2026, 10, 19, 9, tzinfo=UTC)
CHAT = "424242"
OTHER_CHAT = "515151"
TOKEN = "123456:TEST-token_abcDEF"
UPDATES = FIXTURES / "telegram" / "updates.json"

BEARMAN_FERRARI = [
    Entity(EntityKind.DRIVER, "bearman"),
    Entity(EntityKind.TEAM, "haas"),
    Entity(EntityKind.TEAM, "ferrari"),
    Entity(EntityKind.DRIVER, "hamilton"),
    Entity(EntityKind.PERSON, "fred_vasseur"),
]


def update(n: int, published: datetime | None = datetime(2026, 9, 30, 18, tzinfo=UTC)) -> Update:
    return Update(
        headline=f"Synthetic headline {n}",
        url=f"https://example.com/story-{n}",
        source="Example News",
        what_changed_ru=f"Синтетика: изменение {n}.",
        published_at=published,
    )


def seed(
    store: StoryStore,
    slug: str = "bearman-ferrari-2028",
    title: str = "Бирман в Ferrari в 2028?",
    entities: list[Entity] | None = None,
    status: StoryStatus = StoryStatus.RUMOR,
    n: int = 1,
):
    return store.create(
        slug,
        title,
        "Синтетика: краткое описание.",
        status,
        BEARMAN_FERRARI if entities is None else entities,
        update(n),
        NOW - timedelta(days=10),
    )


def message(text: str, chat: str = CHAT, update_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": int(chat), "type": "private"},
            "date": 0,
            "text": text,
        },
    }


def press(data: str, chat: str = CHAT, update_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": int(chat), "is_bot": False, "first_name": "Test"},
            "message": {"message_id": 5, "chat": {"id": int(chat), "type": "private"}, "date": 0},
            "chat_instance": "1",
            "data": data,
        },
    }


class Favourites:
    """Records what the bot asks to add, and keeps the result like the config would."""

    def __init__(self, favourites: config.Favourites = config.DEFAULT_FAVOURITES) -> None:
        self.current = favourites
        self.calls: list[tuple[list[str], list[str]]] = []

    def __call__(self, teams: Any, drivers: Any) -> config.Favourites:
        self.calls.append(([t.id for t in teams], [d.id for d in drivers]))
        self.current = config.Favourites(
            self.current.teams + tuple(teams), self.current.drivers + tuple(drivers)
        )
        return self.current


@pytest.fixture
def store(tmp_path: Path):
    with StoryStore(tmp_path / "state.db") as opened:
        yield opened


@pytest.fixture
def favourites() -> Favourites:
    return Favourites()


@pytest.fixture
def handler(store: StoryStore, favourites: Favourites) -> Bot:
    return Bot(store, CHAT, config.DEFAULT_FAVOURITES, favourites)


def replies(actions: list[bot.Action]) -> list[Reply]:
    return [a for a in actions if isinstance(a, Reply)]


def text_of(actions: list[bot.Action]) -> str:
    return "\n".join(r.text for r in replies(actions))


def buttons_of(actions: list[bot.Action]) -> list[Button]:
    return [b for r in replies(actions) for row in r.buttons for b in row]


# --- parsing and the chat check -------------------------------------------------------------


def test_commands_are_parsed_without_the_bot_username() -> None:
    parsed = bot.parse_update(message("/Story@test_bot  #3 extra"))
    assert parsed == bot.Command(CHAT, "story", ("#3", "extra"))


def test_a_button_press_is_parsed_with_its_chat() -> None:
    assert bot.parse_update(press("follow:2")) == bot.Press(CHAT, "cb1", "follow:2")


@pytest.mark.parametrize(
    "raw",
    [
        {"update_id": 1, "edited_message": message("/stories")["message"]},
        {"update_id": 1, "channel_post": message("/stories")["message"]},
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 1}, "photo": []}},
        {"update_id": 1, "message": {"message_id": 1, "text": "/stories"}},
        {"update_id": 1, "callback_query": {"id": "x", "data": "follow:1"}},
        {"update_id": 1},
    ],
)
def test_other_updates_are_not_parsed(raw: dict[str, Any]) -> None:
    assert bot.parse_update(raw) is None


def test_updates_from_another_chat_get_no_answer_and_change_nothing(
    store: StoryStore, handler: Bot
) -> None:
    story = seed(store)
    store.follow(story.id, NOW)

    assert handler.handle(message(f"/unfollow {story.id}", chat=OTHER_CHAT), NOW) == []
    assert handler.handle(press(f"unfollow:{story.id}", chat=OTHER_CHAT), NOW) == []
    assert handler.handle(message("/stories", chat=OTHER_CHAT), NOW) == []
    assert [s.id for s in store.followed()] == [story.id]


def test_plain_text_and_start_get_the_help(handler: Bot) -> None:
    for text in ("привет", "/start", "/help"):
        assert "/stories" in text_of(handler.handle(message(text), NOW))


def test_an_unknown_command_is_named_with_the_help(handler: Bot) -> None:
    text = text_of(handler.handle(message("/subscribe 1"), NOW))
    assert "Не знаю команду /subscribe" in text
    assert "/follow <номер>" in text


# --- commands -------------------------------------------------------------------------------


def test_stories_lists_followed_and_recent_open_stories_with_buttons(
    store: StoryStore, handler: Bot
) -> None:
    followed = seed(store)
    other = seed(store, "russell-mercedes-2027", "Рассел продлит контракт?", [], n=2)
    seed(store, "old-story", "Старый сюжет", [], StoryStatus.RESOLVED, n=3)
    store.follow(followed.id, NOW)
    store.mute(followed.id, datetime(2026, 10, 22, 12, tzinfo=UTC))

    actions = handler.handle(message("/stories"), NOW)

    text = text_of(actions)
    assert text.splitlines()[:2] == [
        "Ты следишь:",
        f"#{followed.id} Бирман в Ferrari в 2028? — слух, 30.09.2026 "
        "(без уведомлений до 22.10.2026)",
    ]
    assert "Недавние сюжеты:" in text
    assert f"#{other.id} Рассел продлит контракт?" in text
    assert "Старый сюжет" not in text  # resolved: only in /archive
    assert buttons_of(actions) == [
        Button(f"Отписаться #{followed.id}", f"unfollow:{followed.id}"),
        Button(f"Следить #{other.id}", f"follow:{other.id}"),
    ]


def test_stories_without_any(handler: Bot) -> None:
    assert "Сюжетов пока нет" in text_of(handler.handle(message("/stories"), NOW))


def test_stories_says_when_nothing_is_followed(store: StoryStore, handler: Bot) -> None:
    seed(store)
    text = text_of(handler.handle(message("/stories"), NOW))
    assert text.startswith("Ты пока не следишь ни за одним сюжетом.")


def test_story_shows_the_timeline_with_sources(store: StoryStore, handler: Bot) -> None:
    story = seed(store)
    store.add_update(story.id, update(2, datetime(2026, 10, 12, 8, tzinfo=UTC)), NOW)
    store.add_update(story.id, update(3, published=None), NOW)

    actions = handler.handle(message(f"/story {story.id}"), NOW)

    text = text_of(actions)
    assert text.startswith(f"#{story.id} Бирман в Ferrari в 2028?\nСтатус: слух · не следишь\n")
    assert (
        "30.09.2026 — Synthetic headline 1 (Example News)\n"
        "Синтетика: изменение 1.\nСтатус → слух\nhttps://example.com/story-1"
    ) in text
    assert "12.10.2026 — Synthetic headline 2 (Example News)" in text
    # An undated article is dated by when it was recorded.
    assert "19.10.2026 — Synthetic headline 3 (Example News)" in text
    assert text.index("story-1") < text.index("story-2") < text.index("story-3")
    assert buttons_of(actions) == [Button("Следить", f"follow:{story.id}")]


@pytest.mark.parametrize("command", ["/story", "/follow", "/unfollow", "/mute"])
def test_a_story_command_without_a_number_explains_itself(handler: Bot, command: str) -> None:
    assert f"например: {command} 3" in text_of(handler.handle(message(command), NOW))


@pytest.mark.parametrize("arg", ["99", "abc", "-1", "1.5", "99999999999999"])
def test_a_story_command_with_an_unknown_story(store: StoryStore, handler: Bot, arg: str) -> None:
    seed(store)
    assert f"Нет сюжета {arg}" in text_of(handler.handle(message(f"/story {arg}"), NOW))


def test_follow_subscribes_and_offers_the_entities_that_are_not_favourites(
    store: StoryStore, handler: Bot, favourites: Favourites
) -> None:
    story = seed(store)

    actions = handler.handle(message(f"/follow #{story.id}"), NOW)

    assert (
        text_of(actions) == f"Теперь ты следишь за сюжетом #{story.id} «Бирман в Ferrari в 2028?»."
    )
    assert store.subscription(story.id).active
    # Bearman and Haas are favourites already, and a person is never one.
    assert buttons_of(actions) == [
        Button("Отписаться", f"unfollow:{story.id}"),
        Button("Следить за Ferrari, Hamilton", f"favs:{story.id}"),
    ]
    assert favourites.calls == []  # only offered


def test_following_twice_says_so(store: StoryStore, handler: Bot) -> None:
    story = seed(store)
    handler.handle(message(f"/follow {story.id}"), NOW)
    assert "Ты уже следишь" in text_of(handler.handle(message(f"/follow {story.id}"), NOW))


def test_the_unfollow_button_unsubscribes_in_one_tap(store: StoryStore, handler: Bot) -> None:
    story = seed(store)
    store.follow(story.id, NOW)

    actions = handler.handle(press(f"unfollow:{story.id}"), NOW)

    assert actions[0] == Answer("cb1", "Отписка")
    assert "Ты больше не следишь за сюжетом" in text_of(actions)
    assert f"/story {story.id}" in text_of(actions)
    subscription = store.subscription(story.id)
    assert not subscription.active
    assert subscription.unfollowed_at == NOW
    assert store.get(story.id) is not None  # kept in the archive
    assert buttons_of(actions) == [Button("Следить", f"follow:{story.id}")]


def test_unfollow_command_when_not_followed(store: StoryStore, handler: Bot) -> None:
    story = seed(store)
    assert "Ты не следишь" in text_of(handler.handle(message(f"/unfollow {story.id}"), NOW))


def test_the_follow_button(store: StoryStore, handler: Bot) -> None:
    story = seed(store)
    actions = handler.handle(press(f"follow:{story.id}"), NOW)
    assert actions[0] == Answer("cb1", "Следишь")
    assert store.subscription(story.id).active


@pytest.mark.parametrize("data", ["follow:99", "follow:", "delete:1", "", "favs:x"])
def test_a_stale_or_unknown_button(store: StoryStore, handler: Bot, data: str) -> None:
    seed(store)
    assert handler.handle(press(data), NOW) == [Answer("cb1", "Этой кнопки больше нет")]


def test_mute_a_followed_story(store: StoryStore, handler: Bot) -> None:
    story = seed(store)
    store.follow(story.id, NOW)

    actions = handler.handle(message(f"/mute {story.id} 3"), NOW)

    # 09:00 UTC is 11:00 in Warsaw (CEST) on 22 October.
    assert "без уведомлений до 22.10.2026 11:00" in text_of(actions)
    assert store.subscription(story.id).muted_until == NOW + timedelta(days=3)


@pytest.mark.parametrize("days", ["", "0", "366", "two", "-2"])
def test_mute_needs_a_number_of_days(store: StoryStore, handler: Bot, days: str) -> None:
    story = seed(store)
    store.follow(story.id, NOW)
    text = text_of(handler.handle(message(f"/mute {story.id} {days}"), NOW))
    assert "Укажи число дней от 1 до 365" in text
    assert store.subscription(story.id).muted_until is None


def test_mute_a_story_not_followed(store: StoryStore, handler: Bot) -> None:
    story = seed(store)
    text = text_of(handler.handle(message(f"/mute {story.id} 3"), NOW))
    assert f"Сначала: /follow {story.id}" in text


def test_archive_lists_every_story_including_resolved(store: StoryStore, handler: Bot) -> None:
    open_story = seed(store)
    resolved = seed(store, "old-story", "Старый сюжет", [], StoryStatus.RESOLVED, n=2)
    store.follow(open_story.id, NOW)

    text = text_of(handler.handle(message("/archive"), NOW))

    assert text.startswith("Все сюжеты (2):")
    assert f"#{open_story.id} Бирман в Ferrari в 2028? — слух, 30.09.2026 — следишь" in text
    assert f"#{resolved.id} Старый сюжет — завершён" in text


def test_an_empty_archive(handler: Bot) -> None:
    actions = handler.handle(message("/archive"), NOW)
    assert "Архив пуст" in text_of(actions)
    assert not [a for a in actions if isinstance(a, bot.Document)]


def test_archive_sends_the_page_after_the_list(store: StoryStore, handler: Bot) -> None:
    story = seed(store)
    store.follow(story.id, NOW)

    actions = handler.handle(message("/archive"), NOW)

    assert isinstance(actions[-1], bot.Document)
    assert all(isinstance(a, Reply) for a in actions[:-1])
    page = actions[-1]
    assert page.filename == "f1-archive-2026-10-19.html"
    assert page.mime_type == "text/html"
    assert page.caption == bot.ARCHIVE_CAPTION
    html = page.content.decode("utf-8")
    assert '<article class="story followed" id="story-1">' in html
    assert "https://example.com/story-1" in html


# --- following a story's entities -------------------------------------------------------------


def test_the_entities_button_adds_the_new_favourites(
    store: StoryStore, handler: Bot, favourites: Favourites
) -> None:
    story = seed(store)
    store.follow(story.id, NOW)

    actions = handler.handle(press(f"favs:{story.id}"), NOW)

    assert favourites.calls == [(["ferrari"], ["hamilton"])]
    assert actions[0] == Answer("cb1", "Добавлено")
    assert "Добавил в избранное: Ferrari, Hamilton." in text_of(actions)
    # Nothing left to offer.
    assert buttons_of(actions) == [Button("Отписаться", f"unfollow:{story.id}")]
    assert handler.handle(press(f"favs:{story.id}", update_id=2), NOW) == [
        Answer("cb2", "Уже в избранном")
    ]


def test_a_config_error_is_shown_and_changes_nothing(store: StoryStore, handler: Bot) -> None:
    def broken(teams: Any, drivers: Any) -> config.Favourites:
        raise config.ConfigError("config.toml: not valid TOML: line 3")

    handler.add_favourites = broken
    story = seed(store)

    actions = handler.handle(press(f"favs:{story.id}"), NOW)

    assert actions[0] == Answer("cb1", "Не получилось")
    assert "config.toml: not valid TOML" in text_of(actions)
    assert handler.favourites == config.DEFAULT_FAVOURITES


@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        (
            Entity(EntityKind.DRIVER, "max_verstappen"),
            Favourite("max_verstappen", "Max Verstappen", ("Verstappen",)),
        ),
        (Entity(EntityKind.DRIVER, "hamilton"), Favourite("hamilton", "Hamilton", ("Hamilton",))),
        (Entity(EntityKind.TEAM, "red_bull"), Favourite("red_bull", "Red Bull")),
        (Entity(EntityKind.PERSON, "fred_vasseur"), None),
    ],
)
def test_favourite_for(entity: Entity, expected: Favourite | None) -> None:
    assert bot.favourite_for(entity) == expected


# --- config.add_favourites ----------------------------------------------------------------------


def test_add_favourites_appends_to_the_config_and_keeps_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    shutil.copy(Path(__file__).parents[1] / "config.toml", path)
    before = path.read_text(encoding="utf-8")

    result = config.add_favourites(
        path,
        teams=[Favourite("ferrari", "Ferrari"), Favourite("haas", "Haas")],
        drivers=[Favourite("max_verstappen", "Max Verstappen", ("Verstappen",))],
    )

    after = path.read_text(encoding="utf-8")
    assert after.startswith(before.rstrip("\n"))  # comments and entries kept
    assert after.endswith(
        '[[favourites.teams]]\nid = "ferrari"\nname = "Ferrari"\n\n'
        '[[favourites.drivers]]\nid = "max_verstappen"\nname = "Max Verstappen"\n'
        'aliases = ["Verstappen"]\n'
    )
    assert config.load(path) == result
    assert result.favourites.team_ids == ("mclaren", "haas", "ferrari")
    assert result.favourites.driver_ids == ("russell", "ocon", "bearman", "max_verstappen")
    assert not (tmp_path / ".config.toml.tmp").exists()


def test_add_favourites_writes_out_a_default_list_the_file_leaves_out(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[digest]\nlanguage = "ru"\n', encoding="utf-8")

    result = config.add_favourites(path, drivers=[Favourite("hamilton", "Hamilton")])

    assert result.favourites.driver_ids == ("russell", "ocon", "bearman", "hamilton")
    assert result.favourites.teams == config.DEFAULT_FAVOURITES.teams
    assert config.load(path).favourites.drivers[1].track_future


def test_add_favourites_creates_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    result = config.add_favourites(path, teams=[Favourite("ferrari", "Ferrari")])
    assert config.load(path) == result
    assert result.favourites.team_ids == ("mclaren", "haas", "ferrari")


def test_add_favourites_with_nothing_new_leaves_the_file_alone(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("# only a comment\n", encoding="utf-8")
    config.add_favourites(path, teams=[Favourite("haas", "Haas")])
    assert path.read_text(encoding="utf-8") == "# only a comment\n"


def test_add_favourites_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    result = config.add_favourites(path, teams=[Favourite("ferrari", "Ferrari")], write=False)
    assert "ferrari" in result.favourites.team_ids
    assert not path.exists()


def test_add_favourites_refuses_a_file_it_cannot_extend(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = '[favourites]\nteams = [{ id = "haas", name = "Haas" }]\n'
    path.write_text(original, encoding="utf-8")
    with pytest.raises(config.ConfigError, match="not valid TOML"):
        config.add_favourites(path, teams=[Favourite("ferrari", "Ferrari")])
    assert path.read_text(encoding="utf-8") == original


def test_add_favourites_refuses_an_invalid_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[digest]\nlanguag = 'ru'\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="unknown key"):
        config.add_favourites(path, teams=[Favourite("ferrari", "Ferrari")])


def test_names_are_escaped_for_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    odd = Favourite("odd", 'Team "Quote" \\ Back')
    assert odd in config.add_favourites(path, teams=[odd]).favourites.teams
    assert odd in config.load(path).favourites.teams


# --- long messages --------------------------------------------------------------------------------


def test_long_texts_are_split_at_line_ends() -> None:
    text = "\n".join(f"строка {i:04d} " + "x" * 40 for i in range(300))
    parts = bot.chunks(text, limit=1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "\n".join(parts) == text


def test_a_line_longer_than_the_limit_is_cut() -> None:
    assert bot.chunks("a" * 25, limit=10) == ["a" * 10, "a" * 10, "a" * 5]


def test_a_long_archive_keeps_the_buttons_on_the_last_message(
    store: StoryStore, handler: Bot
) -> None:
    for n in range(80):
        story = seed(store, f"story-{n}", f"Синтетический сюжет {n} " + "я" * 60, [], n=n)
        store.follow(story.id, NOW)
    parts = replies(handler.handle(message("/stories"), NOW))
    assert len(parts) > 1
    assert all(len(p.text) <= bot.MAX_LENGTH for p in parts)
    assert all(not p.buttons for p in parts[:-1])
    assert len(parts[-1].buttons) == 80


# --- the Bot API and the listener ---------------------------------------------------------------


class MockTelegram:
    """A mock Bot API: getUpdates answers come from `polls` in turn (then an empty list);
    every request is kept."""

    def __init__(self, polls: list[Any] | None = None) -> None:
        self.polls = list(polls or [])
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if request.headers["content-type"].startswith("multipart/form-data"):
            body = {"multipart": request.read()}
        else:
            body = json.loads(request.content)
        self.requests.append((method, body))
        if method == "getUpdates":
            answer = self.polls.pop(0) if self.polls else []
            if isinstance(answer, httpx.Response):
                return answer
            if isinstance(answer, Exception):
                raise answer
            return httpx.Response(200, json={"ok": True, "result": answer})
        return httpx.Response(200, json={"ok": True, "result": True})

    def sent(self, method: str) -> list[dict[str, Any]]:
        return [body for m, body in self.requests if m == method]


def api(mock: MockTelegram) -> bot.BotApi:
    client = httpx.Client(transport=httpx.MockTransport(mock))
    return bot.BotApi(ts.Credentials(TOKEN, CHAT), client)


def test_replies_carry_the_inline_keyboard(store: StoryStore, handler: Bot) -> None:
    story = seed(store)
    mock = MockTelegram([[message(f"/follow {story.id}", update_id=7)]])

    bot.listen(api(mock), handler, clock=lambda: NOW, sleep=lambda s: None, polls=1)

    [sent] = mock.sent("sendMessage")
    assert sent["chat_id"] == CHAT
    assert "parse_mode" not in sent
    assert sent["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Отписаться", "callback_data": f"unfollow:{story.id}"}],
            [{"text": "Следить за Ferrari, Hamilton", "callback_data": f"favs:{story.id}"}],
        ]
    }
    assert mock.sent("getUpdates")[0]["allowed_updates"] == ["message", "callback_query"]


def test_the_archive_page_goes_as_a_document(store: StoryStore, handler: Bot) -> None:
    seed(store)
    mock = MockTelegram([[message("/archive", update_id=7)]])

    bot.listen(api(mock), handler, clock=lambda: NOW, sleep=lambda s: None, polls=1)

    assert [m for m, _ in mock.requests] == ["getUpdates", "sendMessage", "sendDocument"]
    [sent] = mock.sent("sendDocument")
    body = sent["multipart"]
    assert b'name="chat_id"\r\n\r\n424242\r\n' in body
    assert bot.ARCHIVE_CAPTION.encode("utf-8") in body
    assert b'name="document"; filename="f1-archive-2026-10-19.html"' in body
    assert b"Content-Type: text/html" in body
    assert b"<!doctype html>" in body
    assert TOKEN.encode() not in body


def test_a_refused_document_is_reported_without_the_token(store: StoryStore) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: file empty"})

    client = httpx.Client(transport=httpx.MockTransport(refuse))
    telegram = bot.BotApi(ts.Credentials(TOKEN, CHAT), client)
    with pytest.raises(bot.BotError) as raised:
        telegram.send(bot.Document("a.html", b"x", "text/html", "c"))
    assert str(raised.value) == "sendDocument: Bad Request: file empty"
    assert raised.value.status == 400


def test_the_listener_confirms_updates_with_the_next_offset(
    store: StoryStore, handler: Bot
) -> None:
    story = seed(store)
    mock = MockTelegram(
        [
            [message("/stories", update_id=10), message("/archive", chat=OTHER_CHAT, update_id=11)],
            [press(f"unfollow:{story.id}", update_id=12)],
            [],
        ]
    )

    bot.listen(api(mock), handler, clock=lambda: NOW, sleep=lambda s: None, polls=3)

    offsets = [body.get("offset") for body in mock.sent("getUpdates")]
    assert offsets == [None, 12, 13]
    assert mock.sent("answerCallbackQuery") == [{"callback_query_id": "cb12", "text": "Отписка"}]
    # /stories and the unfollow reply; nothing for the other chat.
    assert len(mock.sent("sendMessage")) == 2


def test_a_failed_poll_is_retried_after_a_growing_pause(store: StoryStore, handler: Bot) -> None:
    waits: list[float] = []
    warnings: list[str] = []
    mock = MockTelegram(
        [
            httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{TOKEN}/getUpdates"),
            httpx.Response(502, text="Bad Gateway"),
            httpx.Response(
                429,
                json={
                    "ok": False,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 7},
                },
            ),
            [message("/help", update_id=1)],
        ]
    )

    bot.listen(api(mock), handler, sleep=waits.append, warn=warnings.append, polls=4)

    assert waits == [5.0, 10.0, 7.0]
    assert len(mock.sent("sendMessage")) == 1
    assert all(TOKEN not in w for w in warnings)
    assert "<redacted>" in warnings[0]


@pytest.mark.parametrize("status", [401, 404, 409])
def test_polling_that_cannot_work_stops_the_listener(
    store: StoryStore, handler: Bot, status: int
) -> None:
    body = {"ok": False, "error_code": status, "description": f"Conflict for bot{TOKEN}"}
    mock = MockTelegram([httpx.Response(status, json=body)])
    with pytest.raises(bot.BotError) as raised:
        bot.listen(api(mock), handler, sleep=lambda s: None, polls=3)
    assert raised.value.status == status
    assert TOKEN not in str(raised.value)


def test_a_reply_that_fails_is_reported_and_the_listener_goes_on(
    store: StoryStore, handler: Bot
) -> None:
    warnings: list[str] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("sendMessage"):
            return httpx.Response(
                403, json={"ok": False, "description": "Forbidden: bot was blocked"}
            )
        return httpx.Response(200, json={"ok": True, "result": [message("/help", update_id=3)]})

    client = httpx.Client(transport=httpx.MockTransport(telegram))
    bot.listen(
        bot.BotApi(ts.Credentials(TOKEN, CHAT), client),
        handler,
        warn=warnings.append,
        sleep=lambda s: None,
        polls=2,
    )
    assert warnings == ["reply not sent: sendMessage: Forbidden: bot was blocked"] * 2


def test_an_update_the_store_fails_on_is_skipped(handler: Bot) -> None:
    warnings: list[str] = []
    handler.store.close()  # every query now fails

    offset = bot.handle_all(
        handler, [message("/stories", update_id=4), {"update_id": "x"}], NOW, print, warnings.append
    )

    assert offset == 5
    assert len(warnings) == 1
    assert warnings[0].startswith("update not handled:")


class TelegramQueue:
    """A mock Bot API that keeps updates the way Telegram does: each poll gets the pending
    updates from its offset on, and an offset forgets the updates before it."""

    def __init__(self, pending: list[dict[str, Any]]) -> None:
        self.pending = list(pending)
        self.offsets: list[int | None] = []
        self.messages = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("getUpdates"):
            offset = body.get("offset")
            self.offsets.append(offset)
            if offset is not None:
                self.pending = [u for u in self.pending if u["update_id"] >= offset]
            return httpx.Response(200, json={"ok": True, "result": self.pending})
        self.messages += request.url.path.endswith("sendMessage")
        return httpx.Response(200, json={"ok": True, "result": True})


def test_a_restarted_listener_does_not_answer_an_update_again(
    tmp_path: Path, store: StoryStore, handler: Bot
) -> None:
    telegram = TelegramQueue([message("/stories", update_id=20)])
    client = httpx.Client(transport=httpx.MockTransport(telegram))
    with bot.OffsetStore(tmp_path / "state.db") as offsets:
        # Handles the update, then stops before a later poll could confirm it.
        bot.listen(
            bot.BotApi(ts.Credentials(TOKEN, CHAT), client), handler, polls=1, offsets=offsets
        )
    with bot.OffsetStore(tmp_path / "state.db") as offsets:
        bot.listen(
            bot.BotApi(ts.Credentials(TOKEN, CHAT), client), handler, polls=1, offsets=offsets
        )

    assert telegram.offsets == [None, 21]
    assert telegram.messages == 1


def test_the_offset_is_stored_before_the_batch_is_handled(
    tmp_path: Path, store: StoryStore, handler: Bot
) -> None:
    stored: list[int | None] = []
    offsets = bot.OffsetStore(tmp_path / "state.db")

    def deliver_and_check(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getUpdates"):
            result = [message("/help", update_id=5), message("/help", update_id=6)]
            return httpx.Response(200, json={"ok": True, "result": result})
        stored.append(offsets.load())
        return httpx.Response(200, json={"ok": True, "result": True})

    client = httpx.Client(transport=httpx.MockTransport(deliver_and_check))
    bot.listen(bot.BotApi(ts.Credentials(TOKEN, CHAT), client), handler, polls=1, offsets=offsets)
    offsets.close()

    assert stored == [7, 7]


def test_an_offset_that_cannot_be_stored_is_reported_and_the_listener_goes_on(
    tmp_path: Path, store: StoryStore, handler: Bot
) -> None:
    warnings: list[str] = []
    offsets = bot.OffsetStore(tmp_path / "state.db")

    def broken_save(offset: int) -> None:
        raise StateError("state.db: disk I/O error")

    offsets.save = broken_save  # type: ignore[method-assign]
    mock = MockTelegram([[message("/help", update_id=8)], []])

    bot.listen(api(mock), handler, warn=warnings.append, polls=2, offsets=offsets)

    assert [body.get("offset") for body in mock.sent("getUpdates")] == [None, 9]
    assert len(mock.sent("sendMessage")) == 1
    assert warnings == ["offset not stored: state.db: disk I/O error"]
    offsets.close()


def test_the_offset_store_keeps_the_last_offset(tmp_path: Path) -> None:
    with bot.OffsetStore(tmp_path / "state.db") as offsets:
        assert offsets.load() is None
        offsets.save(3)
        offsets.save(41)
    with bot.OffsetStore(tmp_path / "state.db") as offsets:
        assert offsets.load() == 41


# --- the command line -----------------------------------------------------------------------------


@pytest.fixture
def cli_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ts.TOKEN_VAR, raising=False)
    monkeypatch.delenv(ts.CHAT_ID_VAR, raising=False)
    shutil.copy(Path(__file__).parents[1] / "config.toml", tmp_path / "config.toml")
    with StoryStore(tmp_path / "state.db") as opened:
        story = seed(opened)
        assert story.id == 1
    return tmp_path


def test_dry_run_answers_the_saved_updates_and_keeps_nothing(
    cli_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_before = (cli_dir / "config.toml").read_text(encoding="utf-8")

    code = main(["bot", "--dry-run", "--updates", str(UPDATES), "--chat-id", CHAT])

    out, err = capsys.readouterr()
    assert code == 0
    assert "Ты пока не следишь ни за одним сюжетом." in out  # /stories
    assert "[button answer] Следишь" in out  # follow:1
    assert "[Отписаться]\n[Следить за Ferrari, Hamilton]" in out
    assert "Добавил в избранное: Ferrari, Hamilton." in out  # favs:1
    assert "Статус: слух · ты следишь" in out  # /story@test_bot 1
    assert "Ты больше не следишь" not in out  # /unfollow from another chat: ignored
    assert "would add Ferrari, Hamilton" in err
    assert "6 update(s), nothing sent or stored" in err
    # Nothing kept: no subscription, config untouched.
    with StoryStore(cli_dir / "state.db") as opened:
        assert opened.subscription(1) is None
    assert (cli_dir / "config.toml").read_text(encoding="utf-8") == config_before


def test_dry_run_shows_the_archive_page_as_a_file(
    cli_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (cli_dir / "archive.json").write_text(json.dumps([message("/archive")]), encoding="utf-8")

    assert main(["bot", "--dry-run", "--updates", "archive.json", "--chat-id", CHAT]) == 0

    out = capsys.readouterr().out
    assert "Все сюжеты (1):" in out
    assert re.search(r"\[file f1-archive-\d{4}-\d{2}-\d{2}\.html, \d+ bytes\] Архив страницей", out)
    assert not list(cli_dir.glob("*.html"))  # sent as a file, nothing written locally


def test_dry_run_takes_the_chat_from_the_environment(
    cli_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ts.CHAT_ID_VAR, OTHER_CHAT)
    assert main(["bot", "--dry-run", "--updates", str(UPDATES), "--env-file", "missing"]) == 0
    # Only the stranger's /unfollow is from that chat; story 1 is not followed.
    assert capsys.readouterr().out.strip() == "Ты не следишь за сюжетом #1.\n[Следить]"


def test_dry_run_without_a_chat_id(cli_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["bot", "--dry-run", "--updates", str(UPDATES), "--env-file", "missing"]) == 1
    assert f"{ts.CHAT_ID_VAR} not set" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [["--dry-run"], ["--updates", "x.json"], ["--chat-id", CHAT]],
)
def test_options_that_go_together(
    cli_dir: Path, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    assert main(["bot", *args]) == 2
    assert "error:" in capsys.readouterr().err


def test_dry_run_with_a_bad_updates_file(cli_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (cli_dir / "bad.json").write_text("{not json", encoding="utf-8")
    (cli_dir / "dict.json").write_text('{"ok": true}', encoding="utf-8")
    assert main(["bot", "--dry-run", "--updates", "bad.json", "--chat-id", CHAT]) == 1
    assert "not JSON" in capsys.readouterr().err
    assert main(["bot", "--dry-run", "--updates", "dict.json", "--chat-id", CHAT]) == 1
    assert "must hold a list" in capsys.readouterr().err


def test_the_listener_needs_the_telegram_settings(
    cli_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["bot", "--env-file", "missing"]) == 1
    assert "not set" in capsys.readouterr().err


def test_the_listener_runs_with_the_configured_chat_and_favourites(
    cli_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ts.TOKEN_VAR, TOKEN)
    monkeypatch.setenv(ts.CHAT_ID_VAR, CHAT)
    seen: dict[str, Any] = {}

    def fake_listen(telegram: bot.BotApi, handler: Bot, offsets: bot.OffsetStore) -> None:
        seen["offsets"] = offsets.load()
        seen["chat"] = handler.chat_id
        seen["teams"] = handler.favourites.team_ids
        # The entities button writes the real config file outside a dry run.
        handler.store.follow(1, NOW)
        handler.handle(press("favs:1"), NOW)
        raise KeyboardInterrupt

    monkeypatch.setattr(bot, "listen", fake_listen)

    assert main(["bot"]) == 0
    assert seen == {"offsets": None, "chat": CHAT, "teams": ("mclaren", "haas")}
    favourites = config.load(cli_dir / "config.toml").favourites
    assert favourites.team_ids == ("mclaren", "haas", "ferrari")
    assert "hamilton" in favourites.driver_ids


def test_a_fatal_poll_error_ends_the_command(
    cli_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ts.TOKEN_VAR, TOKEN)
    monkeypatch.setenv(ts.CHAT_ID_VAR, CHAT)

    def fake_listen(telegram: bot.BotApi, handler: Bot, offsets: bot.OffsetStore) -> None:
        raise bot.BotError("getUpdates: Conflict: terminated by other getUpdates request", 409)

    monkeypatch.setattr(bot, "listen", fake_listen)
    assert main(["bot"]) == 1
    assert "Conflict" in capsys.readouterr().err


def test_load_chat_id_prefers_the_environment(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"{ts.CHAT_ID_VAR}=111\n", encoding="utf-8")
    assert ts.load_chat_id({}, env_file) == "111"
    assert ts.load_chat_id({ts.CHAT_ID_VAR: "222"}, env_file) == "222"

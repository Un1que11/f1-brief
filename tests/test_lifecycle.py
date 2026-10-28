"""Story lifecycle (F26) on synthetic stories (example.com, no real outlet or text): statuses
that move only forward with a sourced update, and one offer to unfollow a followed story with
no news for 60 days, with an injected clock and a mock Bot API: nothing reaches Telegram."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_bot import message, press, text_of
from test_story_detection import OCON_SEAT, OCON_TALKS, attach, new

from f1brief import cli, digest, lifecycle
from f1brief import story_detection as sd
from f1brief import telegram_send as ts
from f1brief.bot import Bot, BotError, Reply
from f1brief.bot_texts import texts_for
from f1brief.cli import main
from f1brief.config import DEFAULT_FAVOURITES
from f1brief.stories import (
    _MIGRATIONS,
    SCHEMA_VERSION,
    Entity,
    EntityKind,
    Story,
    StoryError,
    StoryStatus,
    StoryStore,
    Update,
    can_move,
)

CHAT = "424242"
TOKEN = "123456:TEST-token_abcDEF"
# Monday 2026-10-19, 11:00 in Warsaw (CEST).
MORNING = datetime(2026, 10, 19, 9, tzinfo=UTC)
# The story's only update was published 60 days before MORNING.
LAST = MORNING - timedelta(days=60)

R, D, C, N, X = (
    StoryStatus.RUMOR,
    StoryStatus.DEVELOPING,
    StoryStatus.CONFIRMED,
    StoryStatus.DENIED,
    StoryStatus.RESOLVED,
)


def update(n: int, published: datetime | None = LAST) -> Update:
    return Update(
        headline=f"Synthetic headline {n}",
        url=f"https://example.com/story-{n}",
        source="Example News",
        what_changed=f"Синтетика: изменение {n}.",
        language="ru",
        published_at=published,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[StoryStore]:
    with StoryStore(tmp_path / "state.db") as opened:
        yield opened


def story(
    store: StoryStore,
    n: int = 1,
    status: StoryStatus = R,
    followed: datetime | None = LAST - timedelta(days=10),
) -> Story:
    """A story whose first update (100*n) was published at LAST, followed at `followed`."""
    created = store.create(
        f"story-{n}",
        f"Сюжет {n}",
        "Синтетика: описание.",
        status,
        [Entity(EntityKind.DRIVER, "bearman")],
        update(100 * n),
        LAST,
        language="ru",
    )
    if followed is not None:
        store.follow(created.id, followed)
    return created


# --- statuses move forward only -------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "new", "allowed"),
    [
        (R, D, True),
        (R, C, True),
        (R, N, True),
        (D, C, True),
        (D, N, True),
        (C, X, True),
        (N, X, True),
        (D, R, False),
        (C, R, False),
        (C, D, False),
        (C, N, False),
        (N, C, False),
        (R, X, False),
        (D, X, False),
        (X, R, False),
        (X, C, False),
    ],
)
def test_a_status_moves_only_forward(current: StoryStatus, new: StoryStatus, allowed: bool) -> None:
    assert can_move(current, new) is allowed


def test_a_move_back_is_refused_and_stores_nothing(store: StoryStore) -> None:
    confirmed = story(store, status=C)
    with pytest.raises(StoryError, match="cannot move from confirmed to rumor"):
        store.add_update(confirmed.id, update(2), MORNING, status=R)
    assert store.get(confirmed.id) == confirmed
    assert len(store.timeline(confirmed.id)) == 1


def test_a_rumour_is_not_resolved_before_its_outcome_is_announced(store: StoryStore) -> None:
    rumour = story(store)
    with pytest.raises(StoryError, match="rumor to resolved"):
        store.add_update(rumour.id, update(2), MORNING, status=X)
    assert store.add_update(rumour.id, update(2), MORNING, status=C)
    assert store.add_update(rumour.id, update(3), MORNING, status=X)
    assert store.get(rumour.id).status is X


def test_each_update_records_the_status_it_set(store: StoryStore) -> None:
    rumour = story(store)
    store.add_update(rumour.id, update(2), MORNING)
    store.add_update(rumour.id, update(3), MORNING, status=R)  # its own status: no change
    store.add_update(rumour.id, update(4), MORNING, status=D)
    statuses = [u.status for u in store.timeline(rumour.id)]
    assert statuses == [R, None, None, D]


def test_a_state_file_from_f24_gains_update_statuses_and_offers(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE schema_versions (component TEXT NOT NULL PRIMARY KEY, "
            "version INTEGER NOT NULL)"
        )
        for step in _MIGRATIONS[:3]:
            for statement in step:
                db.execute(statement)
        db.execute("INSERT INTO schema_versions VALUES ('stories', 3)")
        db.execute(
            "INSERT INTO stories (slug, title_ru, summary_ru, status, created_at, "
            "last_update_at) VALUES ('kept', 't', 's', 'developing', ?, ?)",
            (LAST.isoformat(), LAST.isoformat()),
        )
        db.execute(
            "INSERT INTO story_updates (story_id, headline, url, source, what_changed_ru, "
            "recorded_at) VALUES (1, 'h', 'https://example.com/kept', 's', 'w', ?)",
            (LAST.isoformat(),),
        )
    with StoryStore(path) as store:
        [kept] = store.timeline(1)
        assert kept.status is None  # which update set it was not recorded before F26
        store.follow(1, LAST)
        assert [o.story.id for o in lifecycle.plan(store, MORNING).send] == [1]
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT version FROM schema_versions WHERE component = 'stories'"
        ).fetchall() == [(SCHEMA_VERSION,)]


# --- story detection keeps to the lifecycle -------------------------------------------------


def detection_seed(store: StoryStore, status: StoryStatus) -> Story:
    return store.create(
        "ocon-2027",
        "Окон уйдёт после 2026?",
        "Синтетика: описание.",
        status,
        [Entity(EntityKind.DRIVER, "ocon")],
        update(1),
        MORNING,
        language="ru",
    )


def test_an_attachment_that_would_move_back_keeps_its_update_but_not_the_status(
    store: StoryStore,
) -> None:
    detection_seed(store, C)
    result = sd.plan(
        [attach(OCON_SEAT.link, "ocon-2027", status="rumor")], [OCON_SEAT], store.stories()
    )
    assert result.rejected == ()
    [attachment] = result.attachments
    assert attachment.status is None
    [kept] = result.kept_status
    assert (kept.url, kept.reason) == (
        OCON_SEAT.link,
        "ocon-2027 cannot move from confirmed to rumor",
    )
    assert "status not changed for https://example.com/ocon-seat" in sd.format_plan(result, True)

    sd.apply(store, result, MORNING)
    ocon = store.get_by_slug("ocon-2027")
    assert ocon is not None and ocon.status is C
    assert [u.update.url for u in store.timeline(ocon.id)][-1] == OCON_SEAT.link


def test_the_story_s_own_status_is_no_change(store: StoryStore) -> None:
    detection_seed(store, D)
    result = sd.plan(
        [attach(OCON_SEAT.link, "ocon-2027", status="developing")], [OCON_SEAT], store.stories()
    )
    assert (result.attachments[0].status, result.kept_status) == (None, ())


def test_statuses_in_one_answer_are_checked_in_order(store: StoryStore) -> None:
    detection_seed(store, R)
    decisions = [
        attach(OCON_SEAT.link, "ocon-2027", status="confirmed"),
        attach(OCON_TALKS.link, "ocon-2027", status="developing"),
    ]
    result = sd.plan(decisions, [OCON_SEAT, OCON_TALKS], store.stories())
    assert [a.status for a in result.attachments] == [C, None]
    assert [k.url for k in result.kept_status] == [OCON_TALKS.link]
    sd.apply(store, result, MORNING)
    assert store.get_by_slug("ocon-2027").status is C


def test_a_new_story_cannot_start_resolved(store: StoryStore) -> None:
    result = sd.plan([new(OCON_SEAT.link, status="resolved")], [OCON_SEAT], [])
    assert result.new_stories == ()
    assert "cannot start as resolved" in result.rejected[0].reason


def test_the_prompt_says_statuses_only_move_forward() -> None:
    template = digest.template(sd.DETECTION_TEMPLATE, None)
    assert "A status only moves forward" in template
    assert "never start\n   a new story as `resolved`" in template


# --- the offer to unfollow a quiet story ----------------------------------------------------


def offered(store: StoryStore, now: datetime) -> list[int]:
    """Plans and delivers at `now`; the ids of the stories offered."""
    sent: list[Reply] = []
    assert lifecycle.deliver(store, lifecycle.plan(store, now), now, sent.append)
    return [int(r.buttons[0][0].data.split(":")[1]) for r in sent]


def test_no_offer_before_60_quiet_days(store: StoryStore) -> None:
    story(store)
    assert offered(store, MORNING - timedelta(minutes=1)) == []


def test_one_offer_after_60_quiet_days(store: StoryStore) -> None:
    quiet = story(store)
    assert offered(store, MORNING) == [quiet.id]
    assert offered(store, MORNING + timedelta(days=1)) == []
    assert offered(store, MORNING + timedelta(days=90)) == []


def test_a_new_update_starts_a_new_quiet_stretch(store: StoryStore) -> None:
    quiet = story(store)
    assert offered(store, MORNING) == [quiet.id]
    fresh = MORNING + timedelta(days=1)
    store.add_update(quiet.id, update(2, published=fresh), fresh)
    assert offered(store, fresh + timedelta(days=59)) == []
    assert offered(store, fresh + timedelta(days=60)) == [quiet.id]


def test_the_stretch_counts_from_the_follow_when_that_is_later(store: StoryStore) -> None:
    followed_late = story(store, followed=MORNING - timedelta(days=1))
    assert offered(store, MORNING) == []
    assert offered(store, MORNING + timedelta(days=59)) == [followed_late.id]


def test_an_undated_update_counts_from_when_it_was_recorded(store: StoryStore) -> None:
    quiet = story(store)
    recorded = MORNING - timedelta(days=10)
    store.add_update(quiet.id, update(2, published=None), recorded)
    assert offered(store, MORNING) == []
    assert offered(store, recorded + timedelta(days=60)) == [quiet.id]


def test_stories_not_followed_get_no_offer(store: StoryStore) -> None:
    story(store, 1, followed=None)
    unfollowed = story(store, 2)
    store.unfollow(unfollowed.id, MORNING - timedelta(days=1))
    assert offered(store, MORNING) == []


def test_a_muted_story_is_offered_when_the_mute_ends(store: StoryStore) -> None:
    muted = story(store)
    store.mute(muted.id, MORNING + timedelta(days=3))
    assert offered(store, MORNING) == []
    assert offered(store, MORNING + timedelta(days=3)) == [muted.id]


def test_a_resolved_story_is_offered_too(store: StoryStore) -> None:
    done = story(store, status=C)
    store.add_update(done.id, update(2), LAST, status=X)
    assert offered(store, MORNING) == [done.id]


def test_offers_wait_for_the_end_of_the_quiet_hours(store: StoryStore) -> None:
    first, second = story(store, 1), story(store, 2)
    night = MORNING.replace(hour=22)  # 00:00 Warsaw
    planned = lifecycle.plan(store, night)
    assert (planned.send, [o.story.id for o in planned.waiting]) == ((), [first.id, second.id])
    assert (
        planned.describe() == "offers to unfollow quiet stories: 0 to send, 2 waiting (quiet hours)"
    )
    assert offered(store, night) == []
    assert offered(store, MORNING + timedelta(days=1)) == [first.id, second.id]


def test_offers_do_not_count_towards_the_alerts_limit(store: StoryStore) -> None:
    for n in range(1, 5):
        story(store, n)
    assert len(offered(store, MORNING)) == 4
    assert store.alerts_sent(MORNING - timedelta(days=1), MORNING + timedelta(days=1)) == 0


def test_an_offer_telegram_refuses_is_due_again(store: StoryStore) -> None:
    first, second = story(store, 1), story(store, 2)
    warnings: list[str] = []

    def refuse(reply: Reply) -> None:
        raise BotError("sendMessage: HTTP 502", 502)

    planned = lifecycle.plan(store, MORNING)
    assert not lifecycle.deliver(store, planned, MORNING, refuse, warnings.append)
    assert warnings == ["offer to unfollow story #1 not sent: sendMessage: HTTP 502"]
    assert offered(store, MORNING + timedelta(hours=1)) == [first.id, second.id]


@pytest.mark.parametrize(
    ("days", "text"),
    [
        (1, "1 день"),
        (3, "3 дня"),
        (11, "11 дней"),
        (21, "21 день"),
        (60, "60 дней"),
        (62, "62 дня"),
        (112, "112 дней"),
    ],
)
def test_days_in_russian(days: int, text: str) -> None:
    assert texts_for("ru").days(days) == text


def test_the_offer_names_the_story_and_carries_the_unfollow_button(store: StoryStore) -> None:
    quiet = story(store)
    [offer] = lifecycle.plan(store, MORNING + timedelta(days=2)).send
    reply = lifecycle.reply(offer, MORNING + timedelta(days=2))
    assert reply.text.startswith(
        "Сюжет #1 «Сюжет 1» (слух): нет новостей уже 62 дня, последнее обновление — 20.08.2026."
    )
    assert reply.text.endswith(f"Хронология: /story {quiet.id}")
    assert [[(b.text, b.data) for b in row] for row in reply.buttons] == [
        [("Отписаться", f"unfollow:{quiet.id}")]
    ]


def test_pressing_unfollow_on_the_offer_unfollows(store: StoryStore) -> None:
    quiet = story(store)
    [offer] = lifecycle.plan(store, MORNING).send
    [[button]] = lifecycle.reply(offer, MORNING).buttons
    handler = Bot(store, CHAT, DEFAULT_FAVOURITES, lambda teams, drivers: DEFAULT_FAVOURITES)
    handler.handle(press(button.data), MORNING)
    subscription = store.subscription(quiet.id)
    assert subscription is not None and not subscription.active
    assert offered(store, MORNING) == []


def test_the_timeline_shows_which_update_changed_the_status(store: StoryStore) -> None:
    rumour = story(store)
    store.add_update(rumour.id, update(2), MORNING, status=C)
    handler = Bot(store, CHAT, DEFAULT_FAVOURITES, lambda teams, drivers: DEFAULT_FAVOURITES)
    text = text_of(handler.handle(message(f"/story {rumour.id}"), MORNING))
    lines = text.split("\n")
    link = '<a href="{url}">Example News</a>'
    first = lines.index(
        "Синтетика: изменение 100. · " + link.format(url="https://example.com/story-100")
    )
    assert lines[first + 1] == "Статус → слух"
    second = lines.index("Синтетика: изменение 2. · " + link.format(url=update(2).url))
    assert lines[second + 1] == "Статус → подтверждено"


# --- the command line -----------------------------------------------------------------------


class MockTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


@pytest.fixture
def cli_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MockTelegram:
    """A working directory with one followed story quiet for 60 days, the clock at MORNING
    and a mock Bot API."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ts.TOKEN_VAR, raising=False)
    monkeypatch.delenv(ts.CHAT_ID_VAR, raising=False)
    monkeypatch.setattr(cli, "_now", lambda: MORNING)
    telegram = MockTelegram()
    monkeypatch.setattr(cli, "_telegram_client", telegram.client)
    with StoryStore(tmp_path / "state.db") as opened:
        story(opened)
    return telegram


def test_alerts_dry_run_prints_the_offer_and_records_nothing(
    cli_dir: MockTelegram, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["alerts", "--dry-run"]) == 0
    out, err = capsys.readouterr()
    assert "Сюжет #1 «Сюжет 1» (слух): нет новостей уже 60 дней" in out
    assert "[Отписаться]" in out
    assert "offers to unfollow quiet stories: 1 to send" in err
    assert "dry run: no alert sent, nothing recorded" in err
    with StoryStore("state.db") as store:
        assert len(lifecycle.plan(store, MORNING).send) == 1
    assert cli_dir.sent == []


def test_alerts_sends_the_offer_once(
    cli_dir: MockTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ts.TOKEN_VAR, TOKEN)
    monkeypatch.setenv(ts.CHAT_ID_VAR, CHAT)

    assert main(["alerts"]) == 0
    assert main(["alerts"]) == 0

    [sent] = cli_dir.sent
    assert sent["chat_id"] == CHAT
    assert sent["text"].startswith("Сюжет #1 «Сюжет 1» (слух): нет новостей")
    assert sent["reply_markup"] == {
        "inline_keyboard": [[{"text": "Отписаться", "callback_data": "unfollow:1"}]]
    }


def test_alerts_go_before_offers_and_a_refused_alert_holds_the_offers(
    cli_dir: MockTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ts.TOKEN_VAR, TOKEN)
    monkeypatch.setenv(ts.CHAT_ID_VAR, CHAT)
    with StoryStore("state.db") as store:
        alerted = story(store, 2, followed=MORNING - timedelta(days=2))
        store.add_update(alerted.id, update(2, published=MORNING), MORNING - timedelta(hours=1))

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"ok": False, "description": "Bad Gateway"})

    refusing = httpx.MockTransport(refuse)
    monkeypatch.setattr(cli, "_telegram_client", lambda: httpx.Client(transport=refusing))
    assert main(["alerts"]) == 1
    with StoryStore("state.db") as store:
        assert len(lifecycle.plan(store, MORNING).send) == 1

    monkeypatch.setattr(cli, "_telegram_client", cli_dir.client)
    assert main(["alerts"]) == 0
    assert [m["text"].split("\n")[0] for m in cli_dir.sent] == [
        "🔔 <b>Сюжет 2</b>",
        "Сюжет #1 «Сюжет 1» (слух): нет новостей уже 60 дней, последнее обновление — 20.08.2026.",
    ]


def test_run_sends_the_offers_whatever_else_is_due(
    cli_dir: MockTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ts.TOKEN_VAR, TOKEN)
    monkeypatch.setenv(ts.CHAT_ID_VAR, CHAT)
    monkeypatch.setattr(cli, "_run_due", lambda *args: 0)
    assert main(["run"]) == 0
    assert len(cli_dir.sent) == 1

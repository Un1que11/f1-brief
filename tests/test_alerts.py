"""Story alerts (F24) on synthetic stories, with an injected clock (Europe/Warsaw, both DST
changes of 2026) and a mock Bot API: nothing reaches Telegram."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from f1brief import alerts, bot, cli
from f1brief import telegram_send as ts
from f1brief.alerts import Alert
from f1brief.bot import Bot, BotError, Reply
from f1brief.cli import main
from f1brief.config import DEFAULT_FAVOURITES
from f1brief.stories import (
    Entity,
    EntityKind,
    Story,
    StoryStatus,
    StoryStore,
    StoryUpdate,
    Update,
)

CHAT = "424242"
TOKEN = "123456:TEST-token_abcDEF"
# Monday 2026-10-19, 11:00 in Warsaw (CEST).
MORNING = datetime(2026, 10, 19, 9, tzinfo=UTC)
FOLLOWED = MORNING - timedelta(days=5)


def update(n: int) -> Update:
    return Update(
        headline=f"Synthetic headline {n}",
        url=f"https://example.com/story-{n}",
        source="Example News",
        what_changed=f"Синтетика: изменение {n}.",
        language="ru",
        published_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[StoryStore]:
    with StoryStore(tmp_path / "state.db") as opened:
        yield opened


def story(
    store: StoryStore,
    n: int = 1,
    followed: datetime | None = FOLLOWED,
    status: StoryStatus = StoryStatus.RUMOR,
) -> Story:
    """A story created (with update 100*n) a day before `followed`, then followed."""
    created = store.create(
        f"story-{n}",
        f"Сюжет {n}",
        "Синтетика: описание.",
        status,
        [Entity(EntityKind.DRIVER, "bearman")],
        update(100 * n),
        (followed or FOLLOWED) - timedelta(days=1),
        language="ru",
    )
    if followed is not None:
        store.follow(created.id, followed)
    return created


def sent_texts(store: StoryStore, now: datetime) -> list[str]:
    """Plans and delivers at `now`; the texts sent."""
    sent: list[Reply] = []
    assert alerts.deliver(store, alerts.plan(store, now), now, sent.append)
    return [r.text for r in sent]


# --- the clock ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "quiet"),
    [
        (datetime(2026, 10, 19, 6, 0, tzinfo=UTC), False),  # 08:00 CEST
        (datetime(2026, 10, 19, 5, 59, tzinfo=UTC), True),  # 07:59 CEST
        (datetime(2026, 10, 19, 20, 59, tzinfo=UTC), False),  # 22:59 CEST
        (datetime(2026, 10, 19, 21, 0, tzinfo=UTC), True),  # 23:00 CEST
        # Sunday 2026-10-25: the clocks go back at 03:00 CEST, so 08:00 is 07:00 UTC.
        (datetime(2026, 10, 25, 6, 30, tzinfo=UTC), True),  # 07:30 CET
        (datetime(2026, 10, 25, 7, 0, tzinfo=UTC), False),  # 08:00 CET
        (datetime(2026, 10, 25, 21, 59, tzinfo=UTC), False),  # 22:59 CET
        (datetime(2026, 10, 25, 22, 0, tzinfo=UTC), True),  # 23:00 CET
        # Sunday 2026-03-29: the clocks go forward at 02:00 CET, so 08:00 is 06:00 UTC.
        (datetime(2026, 3, 29, 5, 59, tzinfo=UTC), True),  # 07:59 CEST
        (datetime(2026, 3, 29, 6, 0, tzinfo=UTC), False),  # 08:00 CEST
        (datetime(2026, 3, 28, 21, 59, tzinfo=UTC), False),  # 22:59 CET
        (datetime(2026, 3, 28, 22, 0, tzinfo=UTC), True),  # 23:00 CET
    ],
)
def test_quiet_hours_are_warsaw_time(moment: datetime, quiet: bool) -> None:
    assert alerts.is_quiet(moment) is quiet


@pytest.mark.parametrize(
    ("moment", "start", "hours"),
    [
        (MORNING, datetime(2026, 10, 18, 22, tzinfo=UTC), 24),
        (datetime(2026, 10, 25, 12, tzinfo=UTC), datetime(2026, 10, 24, 22, tzinfo=UTC), 25),
        (datetime(2026, 3, 29, 12, tzinfo=UTC), datetime(2026, 3, 28, 23, tzinfo=UTC), 23),
        # 00:30 in Warsaw is still the previous day in UTC.
        (datetime(2026, 10, 25, 23, 30, tzinfo=UTC), datetime(2026, 10, 25, 23, tzinfo=UTC), 24),
    ],
)
def test_the_day_is_the_warsaw_calendar_day(moment: datetime, start: datetime, hours: int) -> None:
    first, end = alerts.day_bounds(moment)
    assert first == start
    # Aware datetimes in one zone subtract as wall-clock times; the real length is in UTC.
    assert end.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=hours)


# --- what is alerted on ---------------------------------------------------------------------


def test_an_update_on_a_followed_story_is_alerted_once(store: StoryStore) -> None:
    followed = story(store)
    store.add_update(followed.id, update(1), MORNING - timedelta(hours=1))

    [text] = sent_texts(store, MORNING)

    # A card (F41): the title in bold, the status chip, what changed with a linked source.
    assert text == (
        "🔔 <b>Сюжет 1</b>\n"
        "🟡 слух\n"
        "\n"
        "• Синтетика: изменение 1. · "
        '<a href="https://example.com/story-1">Example News</a>'
    )
    assert sent_texts(store, MORNING + timedelta(hours=1)) == []


def test_updates_from_before_following_and_unfollowed_stories_are_not_alerted(
    store: StoryStore,
) -> None:
    # Its first update was recorded before it was followed.
    story(store, 1)
    never = story(store, 2, followed=None)
    store.add_update(never.id, update(2), MORNING - timedelta(hours=1))
    dropped = story(store, 3)
    store.add_update(dropped.id, update(3), MORNING - timedelta(hours=2))
    store.unfollow(dropped.id, MORNING - timedelta(hours=1))

    assert alerts.plan(store, MORNING) == alerts.Plan((), (), (), quiet=False)


def test_following_again_does_not_bring_back_older_updates(store: StoryStore) -> None:
    again = story(store)
    store.add_update(again.id, update(1), MORNING - timedelta(hours=3))
    store.unfollow(again.id, MORNING - timedelta(hours=2))
    store.follow(again.id, MORNING - timedelta(hours=1))

    assert sent_texts(store, MORNING) == []


def test_a_storys_waiting_updates_go_in_one_alert_and_the_oldest_story_first(
    store: StoryStore,
) -> None:
    first, second = story(store, 1), story(store, 2)
    store.add_update(second.id, update(21), MORNING - timedelta(hours=5))
    store.add_update(first.id, update(11), MORNING - timedelta(hours=4))
    store.add_update(second.id, update(22), MORNING - timedelta(hours=3))

    planned = alerts.plan(store, MORNING)

    assert [(a.story.id, [u.update.url for u in a.updates]) for a in planned.send] == [
        (second.id, ["https://example.com/story-21", "https://example.com/story-22"]),
        (first.id, ["https://example.com/story-11"]),
    ]


def test_a_muted_storys_updates_are_skipped_for_good(store: StoryStore) -> None:
    muted = story(store)
    store.mute(muted.id, MORNING + timedelta(days=1))
    store.add_update(muted.id, update(1), MORNING - timedelta(hours=1))

    planned = alerts.plan(store, MORNING)
    assert planned.send == ()
    assert [u.update.url for u in planned.skip] == ["https://example.com/story-1"]
    assert sent_texts(store, MORNING) == []
    # Not sent when the mute ends either: the digest covers it.
    assert sent_texts(store, MORNING + timedelta(days=2)) == []


def test_an_update_that_waited_too_long_is_skipped(store: StoryStore) -> None:
    late = story(store)
    store.add_update(late.id, update(1), MORNING - alerts.STALE_AFTER - timedelta(minutes=1))
    store.add_update(late.id, update(2), MORNING - alerts.STALE_AFTER + timedelta(minutes=1))

    planned = alerts.plan(store, MORNING)

    assert [u.update.url for u in planned.skip] == ["https://example.com/story-1"]
    assert [u.update.url for u in planned.send[0].updates] == ["https://example.com/story-2"]


def test_a_confirmed_story_shows_its_status(store: StoryStore) -> None:
    done = story(store, status=StoryStatus.CONFIRMED)
    store.add_update(done.id, update(1), MORNING - timedelta(hours=1))
    assert sent_texts(store, MORNING)[0].startswith("🔔 <b>Сюжет 1</b>\n🟢 подтверждено\n")


def test_an_update_that_changed_the_status_says_so(store: StoryStore) -> None:
    moved = story(store)
    assert store.add_update(
        moved.id, update(1), MORNING - timedelta(hours=1), status=StoryStatus.DEVELOPING
    )
    lines = sent_texts(store, MORNING)[0].splitlines()
    assert lines[:2] == ["🔔 <b>Сюжет 1</b>", "🟠 развивается (статус изменился)"]


def test_titles_headlines_and_links_are_escaped_for_telegram_html(store: StoryStore) -> None:
    odd = store.create(
        "odd",
        "R&D <ставки>",
        "Синтетика.",
        StoryStatus.RUMOR,
        [Entity(EntityKind.DRIVER, "bearman")],
        update(100),
        FOLLOWED - timedelta(days=1),
        language="ru",
    )
    store.follow(odd.id, FOLLOWED)
    store.add_update(
        odd.id,
        Update("H", "https://example.com/a?x=1&y=2", "A&B <News>", "1 < 2 & 3 > 2", MORNING, "ru"),
        MORNING - timedelta(hours=1),
    )
    [text] = sent_texts(store, MORNING)
    assert "<b>R&amp;D &lt;ставки&gt;</b>" in text
    assert "• 1 &lt; 2 &amp; 3 &gt; 2 · " in text
    assert '<a href="https://example.com/a?x=1&amp;y=2">A&amp;B &lt;News&gt;</a>' in text


# --- quiet hours and the daily limit --------------------------------------------------------


def test_alerts_in_the_quiet_hours_wait_for_the_morning(store: StoryStore) -> None:
    night = datetime(2026, 10, 24, 23, 30, tzinfo=UTC)  # 01:30 CEST, the night of the change
    waiting = story(store)
    store.add_update(waiting.id, update(1), night - timedelta(minutes=5))

    planned = alerts.plan(store, night)
    assert (planned.send, len(planned.waiting), planned.quiet) == ((), 1, True)
    assert planned.describe() == "story alerts: 0 to send, 1 waiting (quiet hours)"
    assert sent_texts(store, night) == []
    # 07:30 CET is still quiet after the clocks went back; 08:00 CET is not.
    assert sent_texts(store, datetime(2026, 10, 25, 6, 30, tzinfo=UTC)) == []
    assert len(sent_texts(store, datetime(2026, 10, 25, 7, 0, tzinfo=UTC))) == 1


def test_at_most_three_alerts_a_warsaw_day_the_rest_wait(store: StoryStore) -> None:
    # Sunday 2026-03-29, the clocks go forward: 08:00 CEST is 06:00 UTC.
    morning = datetime(2026, 3, 29, 6, 0, tzinfo=UTC)
    followed = morning - timedelta(days=2)
    # One alert on Saturday evening (22:30 CET), which does not count for Sunday.
    saturday = story(store, 9, followed=followed)
    store.add_update(saturday.id, update(9), morning - timedelta(hours=8))
    assert len(sent_texts(store, datetime(2026, 3, 28, 21, 30, tzinfo=UTC))) == 1

    many = [story(store, n, followed=followed) for n in range(1, 5)]
    for n, each in enumerate(many, start=1):
        store.add_update(each.id, update(n), morning - timedelta(hours=5 - n))

    planned = alerts.plan(store, morning)
    assert [a.story.id for a in planned.send] == [s.id for s in many[:3]]
    assert [a.story.id for a in planned.waiting] == [many[3].id]
    assert planned.describe() == "story alerts: 3 to send, 1 waiting (at most 3 a day)"
    assert len(sent_texts(store, morning)) == 3

    # 22:59 CEST on the same Sunday: still the same day.
    assert sent_texts(store, datetime(2026, 3, 29, 20, 59, tzinfo=UTC)) == []
    # A new update on an alerted story waits behind the older one.
    store.add_update(many[0].id, update(5), datetime(2026, 3, 29, 20, 0, tzinfo=UTC))
    monday = datetime(2026, 3, 30, 6, 0, tzinfo=UTC)
    texts = sent_texts(store, monday)
    assert [t.splitlines()[0] for t in texts] == ["🔔 <b>Сюжет 4</b>", "🔔 <b>Сюжет 1</b>"]


# --- the message and its button -------------------------------------------------------------


def test_every_alert_carries_the_unfollow_button_the_bot_answers(store: StoryStore) -> None:
    followed = story(store)
    store.add_update(followed.id, update(1), MORNING - timedelta(hours=1))
    [alert] = alerts.plan(store, MORNING).send

    [reply] = alerts.replies(alert)
    [[timeline, button]] = reply.buttons
    assert (timeline.text, timeline.data) == ("Хронология", f"story:{followed.id}")
    assert (button.text, button.data) == ("Отписаться", f"unfollow:{followed.id}")

    handler = Bot(store, CHAT, DEFAULT_FAVOURITES, lambda teams, drivers: DEFAULT_FAVOURITES)
    press = {
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "message": {"message_id": 5, "chat": {"id": int(CHAT)}, "date": 0},
            "data": button.data,
        },
    }
    handler.handle(press, MORNING)
    subscription = store.subscription(followed.id)
    assert subscription is not None and not subscription.active


def test_a_long_alert_is_split_and_the_button_goes_on_the_last_part(store: StoryStore) -> None:
    long = story(store)
    updates = tuple(
        StoryUpdate(
            id=n,
            story_id=long.id,
            update=Update(
                headline="Synthetic headline",
                url=f"https://example.com/{n}",
                source="Example News",
                what_changed="Синтетика. " * 100,
                language="ru",
                published_at=None,
            ),
            recorded_at=MORNING,
        )
        for n in range(1, 6)
    )
    parts = alerts.replies(Alert(long, updates))
    assert len(parts) == 2
    assert all(len(p.text) <= bot.MAX_LENGTH for p in parts)
    assert (parts[0].buttons, len(parts[1].buttons)) == ((), 1)


# --- delivery -------------------------------------------------------------------------------


def test_an_alert_telegram_refuses_waits_for_the_next_run(store: StoryStore) -> None:
    first, second = story(store, 1), story(store, 2)
    store.add_update(first.id, update(1), MORNING - timedelta(hours=2))
    store.add_update(second.id, update(2), MORNING - timedelta(hours=1))
    warnings: list[str] = []

    def refuse(reply: Reply) -> None:
        raise BotError("sendMessage: Too Many Requests: retry after 7", 429, 7)

    assert not alerts.deliver(store, alerts.plan(store, MORNING), MORNING, refuse, warnings.append)
    assert warnings == ["alert on story #1 not sent: sendMessage: Too Many Requests: retry after 7"]
    # Neither counts towards the day's limit; both go with the next run.
    assert store.alerts_sent(*alerts.day_bounds(MORNING)) == 0
    assert len(sent_texts(store, MORNING + timedelta(hours=1))) == 2


def test_an_alert_partly_delivered_is_not_sent_again(store: StoryStore) -> None:
    long = story(store)
    for n in range(1, 6):
        store.add_update(
            long.id,
            Update(
                f"Synthetic headline {n}",
                f"https://example.com/{n}",
                "Example News",
                "Синтетика. " * 100,
                None,
                "ru",
            ),
            MORNING - timedelta(hours=1),
        )
    sent: list[Reply] = []

    def first_part_only(reply: Reply) -> None:
        if sent:
            raise BotError("sendMessage: HTTP 502", 502)
        sent.append(reply)

    assert not alerts.deliver(
        store, alerts.plan(store, MORNING), MORNING, first_part_only, lambda w: None
    )
    assert len(sent) == 1
    assert alerts.plan(store, MORNING + timedelta(hours=1)).send == ()


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
    """A working directory with one followed story that has an update to alert on, the
    clock at MORNING and a mock Bot API."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ts.TOKEN_VAR, raising=False)
    monkeypatch.delenv(ts.CHAT_ID_VAR, raising=False)
    monkeypatch.setattr(cli, "_now", lambda: MORNING)
    telegram = MockTelegram()
    monkeypatch.setattr(cli, "_telegram_client", telegram.client)
    with StoryStore(tmp_path / "state.db") as opened:
        followed = story(opened)
        opened.add_update(followed.id, update(1), MORNING - timedelta(hours=1))
    return telegram


def test_alerts_dry_run_prints_and_records_nothing(
    cli_dir: MockTelegram, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["alerts", "--dry-run"]) == 0
    out, err = capsys.readouterr()
    assert "🔔 <b>Сюжет 1</b>\n🟡 слух" in out
    assert "[Отписаться]" in out
    assert "story alerts: 1 to send" in err
    assert "dry run: no alert sent, nothing recorded" in err
    with StoryStore("state.db") as store:
        assert len(store.unalerted()) == 1
    assert cli_dir.sent == []


def test_alerts_sends_with_the_unfollow_button(
    cli_dir: MockTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ts.TOKEN_VAR, TOKEN)
    monkeypatch.setenv(ts.CHAT_ID_VAR, CHAT)

    assert main(["alerts"]) == 0
    assert main(["alerts"]) == 0

    [sent] = cli_dir.sent
    assert sent["chat_id"] == CHAT
    assert sent["text"].startswith("🔔 <b>Сюжет 1</b>")
    assert sent["parse_mode"] == "HTML"
    assert sent["reply_markup"] == {
        "inline_keyboard": [
            [
                {"text": "Хронология", "callback_data": "story:1"},
                {"text": "Отписаться", "callback_data": "unfollow:1"},
            ]
        ]
    }


def test_alerts_without_the_telegram_settings(
    cli_dir: MockTelegram, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["alerts", "--env-file", "missing"]) == 1
    assert "error: story alerts:" in capsys.readouterr().err
    with StoryStore("state.db") as store:
        assert len(store.unalerted()) == 1


def test_nothing_to_alert_needs_no_settings_and_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ts.TOKEN_VAR, raising=False)
    assert main(["alerts", "--env-file", "missing"]) == 0
    assert capsys.readouterr() == ("", "")


def test_run_sends_the_alerts_whatever_else_is_due(
    cli_dir: MockTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ts.TOKEN_VAR, TOKEN)
    monkeypatch.setenv(ts.CHAT_ID_VAR, CHAT)
    monkeypatch.setattr(cli, "_run_due", lambda *args: 1)

    # The digest step failed, the alert still went out.
    assert main(["run"]) == 1
    assert len(cli_dir.sent) == 1


def test_run_dry_run_prints_the_alerts(
    cli_dir: MockTelegram, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_run_due", lambda *args: 0)
    assert main(["run", "--dry-run"]) == 0
    assert "🔔 <b>Сюжет 1</b>" in capsys.readouterr().out
    assert cli_dir.sent == []

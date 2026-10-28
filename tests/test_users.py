# The bot texts are Russian: Cyrillic letters that look like Latin ones are intended here.
# ruff: noqa: RUF001
"""Users instead of a single recipient (F32): accounts, per-user settings, the migration of a
pre-F32 state file to the owner's account, per-user subscriptions, alerts and send logs, and
a time zone other than Warsaw across a DST change. Synthetic data only; no network."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import load_fixture

from f1brief import (
    alerts,
    bot_texts,
    config,
    facts,
    lifecycle,
    schedule,
    send_window,
    stories,
    users,
)
from f1brief import send_log as send_log_module
from f1brief.bot import Bot
from f1brief.cli import main
from f1brief.config import Config, Favourite, Favourites
from f1brief.send_log import SendLog, StateError
from f1brief.stories import Entity, EntityKind, StoryStatus, StoryStore, Update
from f1brief.users import OWNER_ID, UserSettings, UserStatus, UserStore

NEW_YORK = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 19, 9, tzinfo=UTC)
OWNER_CHAT = "424242"
OTHER_CHAT = "777001"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def accounts(state: Path):
    with UserStore(state) as opened:
        yield opened


def update(n: int, recorded: datetime = NOW) -> Update:
    return Update(
        headline=f"Synthetic headline {n}",
        url=f"https://example.com/story-{n}",
        source="Example News",
        what_changed=f"Синтетика: изменение {n}.",
        language="ru",
        published_at=recorded,
    )


def new_story(store: StoryStore, n: int = 1, created: datetime = NOW) -> stories.Story:
    return store.create(
        f"story-{n}",
        f"Сюжет {n}",
        "Синтетика: описание.",
        StoryStatus.RUMOR,
        [Entity(EntityKind.DRIVER, "bearman"), Entity(EntityKind.TEAM, "ferrari")],
        update(100 * n, created),
        created,
        language="ru",
    )


# --- accounts ------------------------------------------------------------------------------


def test_a_new_state_file_has_the_owner_with_the_config_defaults(accounts: UserStore) -> None:
    owner = accounts.owner()
    assert (owner.id, owner.status, owner.chat_id, owner.language, owner.timezone) == (
        OWNER_ID,
        UserStatus.ACTIVE,
        None,
        None,
        None,
    )
    assert accounts.users() == [owner]
    assert accounts.settings(OWNER_ID) == UserSettings()
    defaults = Config(language="pl", days_before_fp1=3)
    assert accounts.effective(OWNER_ID, defaults) == defaults


def test_binding_the_owner_records_the_env_chat(accounts: UserStore) -> None:
    owner = accounts.bind_owner(OWNER_CHAT)
    # A private chat's id is the user's id.
    assert (owner.chat_id, owner.telegram_user_id) == (OWNER_CHAT, 424242)
    assert accounts.bind_owner(OWNER_CHAT) == owner
    assert accounts.by_chat(OWNER_CHAT) == owner
    assert accounts.by_chat(OTHER_CHAT) is None


def test_a_group_chat_is_not_a_user_id_and_a_new_env_chat_moves_the_owner(
    accounts: UserStore,
) -> None:
    owner = accounts.bind_owner("-100123")
    assert (owner.chat_id, owner.telegram_user_id) == ("-100123", None)
    moved = accounts.bind_owner(OWNER_CHAT)
    assert (moved.chat_id, moved.telegram_user_id) == (OWNER_CHAT, 424242)
    assert accounts.by_chat("-100123") is None


def test_the_owner_cannot_take_another_accounts_chat(accounts: UserStore) -> None:
    accounts.create(OTHER_CHAT, NOW)
    with pytest.raises(StateError, match=f"chat {OTHER_CHAT} belongs to user 2"):
        accounts.bind_owner(OTHER_CHAT)


def test_creating_an_account(accounts: UserStore) -> None:
    user = accounts.create(
        OTHER_CHAT,
        NOW,
        telegram_user_id=777001,
        name="Synthetic",
        language="en",
        timezone="America/New_York",
    )
    assert (user.id, user.chat_id, user.language, user.timezone, user.created_at) == (
        2,
        OTHER_CHAT,
        "en",
        "America/New_York",
        NOW,
    )
    with pytest.raises(StateError):
        accounts.create(OTHER_CHAT, NOW)  # the chat has an account
    with pytest.raises(ValueError, match="not a supported language"):
        accounts.create("1", NOW, language="de")
    with pytest.raises(ValueError, match="unknown time zone"):
        accounts.create("1", NOW, timezone="Europe/Atlantis")
    assert accounts.set_status(user.id, UserStatus.BLOCKED).status is UserStatus.BLOCKED


def test_settings_override_the_config_and_none_goes_back_to_it(accounts: UserStore) -> None:
    defaults = Config()
    accounts.set_language(OWNER_ID, "en")
    accounts.set_timezone(OWNER_ID, "America/New_York")
    accounts.update_settings(
        OWNER_ID,
        days_before_fp1=1,
        max_alerts_per_day=5,
        quiet_start=time(22, 30),
        quiet_end=time(7),
    )
    effective = accounts.effective(OWNER_ID, defaults)
    assert (
        effective.language,
        effective.timezone,
        effective.days_before_fp1,
        effective.max_alerts_per_day,
        effective.quiet_start,
        effective.quiet_end,
    ) == ("en", "America/New_York", 1, 5, time(22, 30), time(7))
    assert effective.favourites == defaults.favourites

    # Changing one setting keeps the others.
    accounts.update_settings(OWNER_ID, days_before_fp1=None)
    settings = accounts.settings(OWNER_ID)
    assert (settings.days_before_fp1, settings.max_alerts_per_day) == (None, 5)
    accounts.set_language(OWNER_ID, None)
    assert accounts.effective(OWNER_ID, Config(language="pl")).language == "pl"
    assert accounts.effective(OWNER_ID, Config(days_before_fp1=4)).days_before_fp1 == 4


@pytest.mark.parametrize(
    "change",
    [{"days_before_fp1": -1}, {"max_alerts_per_day": -2}, {"days_before_fp1": True}],
)
def test_invalid_settings_are_refused(accounts: UserStore, change: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="whole number"):
        accounts.update_settings(OWNER_ID, **change)


# --- favourites ----------------------------------------------------------------------------

RED_BULL = Favourite("red_bull", "Red Bull")
HAMILTON = Favourite("hamilton", "Hamilton", ("Hamilton",))


def test_an_account_follows_the_config_favourites_until_it_changes_them(
    accounts: UserStore,
) -> None:
    defaults = config.DEFAULT_FAVOURITES
    assert accounts.favourites(OWNER_ID, defaults) == defaults
    fewer = Favourites(defaults.teams[:1], ())
    assert accounts.favourites(OWNER_ID, fewer) == fewer


def test_adding_favourites_copies_the_defaults_once(accounts: UserStore) -> None:
    defaults = config.DEFAULT_FAVOURITES
    added = accounts.add_favourites(OWNER_ID, defaults, [RED_BULL], [HAMILTON])
    assert added.team_ids == ("mclaren", "haas", "red_bull")
    assert added.driver_ids == ("russell", "ocon", "bearman", "hamilton")
    # Now the account's own: a later change of the config does not reach it.
    assert accounts.favourites(OWNER_ID, Favourites((), ())) == added
    # Adding one that is there already changes nothing.
    assert accounts.add_favourites(OWNER_ID, defaults, [RED_BULL]) == added


def test_removing_a_favourite(accounts: UserStore) -> None:
    defaults = config.DEFAULT_FAVOURITES
    left = accounts.remove_favourite(OWNER_ID, defaults, "drivers", "ocon")
    assert left.driver_ids == ("russell", "bearman")
    assert accounts.favourites(OWNER_ID, defaults) == left
    assert accounts.remove_favourite(OWNER_ID, defaults, "drivers", "ocon") == left
    emptied = accounts.remove_favourite(OWNER_ID, defaults, "teams", "mclaren")
    emptied = accounts.remove_favourite(OWNER_ID, defaults, "teams", "haas")
    # An emptied list stays empty: it does not fall back to the defaults.
    assert accounts.favourites(OWNER_ID, defaults).teams == emptied.teams == ()
    with pytest.raises(ValueError, match="kind"):
        accounts.remove_favourite(OWNER_ID, defaults, "people", "x")


def test_favourite_changes_without_writing(accounts: UserStore) -> None:
    defaults = config.DEFAULT_FAVOURITES
    would = accounts.add_favourites(OWNER_ID, defaults, [RED_BULL], write=False)
    assert "red_bull" in would.team_ids
    would = accounts.remove_favourite(OWNER_ID, defaults, "teams", "haas", write=False)
    assert "haas" not in would.team_ids
    assert accounts.settings(OWNER_ID).favourites is None


def test_favourites_keep_every_field() -> None:
    favourites = Favourites(
        (Favourite("haas", "Haas", ("Haas", "TGR Haas"), names=(("ru", "Хаас"),)),),
        (Favourite("ocon", "Esteban Ocon", ("Ocon",), track_future=True),),
    )
    assert users.favourites_from_json(users.favourites_json(favourites)) == favourites


def test_one_accounts_favourites_are_not_anothers(accounts: UserStore) -> None:
    other = accounts.create(OTHER_CHAT, NOW)
    defaults = config.DEFAULT_FAVOURITES
    accounts.add_favourites(other.id, defaults, [RED_BULL])
    assert accounts.favourites(OWNER_ID, defaults) == defaults
    assert "red_bull" in accounts.favourites(other.id, defaults).team_ids


# --- per-user subscriptions, alerts and send logs ------------------------------------------


def test_following_is_per_account(state: Path, accounts: UserStore) -> None:
    other = accounts.create(OTHER_CHAT, NOW)
    with StoryStore(state) as owners, StoryStore(state, other.id) as others:
        story = new_story(owners)
        assert others.follow(story.id, NOW)
        assert owners.subscription(story.id) is None
        assert owners.followed() == []
        assert [s.id for s in others.followed()] == [story.id]
        # Both may follow, mute and unfollow on their own.
        assert owners.follow(story.id, NOW)
        assert others.mute(story.id, NOW + timedelta(days=7))
        owner_sub, other_sub = owners.subscription(story.id), others.subscription(story.id)
        assert owner_sub is not None and owner_sub.muted_until is None
        assert other_sub is not None and other_sub.muted_until is not None
        assert others.unfollow(story.id, NOW)
        assert owners.subscription(story.id).active  # type: ignore[union-attr]


def test_alerts_and_their_daily_cap_are_per_account(state: Path, accounts: UserStore) -> None:
    other = accounts.create(OTHER_CHAT, NOW)
    with StoryStore(state) as owners, StoryStore(state, other.id) as others:
        story = new_story(owners, created=NOW - timedelta(days=2))
        owners.follow(story.id, NOW - timedelta(days=1))
        others.follow(story.id, NOW - timedelta(days=1))
        owners.add_update(story.id, update(2, NOW - timedelta(hours=2)), NOW - timedelta(hours=2))

        planned = alerts.plan(owners, NOW)
        assert [a.story.id for a in planned.send] == [story.id]
        assert alerts.deliver(owners, planned, NOW, lambda reply: None)
        # The owner's alert is not the other account's: it still waits for its own.
        assert owners.unalerted() == []
        assert [u.story_id for u in others.unalerted()] == [story.id]
        day = alerts.day_bounds(NOW)
        assert (owners.alerts_sent(*day), others.alerts_sent(*day)) == (1, 0)
        assert alerts.plan(others, NOW, max_per_day=0).waiting


def test_offers_to_unfollow_are_per_account(state: Path, accounts: UserStore) -> None:
    other = accounts.create(OTHER_CHAT, NOW)
    long_ago = NOW - timedelta(days=90)
    with StoryStore(state) as owners, StoryStore(state, other.id) as others:
        story = new_story(owners, created=long_ago)
        owners.follow(story.id, long_ago)
        others.follow(story.id, long_ago)
        offers = lifecycle.plan(owners, NOW)
        assert lifecycle.deliver(owners, offers, NOW, lambda reply: None)
        assert lifecycle.plan(owners, NOW).send == ()
        assert [o.story.id for o in lifecycle.plan(others, NOW).send] == [story.id]


def test_the_send_log_is_per_account(state: Path, accounts: UserStore) -> None:
    other = accounts.create(OTHER_CHAT, NOW)
    with SendLog(state) as owners, SendLog(state, other.id) as others:
        assert owners.claim(2026, 18, NOW)
        assert others.get(2026, 18) is None
        assert others.claim(2026, 18, NOW + timedelta(minutes=1))
        assert not owners.claim(2026, 18, NOW)
        owners.mark_sent(2026, 18, NOW, 3)
        assert others.get(2026, 18).status is send_log_module.Status.SENDING  # type: ignore[union-attr]
        assert owners.last_digest_at(NOW + timedelta(hours=1)) == NOW
        assert others.forget(2026, 18)
        assert owners.get(2026, 18) is not None
        assert owners.claim_qualifying(2026, 18, NOW) and others.claim_qualifying(2026, 18, NOW)
        assert owners.claim_summary("2026-12", NOW) and others.claim_summary("2026-12", NOW)


def test_deleting_an_account_deletes_its_rows_only(state: Path, accounts: UserStore) -> None:
    other = accounts.create(OTHER_CHAT, NOW)
    accounts.update_settings(other.id, days_before_fp1=1)
    with StoryStore(state) as owners, StoryStore(state, other.id) as others:
        story = new_story(owners)
        owners.follow(story.id, NOW)
        others.follow(story.id, NOW)
    with SendLog(state, other.id) as log:
        log.claim(2026, 18, NOW)
    with sqlite3.connect(state) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("DELETE FROM users WHERE id = ?", (other.id,))
        for table in ("subscriptions", "sends", "user_settings"):
            rows = db.execute(f"SELECT user_id FROM {table}").fetchall()
            assert rows in ([(OWNER_ID,)], []), table
    with StoryStore(state) as owners:
        assert owners.subscription(story.id) is not None


# --- a state file from before F32 --------------------------------------------------------


def old_state_file(path: Path) -> None:
    """A state file as F31 left it: stories schema 5, an unversioned send log, no users."""
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE schema_versions (component TEXT NOT NULL PRIMARY KEY, "
            "version INTEGER NOT NULL)"
        )
        for step in stories._MIGRATIONS[:5]:
            for statement in step:
                db.execute(statement)
        db.execute("INSERT INTO schema_versions VALUES ('stories', 5)")
        for step in send_log_module._MIGRATIONS[:1]:
            for statement in step:
                db.execute(statement)
        stamp = NOW.isoformat()
        db.execute(
            "INSERT INTO stories (id, slug, title, summary, status, created_at, "
            "last_update_at, language) VALUES (1, 'old', 'Старый', 'Синтетика.', 'rumor', ?, "
            "?, 'ru')",
            (stamp, stamp),
        )
        db.execute(
            "INSERT INTO story_updates (id, story_id, headline, url, source, what_changed, "
            "recorded_at, language) VALUES (10, 1, 'H', 'https://example.com/a', 'Example', "
            "'Синтетика.', ?, 'ru')",
            (stamp,),
        )
        db.execute("INSERT INTO subscriptions VALUES (1, 1, ?, NULL, NULL)", (stamp,))
        db.execute("INSERT INTO story_alerts VALUES (10, 1, 'sent', ?)", (stamp,))
        db.execute("INSERT INTO stale_offers VALUES (1, ?, ?)", (stamp, stamp))
        db.execute("INSERT INTO sends VALUES (2026, 17, 'sent', ?, ?, 4)", (stamp, stamp))
        db.execute(
            "INSERT INTO qualifying_sends VALUES (2026, 17, 'partial', ?, ?, 1)", (stamp, stamp)
        )
        db.execute("INSERT INTO summaries VALUES ('2026-01', 'sent', ?, ?, 2)", (stamp, stamp))


@pytest.mark.parametrize("first", ["stories", "send_log"])
def test_an_old_state_file_moves_to_the_owners_account(state: Path, first: str) -> None:
    old_state_file(state)
    # Whichever part opens first creates the owner before moving rows to it.
    (StoryStore if first == "stories" else SendLog)(state).close()
    with StoryStore(state) as store, SendLog(state) as log, UserStore(state) as accounts:
        assert accounts.owner().id == OWNER_ID
        subscription = store.subscription(1)
        assert subscription is not None and subscription.active
        assert store.unalerted() == []  # the old alert is the owner's
        assert store.stale_offered(1, NOW)
        assert log.get(2026, 17).messages == 4  # type: ignore[union-attr]
        assert log.get_qualifying(2026, 17).status.value == "partial"  # type: ignore[union-attr]
        assert log.get_summary("2026-01") is not None
        other = accounts.create(OTHER_CHAT, NOW)
    with StoryStore(state, other.id) as others, SendLog(state, other.id) as others_log:
        assert others.subscription(1) is None
        assert others_log.get(2026, 17) is None
    with sqlite3.connect(state) as db:
        versions = dict(db.execute("SELECT component, version FROM schema_versions"))
    assert versions == {
        "stories": stories.SCHEMA_VERSION,
        "send_log": len(send_log_module._MIGRATIONS),
        "users": users.SCHEMA_VERSION,
    }


def test_a_users_schema_from_a_newer_version_is_refused(state: Path) -> None:
    UserStore(state).close()
    with sqlite3.connect(state) as db:
        db.execute("UPDATE schema_versions SET version = 99 WHERE component = 'users'")
    with pytest.raises(StateError, match="users schema is version 99"):
        SendLog(state)


# --- a time zone other than Warsaw, across a DST change ------------------------------------
# New York leaves summer time (EDT, UTC-4) for EST (UTC-5) on Sunday 1 November 2026 at 06:00
# UTC; Warsaw left it a week earlier, on 25 October.


@pytest.fixture
def races() -> list[schedule.Race]:
    return schedule.parse_schedule(load_fixture("jolpica/schedule_2026.json"))


def by_round(races: list[schedule.Race], round_: int) -> schedule.Race:
    return next(r for r in races if r.round == round_)


def test_the_send_window_is_new_york_midnight_on_both_sides_of_the_change(
    races: list[schedule.Race],
) -> None:
    mexico, brazil = by_round(races, 19), by_round(races, 20)
    # Mexico: FP1 Fri 30.10 18:30 UTC (14:30 EDT); the window opens Wed 28.10 00:00 EDT.
    assert send_window.window(mexico, 2, NEW_YORK).opens == utc(2026, 10, 28, 4)
    # Brazil: FP1 Fri 06.11 15:30 UTC (10:30 EST); the window opens Wed 04.11 00:00 EST.
    assert send_window.window(brazil, 2, NEW_YORK).opens == utc(2026, 11, 4, 5)
    # Six days before Brazil's FP1 is Sat 31.10, still summer time in New York.
    assert send_window.window(brazil, 6, NEW_YORK).opens == utc(2026, 10, 31, 4)
    # The same window in Warsaw (CET, UTC+1 by then) opens at 23:00 UTC the day before.
    assert send_window.window(brazil, 2).opens == utc(2026, 11, 3, 23)


def test_the_digest_is_due_by_new_york_time(state: Path, races: list[schedule.Race]) -> None:
    with SendLog(state) as log:
        # 04:30 UTC on 4.11 is 23:30 EST on 3.11: too early in New York, due in Warsaw.
        moment = utc(2026, 11, 4, 4, 30)
        brazil_only = [by_round(races, 20)]
        assert not send_window.decide(brazil_only, moment, log, 2, NEW_YORK).due
        assert send_window.decide(brazil_only, moment, log, 2).due
        late = send_window.decide(brazil_only, utc(2026, 11, 3, 12), log, 2, NEW_YORK)
        assert "opens Wed 04 Nov 00:00" in late.reason


@pytest.mark.parametrize(
    ("moment", "quiet"),
    [
        (utc(2026, 10, 31, 11, 59), True),  # 07:59 EDT
        (utc(2026, 10, 31, 12, 0), False),  # 08:00 EDT
        (utc(2026, 11, 1, 12, 30), True),  # 07:30 EST: the same UTC time is quiet now
        (utc(2026, 11, 1, 13, 0), False),  # 08:00 EST
        (utc(2026, 11, 2, 3, 59), False),  # 22:59 EST
        (utc(2026, 11, 2, 4, 0), True),  # 23:00 EST
    ],
)
def test_quiet_hours_in_new_york(moment: datetime, quiet: bool) -> None:
    assert alerts.is_quiet(moment, alerts.QuietHours(tz=NEW_YORK)) is quiet


def test_the_day_of_the_change_is_25_hours_in_new_york() -> None:
    start, end = alerts.day_bounds(utc(2026, 11, 1, 12), NEW_YORK)
    assert start.astimezone(UTC) == utc(2026, 11, 1, 4)
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=25)


@pytest.mark.parametrize(
    ("start", "end", "local", "quiet"),
    [
        (time(1), time(6), time(0, 59), False),  # a window that does not cross midnight
        (time(1), time(6), time(1), True),
        (time(1), time(6), time(6), False),
        (time(8), time(8), time(3), False),  # equal times: no quiet hours
    ],
)
def test_other_quiet_hours(start: time, end: time, local: time, quiet: bool) -> None:
    moment = datetime.combine(datetime(2026, 11, 3).date(), local, NEW_YORK)
    assert alerts.QuietHours(start, end, NEW_YORK).contains(moment) is quiet


def test_the_alert_cap_counts_the_new_york_day(state: Path) -> None:
    with StoryStore(state) as store:
        story = new_story(store, created=utc(2026, 11, 1, 0))
        store.follow(story.id, utc(2026, 11, 1, 1))
        store.add_update(story.id, update(2, utc(2026, 11, 1, 2)), utc(2026, 11, 1, 2))
        hours = alerts.QuietHours(time(23), time(23), NEW_YORK)  # no quiet hours
        # 03:30 UTC on 2.11 is 22:30 EST on 1.11: one alert sent that New York day.
        sent_at = utc(2026, 11, 2, 3, 30)
        planned = alerts.plan(store, sent_at, 1, hours)
        assert planned.send
        assert alerts.deliver(store, planned, sent_at, lambda r: None)
        store.add_update(story.id, update(3, sent_at), sent_at)
        assert alerts.plan(store, utc(2026, 11, 2, 4, 30), 1, hours).waiting  # still 1.11
        assert alerts.plan(store, utc(2026, 11, 2, 5, 30), 1, hours).send  # 00:30 EST, 2.11


def test_the_schedule_in_the_facts_is_in_the_readers_zone(races: list[schedule.Race]) -> None:
    mexico = facts.race_facts(by_round(races, 19), NEW_YORK)
    starts = {s["session"]: s["start_local"] for s in mexico["sessions"]}
    assert starts["FP1"] == "2026-10-30T14:30:00-04:00"
    assert starts["Race"] == "2026-11-01T15:00:00-05:00"


def test_the_bot_shows_the_next_race_in_the_accounts_zone(
    state: Path, races: list[schedule.Race]
) -> None:
    with StoryStore(state) as store:
        ui = Bot(
            store,
            OWNER_CHAT,
            config.DEFAULT_FAVOURITES,
            lambda teams, drivers: config.DEFAULT_FAVOURITES,
            calendar=lambda: races,
            tz=NEW_YORK,
        )
        # After the US GP: the next round is Mexico, across the change.
        [reply] = ui.handle(
            {"update_id": 1, "message": {"chat": {"id": 424242}, "text": "/next"}},
            utc(2026, 10, 26, 12),
        )
    lines = reply.text.splitlines()  # type: ignore[union-attr]
    # The F41 card: the schedule as an aligned <pre> table in the account's zone.
    assert lines[3] == "Расписание, время America/New_York:"
    assert lines[4] == "<pre>Пт 30.10 14:30  Практика 1"
    assert lines[8] == "Вс 01.11 15:00  Гонка</pre>"
    assert "Дайджест к этому этапу придёт Ср 28.10 00:00 по времени America/New_York." in lines


def test_warsaw_keeps_its_wording() -> None:
    assert bot_texts.texts_for("ru").by_zone(schedule.WARSAW) == "по Варшаве"
    assert bot_texts.texts_for("ru").zone_note(ZoneInfo("Europe/Warsaw")) == "время варшавское"


# --- the CLI uses the owner's settings -----------------------------------------------------


def test_config_shows_the_owners_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["config"]) == 0
    assert "owner's own changes" not in capsys.readouterr().out
    assert not (tmp_path / "state.db").exists()  # reading the settings creates no state file

    with UserStore(tmp_path / "state.db") as accounts:
        accounts.set_timezone(OWNER_ID, "America/New_York")
        accounts.remove_favourite(OWNER_ID, config.DEFAULT_FAVOURITES, "teams", "haas")
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "with the owner's own changes from state.db" in out
    assert "time zone: America/New_York" in out
    assert "haas" not in out
    assert "story alerts: at most 3 a day, quiet hours 23:00-08:00" in out


def test_due_uses_the_owners_time_zone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from f1brief import cli

    monkeypatch.chdir(tmp_path)
    calendar = schedule.parse_schedule(load_fixture("jolpica/schedule_2026.json"))
    monkeypatch.setattr(cli, "_calendar", lambda: [by_round(calendar, 20)])
    monkeypatch.setattr(cli, "_now", lambda: utc(2026, 11, 4, 4, 30))
    monkeypatch.setattr(cli, "_decide_summary", lambda *args: None)
    with UserStore(tmp_path / "state.db") as accounts:
        accounts.set_timezone(OWNER_ID, "America/New_York")
    main(["due"])
    out = capsys.readouterr().out
    assert "Too early: the window opens Wed 04 Nov 00:00." in out
    assert "(America/New_York)" in out


def test_replace_keeps_the_config_source() -> None:
    # `effective` keeps where the settings came from, for `config`.
    source = Path("config.toml")
    defaults = replace(Config(), source=source)
    owner = users.User(OWNER_ID, None, None, None, "en", None, UserStatus.ACTIVE, NOW)
    assert users.effective(defaults, owner, UserSettings()).source == source

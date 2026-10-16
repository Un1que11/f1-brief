from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path

import pytest

from f1brief.send_log import DEFAULT_STATE_FILE, SendLog, StateError, Status

STARTED = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
FINISHED = datetime(2026, 10, 21, 8, 1, tzinfo=UTC)


@pytest.fixture
def log(tmp_path: Path):
    with SendLog(tmp_path / "state.db") as opened:
        yield opened


def test_an_unknown_round_has_no_record(log: SendLog) -> None:
    assert log.get(2026, 18) is None


def test_a_round_can_be_claimed_only_once(log: SendLog) -> None:
    assert log.claim(2026, 18, STARTED) is True
    assert log.claim(2026, 18, FINISHED) is False
    assert log.claim(2026, 19, STARTED) is True

    record = log.get(2026, 18)
    assert record is not None
    assert (record.status, record.started_at, record.finished_at) == (
        Status.SENDING,
        STARTED,
        None,
    )


def test_two_connections_cannot_both_claim(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with SendLog(path) as first, SendLog(path) as second:
        assert first.claim(2026, 18, STARTED) is True
        assert second.claim(2026, 18, STARTED) is False


def test_a_sent_round_keeps_its_count(log: SendLog) -> None:
    log.claim(2026, 18, STARTED)
    log.mark_sent(2026, 18, FINISHED, 3)

    record = log.get(2026, 18)
    assert record is not None
    assert (record.status, record.finished_at, record.messages) == (Status.SENT, FINISHED, 3)
    assert log.claim(2026, 18, FINISHED) is False


def test_a_partial_send_keeps_the_delivered_count(log: SendLog) -> None:
    log.claim(2026, 18, STARTED)
    log.mark_partial(2026, 18, FINISHED, 1)

    record = log.get(2026, 18)
    assert record is not None
    assert (record.status, record.messages) == (Status.PARTIAL, 1)


def test_finishing_needs_a_claim(log: SendLog) -> None:
    with pytest.raises(StateError, match="not claimed"):
        log.mark_sent(2026, 18, FINISHED, 1)
    log.claim(2026, 18, STARTED)
    log.mark_sent(2026, 18, FINISHED, 1)
    with pytest.raises(StateError, match="not claimed"):
        log.mark_partial(2026, 18, FINISHED, 0)


def test_release_drops_only_an_unfinished_claim(log: SendLog) -> None:
    log.claim(2026, 18, STARTED)
    log.release(2026, 18)
    assert log.get(2026, 18) is None

    log.claim(2026, 18, STARTED)
    log.mark_sent(2026, 18, FINISHED, 2)
    log.release(2026, 18)
    record = log.get(2026, 18)
    assert record is not None and record.status is Status.SENT


def test_forget_drops_any_record(log: SendLog) -> None:
    log.claim(2026, 18, STARTED)
    log.mark_partial(2026, 18, FINISHED, 1)

    assert log.forget(2026, 18) is True
    assert log.get(2026, 18) is None
    assert log.forget(2026, 18) is False


def test_records_survive_reopening(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with SendLog(path) as log:
        log.claim(2026, 18, STARTED)
        log.mark_sent(2026, 18, FINISHED, 2)
    with SendLog(path) as log:
        record = log.get(2026, 18)
    assert record is not None and record.status is Status.SENT


def test_timestamps_are_stored_in_utc(log: SendLog) -> None:
    from zoneinfo import ZoneInfo

    log.claim(2026, 18, datetime(2026, 10, 21, 10, 0, tzinfo=ZoneInfo("Europe/Warsaw")))
    record = log.get(2026, 18)
    assert record is not None and record.started_at == STARTED


def test_naive_timestamps_are_rejected(log: SendLog) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        log.claim(2026, 18, datetime(2026, 10, 21, 8, 0))


def test_an_unopenable_file_is_a_state_error(tmp_path: Path) -> None:
    with pytest.raises(StateError, match="cannot open"):
        SendLog(tmp_path / "no-such-dir" / "state.db")


def test_the_default_state_file_is_git_ignored() -> None:
    gitignore = Path(__file__).parent.parent / ".gitignore"
    patterns = [
        line.strip()
        for line in gitignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert any(fnmatch(DEFAULT_STATE_FILE, pattern) for pattern in patterns)

# The digest is Russian: Cyrillic letters that look like Latin ones are intended here.
# ruff: noqa: RUF001
import io
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from f1brief import cli
from f1brief import telegram_send as ts
from f1brief.cli import main

# Synthetic values in the Bot API's shapes; no real bot or chat.
TOKEN = "123456:TEST-token_abcDEF"
CHAT_ID = "-1001234567890"
CREDENTIALS = ts.Credentials(token=TOKEN, chat_id=CHAT_ID)
SENT_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

OK = {"ok": True, "result": {"message_id": 1, "chat": {"id": -1001234567890}, "date": 0}}
PARSE_ERROR = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: can't parse entities: Character '.' is reserved",
}


def too_many(retry_after: int) -> httpx.Response:
    body = {
        "ok": False,
        "error_code": 429,
        "description": f"Too Many Requests: retry after {retry_after}",
        "parameters": {"retry_after": retry_after},
    }
    return httpx.Response(429, json=body)


class Recorder:
    """A mock Bot API: answers with `responses` in turn (the last one repeats) and keeps the
    requests."""

    def __init__(self, *responses: httpx.Response | Callable[[], httpx.Response]) -> None:
        self.responses = list(responses) or [httpx.Response(200, json=OK)]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses[min(len(self.requests), len(self.responses)) - 1]
        return response() if callable(response) else response

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def texts(self) -> list[str]:
        return [json.loads(r.content)["text"] for r in self.requests]


def sender(recorder: Recorder, sleeps: list[float] | None = None) -> ts.Sender:
    sleeps = [] if sleeps is None else sleeps
    return ts.Sender(CREDENTIALS, recorder.client(), sleep=sleeps.append)


# --- settings ---------------------------------------------------------------------------


def test_read_env_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# Telegram\n"
        "\n"
        "TELEGRAM_BOT_TOKEN = '1:abc'\n"
        'export TELEGRAM_CHAT_ID="42"\n'
        "OTHER=a=b\n"
        "not a setting\n",
        encoding="utf-8",
    )
    assert ts.read_env_file(path) == {
        "TELEGRAM_BOT_TOKEN": "1:abc",
        "TELEGRAM_CHAT_ID": "42",
        "OTHER": "a=b",
    }


def test_a_missing_env_file_is_empty(tmp_path: Path) -> None:
    assert ts.read_env_file(tmp_path / ".env") == {}


def test_credentials_come_from_the_environment_first(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("TELEGRAM_BOT_TOKEN=from-file\nTELEGRAM_CHAT_ID=7\n", encoding="utf-8")
    env = {"TELEGRAM_BOT_TOKEN": "from-env"}
    assert ts.load_credentials(env, path) == ts.Credentials(token="from-env", chat_id="7")


def test_missing_credentials_are_named_without_values(tmp_path: Path) -> None:
    env = {"TELEGRAM_CHAT_ID": CHAT_ID, "TELEGRAM_BOT_TOKEN": "  "}
    with pytest.raises(ts.SendError) as exc:
        ts.load_credentials(env, tmp_path / ".env")
    assert str(exc.value) == (
        f"TELEGRAM_BOT_TOKEN not set (in the environment or {tmp_path / '.env'})"
    )

    with pytest.raises(ts.SendError, match="TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID not set"):
        ts.load_credentials({}, tmp_path / ".env")


def test_credentials_repr_hides_the_values() -> None:
    assert TOKEN not in repr(CREDENTIALS)
    assert CHAT_ID not in repr(CREDENTIALS)


# --- sending ----------------------------------------------------------------------------


def test_sends_each_message_as_markdown_v2_in_order() -> None:
    recorder, sleeps = Recorder(), []
    sender(recorder, sleeps).send(["*1\\. Раунд*", "Дождь\\.", "Конец"])

    assert [str(r.url) for r in recorder.requests] == [SENT_URL] * 3
    assert all(r.method == "POST" for r in recorder.requests)
    assert json.loads(recorder.requests[0].content) == {
        "chat_id": CHAT_ID,
        "text": "*1\\. Раунд*",
        "parse_mode": "MarkdownV2",
        "link_preview_options": {"is_disabled": True},
    }
    assert recorder.texts() == ["*1\\. Раунд*", "Дождь\\.", "Конец"]
    assert sleeps == [ts.PAUSE_S, ts.PAUSE_S]  # between messages, not before the first


def test_nothing_to_send_is_an_error() -> None:
    with pytest.raises(ts.SendError, match="nothing to send"):
        sender(Recorder()).send([])


def test_a_refused_message_stops_the_rest() -> None:
    recorder = Recorder(httpx.Response(200, json=OK), httpx.Response(400, json=PARSE_ERROR))
    with pytest.raises(ts.SendError) as exc:
        sender(recorder).send(["a", "b", "c"])

    assert str(exc.value) == (
        "message 2 of 3 not sent (1 sent before it): Telegram refused the message: "
        "Bad Request: can't parse entities: Character '.' is reserved"
    )
    assert recorder.texts() == ["a", "b"]


def test_waits_and_retries_when_rate_limited() -> None:
    recorder, sleeps = Recorder(too_many(3), httpx.Response(200, json=OK)), []
    sender(recorder, sleeps).send(["a"])
    assert recorder.texts() == ["a", "a"]
    assert sleeps == [3]


def test_gives_up_after_the_last_retry() -> None:
    recorder, sleeps = Recorder(too_many(1)), []
    with pytest.raises(ts.SendError, match="Too Many Requests: retry after 1"):
        sender(recorder, sleeps).send(["a"])
    assert len(recorder.requests) == ts.MAX_RETRIES + 1
    assert sleeps == [1] * ts.MAX_RETRIES


def test_does_not_wait_too_long() -> None:
    recorder, sleeps = Recorder(too_many(ts.MAX_RETRY_AFTER_S + 1)), []
    with pytest.raises(ts.SendError, match="Too Many Requests"):
        sender(recorder, sleeps).send(["a"])
    assert len(recorder.requests) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(502, text="<html>Bad Gateway</html>"), "HTTP 502"),
        (httpx.Response(401, json={"ok": False, "description": "Unauthorized"}), "Unauthorized"),
    ],
)
def test_reports_http_failures(response: httpx.Response, reason: str) -> None:
    with pytest.raises(ts.SendError, match=f"Telegram refused the message: {reason}$"):
        sender(Recorder(response)).send(["a"])


def test_rejects_a_success_status_without_ok() -> None:
    with pytest.raises(ts.SendError, match=r"unexpected response \(HTTP 200\)"):
        sender(Recorder(httpx.Response(200, text="not json"))).send(["a"])


def test_the_token_never_appears_in_errors() -> None:
    def fail() -> httpx.Response:
        # httpx puts the request URL, token included, into errors like this one.
        raise httpx.ConnectError(f"cannot connect to {SENT_URL}")

    with pytest.raises(ts.SendError) as exc:
        sender(Recorder(fail)).send(["a"])

    assert TOKEN not in str(exc.value)
    assert f"bot{ts.REDACTED}/sendMessage" in str(exc.value)
    # The original exception is not chained, so a traceback cannot show the URL either.
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__


def test_the_token_is_redacted_from_telegram_descriptions() -> None:
    body = {"ok": False, "description": f"Not Found: bot{TOKEN}"}
    with pytest.raises(ts.SendError) as exc:
        sender(Recorder(httpx.Response(404, json=body))).send(["a"])
    assert TOKEN not in str(exc.value)


def test_send_closes_its_own_client(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    client = recorder.client()
    monkeypatch.setattr(ts.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(ts.time, "sleep", lambda seconds: None)
    ts.send(["a"], CREDENTIALS)
    assert recorder.texts() == ["a"]
    assert client.is_closed


# --- the `send` command -----------------------------------------------------------------


@pytest.fixture
def no_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No Telegram settings from the real environment or `.env`; returns an empty dir."""
    monkeypatch.delenv(ts.TOKEN_VAR, raising=False)
    monkeypatch.delenv(ts.CHAT_ID_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def no_client() -> httpx.Client:
    raise AssertionError("a dry run must not create an HTTP client")


def test_send_dry_run_prints_the_messages_without_settings_or_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_settings: Path
) -> None:
    monkeypatch.setattr(cli, "_telegram_client", no_client)
    monkeypatch.setattr("sys.stdin", io.StringIO("7. Чего ожидать\nДождь."))

    assert main(["send", "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "--- message 1/1, 26 chars ---\n*7\\. Чего ожидать*\nДождь\\.\n"
    assert captured.err == "dry run: 1 message(s) not sent\n"


def test_send_posts_the_formatted_digest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_settings: Path
) -> None:
    (no_settings / ".env").write_text(
        f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT_ID}\n", encoding="utf-8"
    )
    digest = no_settings / "digest.txt"
    digest.write_text("1. Раунд\nГонка в 21:00.\n\n6. Погода\nСухо.\n", encoding="utf-8")
    recorder = Recorder()
    monkeypatch.setattr(cli, "_telegram_client", recorder.client)

    assert main(["send", "--input", str(digest)]) == 0
    assert recorder.texts() == ["*1\\. Раунд*\nГонка в 21:00\\.\n\n*6\\. Погода*\nСухо\\."]
    assert [str(r.url) for r in recorder.requests] == [SENT_URL]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "sent 1 message(s)\n"


def test_send_reads_settings_from_the_given_env_file(
    monkeypatch: pytest.MonkeyPatch, no_settings: Path
) -> None:
    env_file = no_settings / "telegram.env"
    env_file.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT_ID}\n")
    recorder = Recorder()
    monkeypatch.setattr(cli, "_telegram_client", recorder.client)
    monkeypatch.setattr("sys.stdin", io.StringIO("Дождь."))

    assert main(["send", "--env-file", str(env_file)]) == 0
    assert len(recorder.requests) == 1


def test_send_without_settings_fails_before_any_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_settings: Path
) -> None:
    monkeypatch.setattr(cli, "_telegram_client", no_client)
    monkeypatch.setattr("sys.stdin", io.StringIO("Дождь."))

    assert main(["send"]) == 1
    assert capsys.readouterr().err == (
        "error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID not set (in the environment or .env)\n"
    )


def test_send_reports_a_refusal_without_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_settings: Path
) -> None:
    monkeypatch.setenv(ts.TOKEN_VAR, TOKEN)
    monkeypatch.setenv(ts.CHAT_ID_VAR, CHAT_ID)
    monkeypatch.setattr(
        cli, "_telegram_client", Recorder(httpx.Response(400, json=PARSE_ERROR)).client
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("Дождь."))

    assert main(["send"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: message 1 of 1 not sent (0 sent before it): ")
    assert TOKEN not in captured.out + captured.err


def test_send_rejects_an_empty_digest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_settings: Path
) -> None:
    monkeypatch.setattr(cli, "_telegram_client", no_client)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n\n"))
    assert main(["send"]) == 1
    assert capsys.readouterr().err == "error: the digest is empty\n"

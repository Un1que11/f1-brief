"""Sending the digest messages (F14) to a Telegram chat through the Bot API.

Docs: https://core.telegram.org/bots/api#sendmessage. The bot token and the chat id come from
the environment (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), or from a `.env` file that is never
committed; a variable set in the environment wins over the file. The token is part of every
Bot API URL, so it is redacted from every error this module raises, and the original
exceptions (whose text may hold the URL) are not chained.
"""

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

API_URL = "https://api.telegram.org"
TOKEN_VAR = "TELEGRAM_BOT_TOKEN"
CHAT_ID_VAR = "TELEGRAM_CHAT_ID"
DEFAULT_ENV_FILE = ".env"
REDACTED = "<redacted>"

# Telegram allows about one message per second in a chat; a short pause keeps a multi-part
# digest in order and clear of flood limits.
PAUSE_S = 1.0
# How often a message is retried after "429 Too Many Requests", and the longest wait accepted.
MAX_RETRIES = 2
MAX_RETRY_AFTER_S = 60


class SendError(Exception):
    """The digest could not be sent (or the Telegram settings are missing)."""


@dataclass(frozen=True)
class Credentials:
    token: str = field(repr=False)
    chat_id: str = field(repr=False)


def read_env_file(path: str | Path) -> dict[str, str]:
    """`KEY=VALUE` lines of a `.env` file: blank lines and `#` comments are skipped, an
    `export ` prefix and quotes around the value are removed. A missing file is empty."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SendError(f"cannot read {path}: {exc.strerror or exc}") from None
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_credentials(
    env: Mapping[str, str] | None = None, env_file: str | Path = DEFAULT_ENV_FILE
) -> Credentials:
    """The token and chat id from `env` (default: `os.environ`), falling back to `env_file`.
    Errors name the missing variables, never their values."""
    env = os.environ if env is None else env
    from_file = read_env_file(env_file)
    found = {
        var: (env.get(var) or from_file.get(var) or "").strip() for var in (TOKEN_VAR, CHAT_ID_VAR)
    }
    missing = [var for var, value in found.items() if not value]
    if missing:
        raise SendError(f"{' and '.join(missing)} not set (in the environment or {env_file})")
    return Credentials(token=found[TOKEN_VAR], chat_id=found[CHAT_ID_VAR])


def redact(text: str, token: str) -> str:
    return text.replace(token, REDACTED) if token else text


def _describe(response: httpx.Response) -> tuple[str, int | None]:
    """Telegram's error description and, for 429, how many seconds to wait."""
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return f"HTTP {response.status_code}", None
    description = str(body.get("description") or f"HTTP {response.status_code}")
    parameters = body.get("parameters")
    retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
    return description, retry_after if isinstance(retry_after, int) else None


@dataclass
class Sender:
    """Sends MarkdownV2 messages to one chat, in order, stopping at the first failure."""

    credentials: Credentials
    client: httpx.Client
    sleep: Callable[[float], None] = time.sleep
    pause: float = PAUSE_S

    def _url(self) -> str:
        return f"{API_URL}/bot{self.credentials.token}/sendMessage"

    def _send_one(self, text: str) -> None:
        payload = {
            "chat_id": self.credentials.chat_id,
            "text": text,
            "parse_mode": "MarkdownV2",
            # News links would each pull in a preview card; the digest reads better without.
            "link_preview_options": {"is_disabled": True},
        }
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self.client.post(self._url(), json=payload)
            except httpx.HTTPError as exc:
                reason = redact(str(exc) or type(exc).__name__, self.credentials.token)
                raise SendError(f"request failed: {reason}") from None
            if response.is_success:
                try:
                    ok = response.json().get("ok") is True
                except (ValueError, AttributeError):
                    ok = False
                if ok:
                    return
                raise SendError(f"unexpected response (HTTP {response.status_code})")
            description, retry_after = _describe(response)
            description = redact(description, self.credentials.token)
            if (
                response.status_code == 429
                and retry_after is not None
                and retry_after <= MAX_RETRY_AFTER_S
                and attempt < MAX_RETRIES
            ):
                self.sleep(retry_after)
                continue
            raise SendError(f"Telegram refused the message: {description}")

    def send(self, messages: Sequence[str]) -> None:
        if not messages:
            raise SendError("nothing to send")
        for i, text in enumerate(messages):
            if i:
                self.sleep(self.pause)
            try:
                self._send_one(text)
            except SendError as exc:
                raise SendError(
                    f"message {i + 1} of {len(messages)} not sent ({i} sent before it): {exc}"
                ) from None


def send(
    messages: Sequence[str], credentials: Credentials, client: httpx.Client | None = None
) -> None:
    """Sends `messages` to the chat in `credentials`."""
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        Sender(credentials, client).send(messages)
    finally:
        if owns_client:
            client.close()

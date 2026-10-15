"""Minimal read-only client for the OpenF1 API (https://openf1.org), historical data only."""

import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx

BASE_URL = "https://api.openf1.org/v1"
SOURCE_NAME = "OpenF1"
# OpenF1 answers 429 when called too quickly; back off and retry a few times.
RETRY_DELAYS_S = (1.0, 2.0, 4.0)


class OpenF1Error(Exception):
    """An OpenF1 request failed or returned something unexpected."""


def get_rows(
    endpoint: str,
    params: dict[str, Any],
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """GETs `{BASE_URL}/{endpoint}` and returns the list of rows.

    Numbers with a fraction are parsed as Decimal (lap times stay exact).
    """
    url = f"{BASE_URL}/{endpoint}"
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        for delay in (*RETRY_DELAYS_S, None):
            response = client.get(url, params=params)
            if response.status_code == 429 and delay is not None:
                sleep(delay)
                continue
            response.raise_for_status()
            rows = response.json(parse_float=Decimal)
            break
    except httpx.HTTPError as exc:
        raise OpenF1Error(f"failed to fetch {url}: {exc}") from exc
    except ValueError as exc:
        raise OpenF1Error(f"invalid JSON from {url}") from exc
    finally:
        if owns_client:
            client.close()
    if not isinstance(rows, list):
        raise OpenF1Error(f"unexpected response shape from {url}")
    return rows

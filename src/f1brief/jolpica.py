"""Minimal read-only client for the Jolpica-F1 API (Ergast-compatible)."""

from typing import Any

import httpx

BASE_URL = "https://api.jolpi.ca/ergast/f1"
SOURCE_NAME = "Jolpica-F1"
# Larger than any season's race count or grid, so one page always holds everything.
PAGE_LIMIT = 100


class JolpicaError(Exception):
    """A Jolpica request failed or returned something unexpected."""


def get_json(path: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """GETs `{BASE_URL}/{path}.json` and returns the decoded body."""
    url = f"{BASE_URL}/{path}.json"
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        response = client.get(url, params={"limit": PAGE_LIMIT})
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        raise JolpicaError(f"failed to fetch {url}: {exc}") from exc
    except ValueError as exc:
        raise JolpicaError(f"invalid JSON from {url}") from exc
    finally:
        if owns_client:
            client.close()
    if not isinstance(body, dict):
        raise JolpicaError(f"unexpected response shape from {url}")
    return body

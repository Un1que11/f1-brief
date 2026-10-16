"""A mock transport that serves every recorded fixture by URL, for the facts tests."""

import json
from collections.abc import Callable
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures"


def _json(relative: str) -> bytes:
    return (FIXTURES / relative).read_bytes()


JOLPICA = {
    "current": "schedule_2026",
    "current/driverStandings": "driver_standings_2026_r17",
    "current/constructorStandings": "constructor_standings_2026_r17",
    "2026/16/driverStandings": "driver_standings_2026_r16",
    "2026/16/constructorStandings": "constructor_standings_2026_r16",
    "current/constructors/mclaren/results": "mclaren_results_2026",
    "current/constructors/mclaren/qualifying": "mclaren_qualifying_2026",
    "current/drivers/russell/results": "russell_results_2026",
    "current/drivers/ocon/results": "ocon_results_2026",
    "2026/constructors/mercedes/results": "mercedes_results_2026",
    "2026/constructors/mercedes/qualifying": "mercedes_qualifying_2026",
    "2026/constructors/haas/results": "haas_results_2026",
    "2026/constructors/haas/qualifying": "haas_qualifying_2026",
    "circuits/americas/races": "americas_races",
    "2023/18/results": "results_2023_americas",
    "2023/18/qualifying": "qualifying_2023_americas",
    "2024/19/results": "results_2024_americas",
    "2024/19/qualifying": "qualifying_2024_americas",
    "2025/19/results": "results_2025_americas",
    "2025/19/qualifying": "qualifying_2025_americas",
}
FEEDS = {
    "https://www.formula1.com/en/latest/all.xml": "official_like.xml",
    "https://www.motorsport.com/rss/f1/news/": "motorsport_like.xml",
    "https://feeds.bbci.co.uk/sport/formula1/rss.xml": "bbc_like.xml",
    # Autosport and RaceFans have no fixture: they answer 404 and count as failed feeds.
}

Override = Callable[[httpx.Request], httpx.Response | None]


def route(request: httpx.Request) -> httpx.Response:
    url = request.url
    if url.host == "api.jolpi.ca":
        name = JOLPICA.get(url.path.removeprefix("/ergast/f1/").removesuffix(".json"))
        if name:
            return httpx.Response(200, content=_json(f"jolpica/{name}.json"))
    elif url.host == "api.openf1.org":
        endpoint = url.path.rsplit("/", 1)[-1]
        if endpoint == "sessions" and url.params.get("year") == "2026":
            return httpx.Response(200, content=_json("openf1/sessions_2026_race.json"))
        if url.params.get("session_key") == "11388":
            return httpx.Response(200, content=_json(f"openf1/{endpoint}_11388.json"))
    elif url.host == "api.open-meteo.com":
        return httpx.Response(200, content=_json("openmeteo/austin_2026-10-23_25.json"))
    else:
        feed = FEEDS.get(str(url))
        if feed:
            return httpx.Response(200, content=(FIXTURES / "rss" / feed).read_bytes())
    return httpx.Response(404, json={"error": "no fixture"})


def client(seen: list[str], override: Override | None = None) -> httpx.Client:
    """A client answering from fixtures; `override` may answer first. Every URL goes to `seen`."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if override is not None and (response := override(request)) is not None:
            return response
        return route(request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def dumps(document: object) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2)

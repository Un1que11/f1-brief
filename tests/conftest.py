import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


@pytest.fixture
def schedule_2026() -> dict[str, Any]:
    """Jolpica response for GET /ergast/f1/2026.json?limit=100, recorded 2026-10-15."""
    return load_fixture("jolpica/schedule_2026.json")


@pytest.fixture
def driver_standings_r16() -> dict[str, Any]:
    """GET /ergast/f1/2026/16/driverStandings.json, recorded 2026-10-15."""
    return load_fixture("jolpica/driver_standings_2026_r16.json")


@pytest.fixture
def driver_standings_r17() -> dict[str, Any]:
    return load_fixture("jolpica/driver_standings_2026_r17.json")


@pytest.fixture
def constructor_standings_r16() -> dict[str, Any]:
    return load_fixture("jolpica/constructor_standings_2026_r16.json")


@pytest.fixture
def constructor_standings_r17() -> dict[str, Any]:
    return load_fixture("jolpica/constructor_standings_2026_r17.json")


@pytest.fixture
def mclaren_2026() -> tuple[dict[str, Any], dict[str, Any]]:
    """GET /2026/constructors/mclaren/{results,qualifying}.json, recorded 2026-10-15 (R1-R17)."""
    return (
        load_fixture("jolpica/mclaren_results_2026.json"),
        load_fixture("jolpica/mclaren_qualifying_2026.json"),
    )


@pytest.fixture
def mclaren_2025() -> tuple[dict[str, Any], dict[str, Any]]:
    """Same for the full 2025 season (R1-R24)."""
    return (
        load_fixture("jolpica/mclaren_results_2025.json"),
        load_fixture("jolpica/mclaren_qualifying_2025.json"),
    )

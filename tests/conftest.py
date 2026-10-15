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

"""The CI workflow runs exactly the checks CLAUDE.md requires before a PR is opened."""

from pathlib import Path

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"


def _run_lines() -> list[str]:
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    return [line.strip().removeprefix("run:").strip() for line in lines if "run:" in line]


def test_workflow_runs_the_required_checks():
    runs = _run_lines()
    assert "uv run ruff check" in runs
    assert "uv run ruff format --check" in runs
    assert "uv run pytest" in runs


def test_workflow_installs_from_the_lockfile():
    assert "uv sync --locked" in _run_lines()


def test_workflow_runs_on_pull_requests_and_main():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request:" in text
    assert "branches: [main]" in text


def test_workflow_uses_no_secrets():
    # Tests run on recorded fixtures only; CI must never need the Telegram token.
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")

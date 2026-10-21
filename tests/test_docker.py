"""The Docker image (F37): what the Dockerfile and .dockerignore promise, checked as text.

The image itself is built (both platforms) and smoke-tested by `.github/workflows/docker.yml`;
these tests need no Docker and no network.
"""

import fnmatch
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
WORKFLOW = ROOT / ".github" / "workflows" / "docker.yml"


def _instructions() -> list[str]:
    """Dockerfile instructions with continuation lines joined, comments dropped."""
    joined: list[str] = []
    current = ""
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        joined.append(current + line)
        current = ""
    return joined


def _final_stage() -> list[str]:
    instructions = _instructions()
    last_from = max(i for i, line in enumerate(instructions) if line.startswith("FROM "))
    return instructions[last_from:]


def _index(stage: list[str], needle: str) -> int:
    return next(i for i, line in enumerate(stage) if needle in line)


def test_runtime_is_python_312_slim() -> None:
    assert "ARG PYTHON_IMAGE=python:3.12-slim" in DOCKERFILE.read_text(encoding="utf-8")
    assert _final_stage()[0] == "FROM ${PYTHON_IMAGE}"


def test_dependencies_are_a_cached_layer_from_the_lockfile() -> None:
    stage = _final_stage()
    deps = _index(stage, "--no-install-project")
    assert "uv sync --locked --no-dev" in stage[deps]
    assert "source=uv.lock" in stage[deps]
    assert "type=cache" in stage[deps]
    # The source code arrives only after the dependency layer, so code changes reuse it.
    assert deps < _index(stage, "COPY src")


def test_versions_are_pinned() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^ARG UV_VERSION=\d+\.\d+\.\d+$", text, re.MULTILINE)
    assert re.search(r"^ARG CLAUDE_CODE_VERSION=\d+\.\d+\.\d+$", text, re.MULTILINE)
    assert '"@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"' in text


def test_claude_is_installed_and_checked() -> None:
    instructions = _instructions()
    install = next(line for line in instructions if "npm install" in line)
    # The npm package leaves a stub that exits 1 when its native binary is missing.
    assert "/claude --version" in install
    assert "COPY --from=claude /claude /usr/local/bin/claude" in _final_stage()


def test_warsaw_time_zone() -> None:
    stage = " ".join(_final_stage())
    assert "TZ=Europe/Warsaw" in stage
    assert "tzdata" in stage


def test_runs_as_a_non_root_user_in_data() -> None:
    stage = _final_stage()
    user = _index(stage, "USER ")
    assert stage[user] == "USER f1brief"
    assert "useradd" in stage[_index(stage, "useradd")]
    assert stage[user + 1 :][0] == "WORKDIR /data"
    assert stage[-2:] == ['ENTRYPOINT ["f1brief"]', 'CMD ["--help"]']


def test_claude_auth_check_is_switched_on_and_autoupdate_off() -> None:
    stage = " ".join(_final_stage())
    assert "F1BRIEF_CONTAINER=1" in stage
    assert "DISABLE_AUTOUPDATER=1" in stage
    assert "CLAUDE_CONFIG_DIR=/home/f1brief/.claude" in stage


def test_no_secrets_or_state_in_the_image() -> None:
    for line in _instructions():
        if line.startswith(("ENV ", "ARG ")):
            assert not re.search(r"(TOKEN|API_KEY|CHAT_ID)\s*=", line), line
        if line.startswith(("COPY ", "ADD ")):
            assert ".env" not in line and "state.db" not in line, line


def _included(path: str) -> bool:
    """Whether `path` reaches the build context under .dockerignore (last match wins; a
    pattern also matches everything under a matching directory)."""
    parts = path.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    included = True
    for raw in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pattern = line.removeprefix("!").rstrip("/")
        names = [pattern, pattern.removeprefix("**/")]
        if any(fnmatch.fnmatchcase(c, p) for c in candidates for p in names) or any(
            pattern.startswith("**/") and fnmatch.fnmatchcase(part, pattern[3:]) for part in parts
        ):
            included = negate
    return included


def test_dockerignore_keeps_secrets_state_and_caches_out() -> None:
    for path in [
        ".env",
        ".env.local",
        "src/.env",
        ".venv/bin/python",
        "state.db",
        "src/f1brief/__pycache__/cli.cpython-312.pyc",
        ".ruff_cache/x",
        ".pytest_cache/x",
        ".git/HEAD",
        "tests/test_cli.py",
        "config.toml",
        "archive.html",
    ]:
        assert not _included(path), path


def test_dockerignore_lets_the_build_inputs_in() -> None:
    for path in [
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "story_seeds.toml",
        "src/f1brief/cli.py",
        "src/f1brief/digest_prompt.md",
    ]:
        assert _included(path), path


def test_ci_builds_both_platforms_without_secrets() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--platform linux/arm64,linux/amd64" in text
    assert "secrets." not in text
    assert "push: true" not in text and "--push" not in text

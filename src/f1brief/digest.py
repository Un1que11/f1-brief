"""The digest, written by headless Claude Code (`claude -p`) from the facts document alone.

Claude answers with structured JSON (sections of items, each with its sources); `cards` checks
the sources against the facts document and renders the result for Telegram (F41).

The prompt is a fixed template (`prompts/<language>/digest.md`; `summary.md` for the
off-season monthly summary; `qualifying.md` for the message after qualifying) with the facts
JSON (F12, F18, F19) inserted, and nothing else: no names, results or dates live in the
template. Each language (F29) has its own copy of every template; the copies differ only in
the output language, and a test keeps their rules and sections the same. Claude runs without
tools, MCP servers or user settings, from an empty directory, so it cannot read or fetch
anything besides the prompt either.
"""

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

FACTS_MARKER = "{{FACTS_JSON}}"
DIGEST_TEMPLATE = "digest"
SUMMARY_TEMPLATE = "summary"
QUALIFYING_TEMPLATE = "qualifying"
TEMPLATES = (DIGEST_TEMPLATE, SUMMARY_TEMPLATE, QUALIFYING_TEMPLATE)
DEFAULT_LANGUAGE = "ru"
TIMEOUT_S = 600.0

# Replaces Claude Code's default (coding agent) system prompt. Fact-free, like the template.
SYSTEM_PROMPT = (
    "You write a pre-race-weekend F1 digest strictly from the facts document in the "
    "user's message. You have no tools and use no other knowledge."
)
SUMMARY_SYSTEM_PROMPT = (
    "You write a monthly off-season F1 summary strictly from the facts document in the "
    "user's message. You have no tools and use no other knowledge."
)
QUALIFYING_SYSTEM_PROMPT = (
    "You write a short F1 message after qualifying strictly from the facts document in the "
    "user's message. You have no tools and use no other knowledge."
)


class GenerationError(Exception):
    """The digest text could not be generated."""


# Set by the Docker image (F37). Outside a container `claude` may be logged in through the
# macOS Keychain, which cannot be seen from here, so the check below only runs inside one.
CONTAINER_FLAG = "F1BRIEF_CONTAINER"
# Environment credentials Claude Code reads in `-p` mode (see its authentication docs).
CREDENTIAL_VARIABLES = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
MISSING_AUTH = (
    "claude has no credentials in this container: set CLAUDE_CODE_OAUTH_TOKEN to a token "
    "from `claude setup-token` (run it once on the Mac) or ANTHROPIC_API_KEY in the "
    "container's environment (README, section Docker)"
)


def missing_container_auth(environ: Mapping[str, str]) -> str | None:
    """Why `claude -p` cannot authenticate in the container, or None when it can (or when
    not in a container). A login stored in the container's config dir counts as well."""
    if not environ.get(CONTAINER_FLAG):
        return None
    if any(environ.get(name, "").strip() for name in CREDENTIAL_VARIABLES):
        return None
    config_dir = environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        home = environ.get("HOME")
        config_dir = str((Path(home) if home else Path.home()) / ".claude")
    if (Path(config_dir) / ".credentials.json").is_file():
        return None
    return MISSING_AUTH


def template(name: str = DIGEST_TEMPLATE, language: str | None = DEFAULT_LANGUAGE) -> str:
    """`prompts/<language>/<name>.md`; with no language, the shared `prompts/<name>.md` (the
    story detection prompt, whose answers are not reader-facing text)."""
    prompts = resources.files("f1brief").joinpath("prompts")
    path = (prompts if language is None else prompts.joinpath(language)).joinpath(f"{name}.md")
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(f"no {name!r} prompt template for language {language!r}") from None


def facts_json(document: dict[str, Any]) -> str:
    """The document as JSON for the prompt. `</` is escaped (`<\\/`, still the same JSON), so a
    news title cannot close the `<facts>` block early."""
    return json.dumps(document, ensure_ascii=False, indent=2).replace("</", "<\\/")


def build_prompt(
    document: dict[str, Any], name: str = DIGEST_TEMPLATE, language: str | None = DEFAULT_LANGUAGE
) -> str:
    text = template(name, language)
    if text.count(FACTS_MARKER) != 1:
        raise ValueError(f"the prompt template must contain {FACTS_MARKER} exactly once")
    return text.replace(FACTS_MARKER, facts_json(document))


class TextModel(Protocol):
    """Turns a prompt into text. Tests pass a fake; the real one is `ClaudeCli`."""

    def __call__(self, prompt: str) -> str: ...


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ClaudeCli:
    """Runs `claude -p` with the prompt on stdin and returns its text output."""

    executable: str = "claude"
    model: str | None = None
    system_prompt: str = SYSTEM_PROMPT
    timeout: float = TIMEOUT_S
    run: Runner | None = None  # subprocess.run, looked up at call time so tests can block it
    environ: Mapping[str, str] | None = None  # os.environ when None

    def command(self) -> list[str]:
        cmd = [
            self.executable,
            "-p",
            "--output-format",
            "text",
            "--system-prompt",
            self.system_prompt,
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "project",
            "--no-session-persistence",
        ]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def __call__(self, prompt: str) -> str:
        problem = missing_container_auth(os.environ if self.environ is None else self.environ)
        if problem is not None:
            raise GenerationError(problem)
        run = self.run or subprocess.run
        # An empty working directory: no project CLAUDE.md or settings to pick up.
        with tempfile.TemporaryDirectory(prefix="f1brief-") as cwd:
            try:
                done = run(
                    self.command(),
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=cwd,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise GenerationError(f"{self.executable} not found on PATH") from exc
            except subprocess.TimeoutExpired as exc:
                message = f"{self.executable} timed out after {self.timeout:g}s"
                raise GenerationError(message) from exc
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip()
            raise GenerationError(f"{self.executable} exited with {done.returncode}: {detail}")
        text = done.stdout.strip()
        if not text:
            raise GenerationError(f"{self.executable} returned no text")
        return text


def generate(
    document: dict[str, Any], model: TextModel | None = None, language: str = DEFAULT_LANGUAGE
) -> str:
    """The digest text, in `language`, for a facts document from `f1brief.facts.collect`."""
    return (model or ClaudeCli())(build_prompt(document, DIGEST_TEMPLATE, language))


def generate_summary(
    document: dict[str, Any], model: TextModel | None = None, language: str = DEFAULT_LANGUAGE
) -> str:
    """The monthly summary text, in `language`, for a document from `f1brief.offseason.collect`."""
    model = model or ClaudeCli(system_prompt=SUMMARY_SYSTEM_PROMPT)
    return model(build_prompt(document, SUMMARY_TEMPLATE, language))


def generate_after_qualifying(
    document: dict[str, Any], model: TextModel | None = None, language: str = DEFAULT_LANGUAGE
) -> str:
    """The message text, in `language`, for a document from `f1brief.after_qualifying.collect`."""
    model = model or ClaudeCli(system_prompt=QUALIFYING_SYSTEM_PROMPT)
    return model(build_prompt(document, QUALIFYING_TEMPLATE, language))

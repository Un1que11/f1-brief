"""The digest text, written by headless Claude Code (`claude -p`) from the facts document alone.

The prompt is a fixed template (`digest_prompt.md`; `summary_prompt.md` for the off-season
monthly summary; `qualifying_prompt.md` for the message after qualifying) with the facts JSON
(F12, F18, F19) inserted, and nothing else: no names, results
or dates live in the template. Claude runs without tools, MCP servers or user settings, from an
empty directory, so it cannot read or fetch anything besides the prompt either.
"""

import json
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from typing import Any, Protocol

FACTS_MARKER = "{{FACTS_JSON}}"
DIGEST_TEMPLATE = "digest_prompt.md"
SUMMARY_TEMPLATE = "summary_prompt.md"
QUALIFYING_TEMPLATE = "qualifying_prompt.md"
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


def template(name: str = DIGEST_TEMPLATE) -> str:
    return resources.files("f1brief").joinpath(name).read_text(encoding="utf-8")


def facts_json(document: dict[str, Any]) -> str:
    """The document as JSON for the prompt. `</` is escaped (`<\\/`, still the same JSON), so a
    news title cannot close the `<facts>` block early."""
    return json.dumps(document, ensure_ascii=False, indent=2).replace("</", "<\\/")


def build_prompt(document: dict[str, Any], name: str = DIGEST_TEMPLATE) -> str:
    text = template(name)
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


def generate(document: dict[str, Any], model: TextModel | None = None) -> str:
    """The digest text for a facts document from `f1brief.facts.collect`."""
    return (model or ClaudeCli())(build_prompt(document))


def generate_summary(document: dict[str, Any], model: TextModel | None = None) -> str:
    """The monthly summary text for a document from `f1brief.offseason.collect`."""
    model = model or ClaudeCli(system_prompt=SUMMARY_SYSTEM_PROMPT)
    return model(build_prompt(document, SUMMARY_TEMPLATE))


def generate_after_qualifying(document: dict[str, Any], model: TextModel | None = None) -> str:
    """The message text for a document from `f1brief.after_qualifying.collect`."""
    model = model or ClaudeCli(system_prompt=QUALIFYING_SYSTEM_PROMPT)
    return model(build_prompt(document, QUALIFYING_TEMPLATE))

"""The launchd job that runs `f1brief run` on the Mac (F17).

`f1brief launchd` writes a LaunchAgent property list for the current checkout. launchd starts
jobs with a bare environment (PATH is only /usr/bin:/bin:/usr/sbin:/sbin and the working
directory is /), so the plist carries the absolute path of `uv`, a PATH that also finds
`claude`, and the checkout as the working directory, where `config.toml`, `.env` and
`state.db` are looked up. The plist holds no secrets: the Telegram settings stay in `.env`.

The job runs every hour, not once a day: `run` itself decides whether anything is due and
exits quickly otherwise, and the message after qualifying is only due between two hours after
qualifying starts and the race start, so a daily run would often miss it. A run missed while
the Mac sleeps is made up once on wake-up.
"""

import plistlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LABEL = "local.f1brief.run"
PLIST_NAME = f"{LABEL}.plist"
DEFAULT_INTERVAL_MINUTES = 60
MAX_INTERVAL_MINUTES = 24 * 60
# Where Homebrew installs on Apple Silicon and on Intel, then launchd's own default PATH.
FALLBACK_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")

Which = Callable[[str], str | None]


class LaunchdError(Exception):
    """The job cannot be set up (a missing tool, a wrong checkout or a bad interval)."""


@dataclass(frozen=True)
class Job:
    """Everything the plist is made from; all paths are absolute."""

    workdir: Path
    uv: Path
    path_dirs: tuple[str, ...]
    log_dir: Path
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES

    @property
    def stdout_log(self) -> Path:
        return self.log_dir / "run.log"

    @property
    def stderr_log(self) -> Path:
        return self.log_dir / "run.err.log"


def default_plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / PLIST_NAME


def default_log_dir(home: Path) -> Path:
    return home / "Library" / "Logs" / "f1brief"


def _search_path(*tool_dirs: Path) -> tuple[str, ...]:
    """The tools' own directories first, then the fallbacks, without duplicates."""
    dirs: list[str] = []
    for entry in (*(str(d) for d in tool_dirs), *FALLBACK_PATH):
        if entry not in dirs:
            dirs.append(entry)
    return tuple(dirs)


def _locate(name: str, given: str | None, which: Which) -> Path:
    found = given if given is not None else which(name)
    if found is None:
        raise LaunchdError(f"`{name}` is not on PATH; pass --{name} with its full path")
    path = Path(found).expanduser()
    if not path.is_absolute():
        raise LaunchdError(f"--{name} must be an absolute path, got {found}")
    if not path.is_file():
        raise LaunchdError(f"{path} does not exist")
    return path


def make_job(
    workdir: Path,
    log_dir: Path,
    *,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    uv: str | None = None,
    claude: str | None = None,
    which: Which = shutil.which,
) -> Job:
    """Check the checkout and find `uv` and `claude` (`run` needs both)."""
    if not 1 <= interval_minutes <= MAX_INTERVAL_MINUTES:
        raise LaunchdError(
            f"the interval must be between 1 and {MAX_INTERVAL_MINUTES} minutes, "
            f"got {interval_minutes}"
        )
    workdir = workdir.expanduser().resolve()
    if not (workdir / "pyproject.toml").is_file():
        raise LaunchdError(f"{workdir} is not the f1-brief checkout (no pyproject.toml)")
    uv_path = _locate("uv", uv, which)
    claude_path = _locate("claude", claude, which)
    return Job(
        workdir=workdir,
        uv=uv_path,
        path_dirs=_search_path(uv_path.parent, claude_path.parent),
        log_dir=log_dir.expanduser().resolve(),
        interval_minutes=interval_minutes,
    )


def plist(job: Job) -> dict[str, Any]:
    """The LaunchAgent definition, as plistlib takes it."""
    return {
        "Label": LABEL,
        "ProgramArguments": [str(job.uv), "run", "--project", str(job.workdir), "f1brief", "run"],
        "WorkingDirectory": str(job.workdir),
        "EnvironmentVariables": {"PATH": ":".join(job.path_dirs), "LANG": "en_US.UTF-8"},
        "StartInterval": job.interval_minutes * 60,
        "RunAtLoad": True,
        "StandardOutPath": str(job.stdout_log),
        "StandardErrorPath": str(job.stderr_log),
    }


def render(job: Job) -> bytes:
    return plistlib.dumps(plist(job), sort_keys=False)


def install_commands(plist_path: Path) -> list[str]:
    """What to run after the plist is written: (re)load the job and start it once now."""
    target = "gui/$(id -u)"
    return [
        f"launchctl bootout {target} {plist_path} 2>/dev/null || true",
        f"launchctl bootstrap {target} {plist_path}",
        f"launchctl print {target}/{LABEL} | head -20",
    ]

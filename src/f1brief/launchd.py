"""The launchd jobs that run `f1brief run` (F17) and keep `f1brief bot` alive (F36) on the Mac.

`f1brief launchd` writes a LaunchAgent property list for the current checkout. launchd starts
jobs with a bare environment (PATH is only /usr/bin:/bin:/usr/sbin:/sbin and the working
directory is /), so the plist carries the absolute path of `uv`, a PATH that also finds
`claude`, and the checkout as the working directory, where `config.toml`, `.env` and
`state.db` are looked up. The plist holds no secrets: the Telegram settings stay in `.env`.

The job runs every hour, not once a day: `run` itself decides whether anything is due and
exits quickly otherwise, and the message after qualifying is only due between two hours after
qualifying starts and the race start, so a daily run would often miss it. A run missed while
the Mac sleeps is made up once on wake-up.

The bot job (`--job bot`) starts the long-polling listener at login and keeps it running:
launchd restarts it whenever it exits, at most once per THROTTLE_SECONDS, so a bad token or a
second listener (both make `bot` exit with an error) cannot spin in a tight loop.
"""

import plistlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JOBS = ("run", "bot")
DEFAULT_JOB = "run"
LABEL = "local.f1brief.run"
PLIST_NAME = f"{LABEL}.plist"
THROTTLE_SECONDS = 30
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
    name: str = DEFAULT_JOB

    @property
    def label(self) -> str:
        return label(self.name)

    @property
    def stdout_log(self) -> Path:
        return self.log_dir / f"{self.name}.log"

    @property
    def stderr_log(self) -> Path:
        return self.log_dir / f"{self.name}.err.log"


def label(job: str = DEFAULT_JOB) -> str:
    return f"local.f1brief.{job}"


def default_plist_path(home: Path, job: str = DEFAULT_JOB) -> Path:
    return home / "Library" / "LaunchAgents" / f"{label(job)}.plist"


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
    name: str = DEFAULT_JOB,
) -> Job:
    """Check the checkout and find `uv` and `claude` (`run` needs both; `bot` only `uv`)."""
    if name not in JOBS:
        raise LaunchdError(f"unknown job {name!r}; expected one of: {', '.join(JOBS)}")
    if not 1 <= interval_minutes <= MAX_INTERVAL_MINUTES:
        raise LaunchdError(
            f"the interval must be between 1 and {MAX_INTERVAL_MINUTES} minutes, "
            f"got {interval_minutes}"
        )
    workdir = workdir.expanduser().resolve()
    if not (workdir / "pyproject.toml").is_file():
        raise LaunchdError(f"{workdir} is not the f1-brief checkout (no pyproject.toml)")
    uv_path = _locate("uv", uv, which)
    tool_dirs = [uv_path.parent]
    if name == "run" or claude is not None:
        tool_dirs.append(_locate("claude", claude, which).parent)
    return Job(
        workdir=workdir,
        uv=uv_path,
        path_dirs=_search_path(*tool_dirs),
        log_dir=log_dir.expanduser().resolve(),
        interval_minutes=interval_minutes,
        name=name,
    )


def plist(job: Job) -> dict[str, Any]:
    """The LaunchAgent definition, as plistlib takes it."""
    data: dict[str, Any] = {
        "Label": job.label,
        "ProgramArguments": [
            str(job.uv),
            "run",
            "--project",
            str(job.workdir),
            "f1brief",
            job.name,
        ],
        "WorkingDirectory": str(job.workdir),
        "EnvironmentVariables": {"PATH": ":".join(job.path_dirs), "LANG": "en_US.UTF-8"},
    }
    if job.name == "bot":
        # Always running: restarted whenever it exits, but not more often than the throttle.
        data["KeepAlive"] = True
        data["ThrottleInterval"] = THROTTLE_SECONDS
    else:
        data["StartInterval"] = job.interval_minutes * 60
    data["RunAtLoad"] = True
    data["StandardOutPath"] = str(job.stdout_log)
    data["StandardErrorPath"] = str(job.stderr_log)
    return data


def render(job: Job) -> bytes:
    return plistlib.dumps(plist(job), sort_keys=False)


def install_commands(plist_path: Path, job: str = DEFAULT_JOB) -> list[str]:
    """What to run after the plist is written: (re)load the job and start it once now."""
    target = "gui/$(id -u)"
    return [
        f"launchctl bootout {target} {plist_path} 2>/dev/null || true",
        f"launchctl bootstrap {target} {plist_path}",
        f"launchctl print {target}/{label(job)} | head -20",
    ]

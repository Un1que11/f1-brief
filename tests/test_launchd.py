"""The LaunchAgent plist for `f1brief run` (F17). Nothing here calls launchctl."""

import plistlib
from pathlib import Path

import pytest

from f1brief import launchd
from f1brief.cli import main


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    workdir = tmp_path / "f1-brief"
    workdir.mkdir()
    (workdir / "pyproject.toml").write_text('[project]\nname = "f1brief"\n', encoding="utf-8")
    return workdir


@pytest.fixture
def tools(tmp_path: Path) -> dict[str, Path]:
    """Fake `uv` and `claude` executables in the places a Mac usually has them."""
    uv = tmp_path / "home" / ".local" / "bin" / "uv"
    claude = tmp_path / "home" / ".claude" / "local" / "claude"
    for tool in (uv, claude):
        tool.parent.mkdir(parents=True)
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
    return {"uv": uv, "claude": claude}


def _which(tools: dict[str, Path]):
    return lambda name: str(tools[name]) if name in tools else None


def test_plist_runs_f1brief_run_from_the_checkout(
    checkout: Path, tools: dict[str, Path], tmp_path: Path
) -> None:
    job = launchd.make_job(checkout, tmp_path / "logs", which=_which(tools))

    data = plistlib.loads(launchd.render(job))

    assert data["Label"] == "local.f1brief.run"
    assert data["ProgramArguments"] == [
        str(tools["uv"]),
        "run",
        "--project",
        str(checkout),
        "f1brief",
        "run",
    ]
    # config.toml, .env and state.db are looked up relative to the working directory.
    assert data["WorkingDirectory"] == str(checkout)
    assert data["RunAtLoad"] is True
    assert data["StandardOutPath"] == str(tmp_path / "logs" / "run.log")
    assert data["StandardErrorPath"] == str(tmp_path / "logs" / "run.err.log")


def test_job_runs_hourly_by_default(checkout: Path, tools: dict[str, Path], tmp_path: Path) -> None:
    job = launchd.make_job(checkout, tmp_path, which=_which(tools))

    # 60 minutes x 60 seconds.
    assert launchd.plist(job)["StartInterval"] == 3600


def test_interval_is_given_in_minutes(
    checkout: Path, tools: dict[str, Path], tmp_path: Path
) -> None:
    job = launchd.make_job(checkout, tmp_path, interval_minutes=30, which=_which(tools))

    assert launchd.plist(job)["StartInterval"] == 1800


@pytest.mark.parametrize("minutes", [0, -5, 1441])
def test_interval_out_of_range_is_refused(
    checkout: Path, tools: dict[str, Path], tmp_path: Path, minutes: int
) -> None:
    with pytest.raises(launchd.LaunchdError, match="between 1 and 1440"):
        launchd.make_job(checkout, tmp_path, interval_minutes=minutes, which=_which(tools))


def test_path_finds_uv_and_claude_before_the_system_dirs(
    checkout: Path, tools: dict[str, Path], tmp_path: Path
) -> None:
    job = launchd.make_job(checkout, tmp_path, which=_which(tools))

    path = launchd.plist(job)["EnvironmentVariables"]["PATH"].split(":")

    assert path == [
        str(tools["uv"].parent),
        str(tools["claude"].parent),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]


def test_path_has_no_duplicates_when_tools_share_a_dir(checkout: Path, tmp_path: Path) -> None:
    bindir = tmp_path / "opt" / "homebrew" / "bin"
    bindir.mkdir(parents=True)
    for name in ("uv", "claude"):
        (bindir / name).write_text("", encoding="utf-8")
    job = launchd.make_job(checkout, tmp_path, which=lambda name: str(bindir / name))

    path = launchd.plist(job)["EnvironmentVariables"]["PATH"].split(":")

    assert path.count(str(bindir)) == 1
    assert len(path) == len(set(path))


def test_plist_holds_no_secrets(checkout: Path, tools: dict[str, Path], tmp_path: Path) -> None:
    job = launchd.make_job(checkout, tmp_path, which=_which(tools))

    text = launchd.render(job).decode("utf-8")

    assert "TELEGRAM" not in text
    assert set(launchd.plist(job)["EnvironmentVariables"]) == {"PATH", "LANG"}


def test_explicit_tool_paths_win_over_path_lookup(
    checkout: Path, tools: dict[str, Path], tmp_path: Path
) -> None:
    job = launchd.make_job(
        checkout,
        tmp_path,
        uv=str(tools["uv"]),
        claude=str(tools["claude"]),
        which=lambda name: None,
    )

    assert job.uv == tools["uv"]


def test_missing_claude_is_an_error(checkout: Path, tools: dict[str, Path], tmp_path: Path) -> None:
    with pytest.raises(launchd.LaunchdError, match="`claude` is not on PATH; pass --claude"):
        launchd.make_job(checkout, tmp_path, which=_which({"uv": tools["uv"]}))


def test_relative_tool_path_is_an_error(checkout: Path, tmp_path: Path) -> None:
    with pytest.raises(launchd.LaunchdError, match="--uv must be an absolute path"):
        launchd.make_job(checkout, tmp_path, uv="bin/uv", claude="/bin/sh", which=lambda n: None)


def test_tool_path_that_does_not_exist_is_an_error(checkout: Path, tmp_path: Path) -> None:
    with pytest.raises(launchd.LaunchdError, match="does not exist"):
        launchd.make_job(
            checkout, tmp_path, uv=str(tmp_path / "nope"), claude="/bin/sh", which=lambda n: None
        )


def test_workdir_must_be_the_checkout(tools: dict[str, Path], tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(launchd.LaunchdError, match=r"no pyproject\.toml"):
        launchd.make_job(empty, tmp_path, which=_which(tools))


def test_default_locations_are_in_the_users_library() -> None:
    home = Path("/Users/someone")

    assert launchd.default_plist_path(home) == Path(
        "/Users/someone/Library/LaunchAgents/local.f1brief.run.plist"
    )
    assert launchd.default_log_dir(home) == Path("/Users/someone/Library/Logs/f1brief")


def test_install_commands_reload_the_job() -> None:
    commands = launchd.install_commands(Path("/Users/someone/Library/LaunchAgents/x.plist"))

    assert commands[0].startswith("launchctl bootout gui/$(id -u) ")
    assert (
        commands[1]
        == "launchctl bootstrap gui/$(id -u) /Users/someone/Library/LaunchAgents/x.plist"
    )


def test_cli_prints_the_plist(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    checkout: Path,
    tools: dict[str, Path],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(launchd, "make_job", _with_which(launchd.make_job, _which(tools)))

    code = main(["launchd", "--workdir", str(checkout), "--log-dir", str(tmp_path / "logs")])

    assert code == 0
    data = plistlib.loads(capsys.readouterr().out.encode("utf-8"))
    assert data["WorkingDirectory"] == str(checkout)
    # Printing writes nothing.
    assert not (tmp_path / "logs").exists()


def test_cli_writes_the_plist_and_the_log_dir(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    checkout: Path,
    tools: dict[str, Path],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(launchd, "make_job", _with_which(launchd.make_job, _which(tools)))
    output = tmp_path / "LaunchAgents" / "local.f1brief.run.plist"

    code = main(
        [
            "launchd",
            "--workdir",
            str(checkout),
            "--log-dir",
            str(tmp_path / "logs"),
            "--interval",
            "30",
            "--output",
            str(output),
        ]
    )

    assert code == 0
    assert plistlib.loads(output.read_bytes())["StartInterval"] == 1800
    assert (tmp_path / "logs").is_dir()
    out = capsys.readouterr().out
    assert f"wrote {output}" in out
    assert f"launchctl bootstrap gui/$(id -u) {output}" in out


def test_cli_reports_a_missing_tool(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], checkout: Path
) -> None:
    monkeypatch.setattr(launchd, "make_job", _with_which(launchd.make_job, lambda name: None))

    assert main(["launchd", "--workdir", str(checkout)]) == 2
    assert capsys.readouterr().err == "error: `uv` is not on PATH; pass --uv with its full path\n"


def _with_which(make_job, which):
    def wrapped(*args, **kwargs):
        return make_job(*args, which=which, **kwargs)

    return wrapped


# --- F36: the always-on bot listener ---


def test_bot_plist_keeps_the_listener_running(
    checkout: Path, tools: dict[str, Path], tmp_path: Path
) -> None:
    job = launchd.make_job(checkout, tmp_path / "logs", which=_which(tools), name="bot")

    data = plistlib.loads(launchd.render(job))

    assert data["Label"] == "local.f1brief.bot"
    assert data["ProgramArguments"][-2:] == ["f1brief", "bot"]
    assert data["WorkingDirectory"] == str(checkout)
    assert data["KeepAlive"] is True
    assert data["RunAtLoad"] is True
    # Restarted at most every 30 s, so a bad token cannot loop fast.
    assert data["ThrottleInterval"] == 30
    assert "StartInterval" not in data
    assert data["StandardOutPath"] == str(tmp_path / "logs" / "bot.log")
    assert data["StandardErrorPath"] == str(tmp_path / "logs" / "bot.err.log")


def test_run_plist_has_no_keep_alive(
    checkout: Path, tools: dict[str, Path], tmp_path: Path
) -> None:
    data = launchd.plist(launchd.make_job(checkout, tmp_path, which=_which(tools)))

    assert "KeepAlive" not in data
    assert "ThrottleInterval" not in data


def test_bot_job_does_not_need_claude(
    checkout: Path, tools: dict[str, Path], tmp_path: Path
) -> None:
    job = launchd.make_job(checkout, tmp_path, which=_which({"uv": tools["uv"]}), name="bot")

    assert job.path_dirs[0] == str(tools["uv"].parent)
    assert str(tools["claude"].parent) not in job.path_dirs


def test_unknown_job_is_refused(checkout: Path, tools: dict[str, Path], tmp_path: Path) -> None:
    with pytest.raises(launchd.LaunchdError, match="unknown job 'web'"):
        launchd.make_job(checkout, tmp_path, which=_which(tools), name="web")


def test_default_plist_path_per_job() -> None:
    home = Path("/Users/someone")

    assert launchd.default_plist_path(home, "bot") == Path(
        "/Users/someone/Library/LaunchAgents/local.f1brief.bot.plist"
    )


def test_bot_install_commands_print_the_bot_label() -> None:
    commands = launchd.install_commands(Path("/tmp/x.plist"), "bot")

    assert commands[2] == "launchctl print gui/$(id -u)/local.f1brief.bot | head -20"


def test_cli_writes_the_bot_plist(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    checkout: Path,
    tools: dict[str, Path],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(launchd, "make_job", _with_which(launchd.make_job, _which(tools)))
    out = tmp_path / "agents" / "local.f1brief.bot.plist"

    code = main(
        [
            "launchd",
            "--job",
            "bot",
            "--workdir",
            str(checkout),
            "--log-dir",
            str(tmp_path / "logs"),
            "--output",
            str(out),
        ]
    )

    assert code == 0
    data = plistlib.loads(out.read_bytes())
    assert data["Label"] == "local.f1brief.bot"
    assert "local.f1brief.bot" in capsys.readouterr().out

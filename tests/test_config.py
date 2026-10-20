"""`config.toml`: loading, defaults, validation, and how the CLI uses it."""

from pathlib import Path

import pytest

from f1brief import cli, config
from f1brief.cli import main
from f1brief.config import Config, ConfigError, Favourite

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_checked_in_config_matches_the_built_in_defaults() -> None:
    # The defaults are what runs when the file is absent; they must not drift from the file.
    assert config.load(REPO_CONFIG) == Config()


def test_checked_in_config_has_the_favourites_from_claude_md() -> None:
    settings = config.load(REPO_CONFIG)
    assert settings.favourites.team_ids == ("mclaren", "haas")
    assert settings.favourites.driver_ids == ("russell", "ocon", "bearman")
    assert [d.track_future for d in settings.favourites.drivers] == [False, True, False]
    assert settings.favourites.drivers[2] == Favourite("bearman", "Oliver Bearman", ("Bearman",))
    assert (settings.language, settings.days_before_fp1, settings.timezone) == (
        "ru",
        2,
        "Europe/Warsaw",
    )
    assert settings.source == REPO_CONFIG


def test_missing_default_file_means_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = config.load()
    assert settings == Config()
    assert settings.source is None


def test_missing_explicit_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no such file"):
        config.load(tmp_path / "nope.toml")


def test_default_file_is_read_from_the_working_directory(tmp_path: Path, monkeypatch) -> None:
    write(tmp_path, "[digest]\ndays_before_fp1 = 3\n")
    monkeypatch.chdir(tmp_path)
    assert config.load().days_before_fp1 == 3


def test_any_number_of_favourites(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
[[favourites.teams]]
id = "williams"
name = "Williams"

[[favourites.drivers]]
id = "albon"
name = "Alexander Albon"
aliases = ["Albon", " Alex Albon "]
track_future = true
""",
    )
    settings = config.load(path)
    assert settings.favourites.teams == (Favourite("williams", "Williams", ("Williams",)),)
    assert settings.favourites.drivers == (
        Favourite("albon", "Alexander Albon", ("Albon", "Alex Albon"), track_future=True),
    )
    # A section that is left out keeps its defaults.
    assert (settings.language, settings.days_before_fp1) == ("ru", 2)


def test_empty_lists_mean_no_favourites(tmp_path: Path) -> None:
    settings = config.load(write(tmp_path, "[favourites]\nteams = []\ndrivers = []\n"))
    assert settings.favourites.teams == ()
    assert settings.favourites.drivers == ()


def test_left_out_favourite_list_keeps_its_default(tmp_path: Path) -> None:
    settings = config.load(write(tmp_path, "[favourites]\nteams = []\n"))
    assert settings.favourites.teams == ()
    assert settings.favourites.driver_ids == ("russell", "ocon", "bearman")


def test_alias_defaults_to_the_name() -> None:
    assert Favourite("haas", "Haas").aliases == ("Haas",)


def test_only_narrows_the_favourites_to_given_ids() -> None:
    favs = config.DEFAULT_FAVOURITES

    assert favs.only() == favs
    narrowed = favs.only(["haas"], ["bearman", "lawson", "bearman"])
    # Known ids keep their config entry; an unknown one is named by its id; no duplicates.
    assert narrowed.teams == (favs.teams[1],)
    assert narrowed.drivers == (favs.drivers[2], config.Favourite("lawson", "lawson"))
    assert favs.only(driver_ids=[]).drivers == ()
    assert favs.only(driver_ids=[]).teams == favs.teams


def test_timezone_is_usable() -> None:
    assert str(Config(timezone="America/Chicago").tz) == "America/Chicago"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[digest\n", "not valid TOML"),
        ('[digest]\nlanguage = "de"\n', r"'de' is not supported \(supported: ru, en, pl\)"),
        ("[digest]\nlanguage = 1\n", "digest.language: must be a string"),
        ("[digest]\ndays_before_fp1 = -1\n", "0 or more"),
        ("[digest]\ndays_before_fp1 = 1.5\n", "must be a whole number"),
        ("[digest]\ndays_before_fp1 = true\n", "must be a whole number"),
        ('[digest]\ndays_before_fp1 = "2"\n', "must be a whole number"),
        ('[digest]\ntimezone = "Europe/Atlantis"\n', "unknown time zone"),
        ('[digest]\ntimezone = "../etc"\n', "unknown time zone"),
        ('[digest]\ntimezone = "America/Chicago"\n', "not supported yet"),
        ("[digest]\ndays_befor_fp1 = 3\n", r"unknown key\(s\) days_befor_fp1"),
        ("telegram_token = 'x'\n", r"top level: unknown key\(s\) telegram_token"),
        ('digest = "x"\n', r"\[digest\]: must be a table"),
        ('[favourites]\nteams = "mclaren"\n', "must be a list of tables"),
        ('[favourites]\nteams = ["mclaren"]\n', r"teams\[1\]: must be a table"),
        ('[[favourites.teams]]\nname = "McLaren"\n', r"teams\[1\].id: required"),
        ('[[favourites.teams]]\nid = "mclaren"\n', r"teams\[1\].name: required"),
        ('[[favourites.teams]]\nid = "mclaren"\nname = "  "\n', "name: required"),
        ('[[favourites.teams]]\nid = "McLaren"\nname = "McLaren"\n', "not a Jolpica id"),
        ('[[favourites.teams]]\nid = "red bull"\nname = "Red Bull"\n', "not a Jolpica id"),
        (
            '[[favourites.drivers]]\nid = "ocon"\nname = "Ocon"\n'
            '[[favourites.drivers]]\nid = "ocon"\nname = "E. Ocon"\n',
            r"drivers\[2\].id: 'ocon' is listed twice",
        ),
        (
            '[[favourites.drivers]]\nid = "ocon"\nname = "Ocon"\naliases = "Ocon"\n',
            "aliases: must be a list",
        ),
        (
            '[[favourites.drivers]]\nid = "ocon"\nname = "Ocon"\naliases = [""]\n',
            "aliases: must be a list of non-empty strings",
        ),
        (
            '[[favourites.drivers]]\nid = "ocon"\nname = "Ocon"\ntrack_future = "yes"\n',
            "track_future: must be true or false",
        ),
        ('[[favourites.drivers]]\nid = "ocon"\nname = "Ocon"\nteam = "haas"\n', "unknown key"),
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, text: str, message: str) -> None:
    path = write(tmp_path, text)
    with pytest.raises(ConfigError, match=message) as info:
        config.load(path)
    assert str(path) in str(info.value)  # the error says which file


def test_cli_config_command_shows_the_settings(capsys) -> None:
    assert main(["--config", str(REPO_CONFIG), "config"]) == 0
    out = capsys.readouterr().out
    assert f"settings: {REPO_CONFIG}" in out
    assert "days before FP1: 2" in out
    assert "time zone: Europe/Warsaw" in out
    assert "  haas: Haas, news words: Haas" in out
    assert "  ocon: Esteban Ocon, news words: Ocon (and their future in F1)" in out
    assert "  bearman: Oliver Bearman, news words: Bearman\n" in out


def test_cli_config_command_without_a_file(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "[favourites]\nteams = []\n")
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "favourite teams:\n  none\n" in out
    (tmp_path / "config.toml").unlink()
    assert main(["config"]) == 0
    assert "built-in defaults (no config file)" in capsys.readouterr().out


def test_cli_rejects_a_bad_config_before_doing_anything(tmp_path: Path, capsys) -> None:
    path = write(tmp_path, "[digest]\ndays_before_fp1 = -3\n")
    assert main(["--config", str(path), "due"]) == 2
    assert "error: config:" in capsys.readouterr().err


class _Due:
    """Records the lead time `due` and `run` are called with."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def due(self, days_before: int, state: str) -> int:
        self.calls.append(("due", days_before))
        return 0

    def run(self, days_before: int, *args: object) -> int:
        self.calls.append(("run", days_before))
        return 0


@pytest.fixture
def recorded(monkeypatch) -> _Due:
    recorder = _Due()
    monkeypatch.setattr(cli, "_cmd_due", recorder.due)
    monkeypatch.setattr(cli, "_cmd_run", recorder.run)
    return recorder


def test_days_before_comes_from_the_config(tmp_path: Path, recorded: _Due) -> None:
    path = write(tmp_path, "[digest]\ndays_before_fp1 = 4\n")
    assert main(["--config", str(path), "due"]) == 0
    assert main(["--config", str(path), "run", "--dry-run"]) == 0
    assert recorded.calls == [("due", 4), ("run", 4)]


def test_days_before_flag_overrides_the_config(tmp_path: Path, recorded: _Due) -> None:
    path = write(tmp_path, "[digest]\ndays_before_fp1 = 4\n")
    assert main(["--config", str(path), "due", "--days-before", "0"]) == 0
    assert main(["--config", str(path), "run", "--days-before", "1"]) == 0
    assert recorded.calls == [("due", 0), ("run", 1)]


def test_days_before_default_without_a_config(tmp_path: Path, monkeypatch, recorded) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["due"]) == 0
    assert recorded.calls == [("due", 2)]

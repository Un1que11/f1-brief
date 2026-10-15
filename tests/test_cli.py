import pytest

from f1brief import __version__
from f1brief.cli import main


def test_main_without_arguments_succeeds() -> None:
    assert main([]) == 0


def test_version_flag_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"f1brief {__version__}"


def test_unknown_argument_fails() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--no-such-flag"])

    assert exc.value.code == 2

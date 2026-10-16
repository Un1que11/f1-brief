# The digest is Russian: Cyrillic letters that look like Latin ones are intended here.
# ruff: noqa: RUF001
import io
import re
from pathlib import Path

import pytest

from f1brief import telegram_format as tf
from f1brief.cli import main

# A synthetic digest in the shape the prompt asks for: numbered headings on their own lines,
# a source in brackets after every point, a news link.
DIGEST = """\
1. Раунд 20: Гран-при США, Остин

Гонка — 25 октября, старт в 21:00 (время варшавское) [Jolpica-F1].
FP1 — 23 октября, 19:30 [Jolpica-F1].

2. Что изменилось
Норрис +12 очков, лидер (P1) [Jolpica-F1].
Штраф: 5 с за превышение скорости на пит-лейне! [BBC Sport] https://example.com/f1/a_b?x=1&y=2

7. Чего ожидать
1. Маклорен — фаворит квалификации {по форме} [Jolpica-F1].
2. Дождь вероятен ~40% в субботу [Open-Meteo].
"""

UNESCAPE = re.compile(r"\\(.)", re.DOTALL)


def plain(message: str) -> str:
    """A MarkdownV2 message back to its text: bold markers dropped, escapes undone."""
    return UNESCAPE.sub(r"\1", re.sub(r"(?<!\\)\*", "", message))


def words(text: str) -> list[str]:
    return text.split()


# --- escaping ----------------------------------------------------------------------------


def test_every_markdownv2_special_character_is_escaped() -> None:
    specials = "_*[]()~`>#+-=|{}.!\\"
    assert tf.escape(specials) == "".join(f"\\{ch}" for ch in specials)


def test_other_characters_are_left_alone() -> None:
    text = "Норрис, P1: 25 очков; «Маклорен» — 2-й? 50% & co / 21:00 🏁"
    assert tf.escape(text) == text.replace("-", "\\-")


def test_length_counts_utf16_code_units() -> None:
    assert tf.length("abc") == 3
    assert tf.length("Остин") == 5
    assert tf.length("🏁") == 2


# --- headings ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    ["1. Раунд 20: Гран-при США, Остин", "7. Чего ожидать", "12. Погода"],
)
def test_numbered_short_line_without_source_is_a_heading(line: str) -> None:
    assert tf.is_heading(line)


@pytest.mark.parametrize(
    "line",
    [
        "1. Маклорен — фаворит квалификации [Jolpica-F1].",  # a point with a source
        "2. См. https://example.com",
        "Погода",
        "1.5 секунды отставания",
        "1. " + "очень длинная строка " * 5,
    ],
)
def test_points_and_long_lines_are_not_headings(line: str) -> None:
    assert not tf.is_heading(line)


def test_headings_are_bold_and_the_rest_is_escaped() -> None:
    [message] = tf.messages(DIGEST)
    lines = message.split("\n")
    assert lines[0] == "*1\\. Раунд 20: Гран\\-при США, Остин*"
    assert "*2\\. Что изменилось*" in lines
    assert "*7\\. Чего ожидать*" in lines
    assert "1\\. Маклорен — фаворит квалификации \\{по форме\\} \\[Jolpica\\-F1\\]\\." in lines
    assert sum(line.startswith("*") for line in lines) == 3


def test_one_message_keeps_the_text_and_paragraphs() -> None:
    [message] = tf.messages(DIGEST)
    assert plain(message) == DIGEST.strip()


def test_blank_lines_and_trailing_spaces_are_normalised() -> None:
    [message] = tf.messages("\r\n\r\nА  \r\n\n\n\nБ \n")
    assert message == "А\n\nБ"


def test_empty_digest_is_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        tf.messages(" \n\n ")


def test_limit_below_one_escaped_character_is_rejected() -> None:
    with pytest.raises(ValueError, match="limit"):
        tf.messages(DIGEST, limit=1)


# --- splitting ---------------------------------------------------------------------------


def long_digest() -> str:
    """About 3 times Telegram's limit: seven sections of numbered points with sources."""
    sections = []
    for n in range(1, 8):
        points = [
            f"Пункт {n}.{i}: Норрис +{i} очков (P{i}), Расселл — P{i + 1}! [Jolpica-F1]"
            for i in range(1, 25)
        ]
        sections.append(f"{n}. Раздел номер {n}\n" + "\n".join(points))
    return "\n\n".join(sections) + "\n"


def test_long_digest_is_split_within_telegram_limit() -> None:
    text = long_digest()
    chunks = tf.messages(text)
    assert len(chunks) > 1
    assert all(tf.length(chunk) <= tf.MAX_LENGTH for chunk in chunks)
    # Nothing is lost or reordered.
    assert words(" ".join(plain(chunk) for chunk in chunks)) == words(text)


def test_split_falls_between_paragraphs_when_they_fit() -> None:
    text = long_digest()
    chunks = tf.messages(text)
    # Every section is far below the limit, so each message holds whole sections.
    for chunk in chunks:
        assert chunk.startswith("*")
        for para in chunk.split("\n\n"):
            assert re.match(r"\*\d\\\. Раздел номер \d\*\n", para)
            assert para.count("\n") == 24


def test_no_message_ends_with_a_heading() -> None:
    text = long_digest()
    for limit in (300, 1000, 2500, tf.MAX_LENGTH):
        for chunk in tf.messages(text, limit):
            assert not chunk.split("\n")[-1].startswith("*")


@pytest.mark.parametrize("limit", [2, 3, 7, 25, 80, 500])
def test_any_limit_is_respected_and_no_text_is_lost(limit: int) -> None:
    chunks = tf.messages(DIGEST, limit)
    assert all(tf.length(chunk) <= limit for chunk in chunks)
    assert "".join(words(" ".join(plain(c) for c in chunks))) == "".join(words(DIGEST))


@pytest.mark.parametrize("limit", [2, 3, 5, 9])
def test_escapes_are_never_cut_in_half(limit: int) -> None:
    for chunk in tf.messages("!!!!!.....-----" * 3, limit):
        assert re.fullmatch(r"(\\.)+", chunk), chunk


def test_long_line_is_split_between_words() -> None:
    line = " ".join(f"слово{i}" for i in range(50))
    chunks = tf.messages(line, limit=40)
    assert all(tf.length(c) <= 40 for c in chunks)
    assert all(re.fullmatch(r"слово\d+( слово\d+)*", c) for c in chunks)
    assert " ".join(chunks) == line


def test_emoji_counts_twice_towards_the_limit() -> None:
    chunks = tf.messages("🏁" * 10, limit=6)
    assert chunks == ["🏁🏁🏁"] * 3 + ["🏁"]


# --- cli ---------------------------------------------------------------------------------


def test_format_command_prints_numbered_messages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "digest.txt"
    path.write_text(long_digest(), encoding="utf-8")
    assert main(["format", "--input", str(path), "--limit", "2000"]) == 0
    out = capsys.readouterr().out
    headers = re.findall(r"^--- message (\d+)/(\d+), (\d+) chars ---$", out, re.MULTILINE)
    assert len(headers) == int(headers[0][1]) > 1
    assert [int(h[0]) for h in headers] == list(range(1, len(headers) + 1))
    assert all(int(h[2]) <= 2000 for h in headers)


def test_format_command_reads_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("7. Чего ожидать\nДождь."))
    assert main(["format"]) == 0
    assert capsys.readouterr().out == (
        "--- message 1/1, 26 chars ---\n*7\\. Чего ожидать*\nДождь\\.\n"
    )


def test_format_command_rejects_an_empty_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "digest.txt"
    path.write_text("\n", encoding="utf-8")
    assert main(["format", "--input", str(path)]) == 1
    assert capsys.readouterr().err == "error: the digest is empty\n"


def test_format_command_rejects_a_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["format", "--input", str(tmp_path / "nope.txt")]) == 1
    assert capsys.readouterr().err.startswith("error: cannot read")


def test_format_command_rejects_a_tiny_limit(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["format", "--limit", "1"]) == 2
    assert capsys.readouterr().err == "error: --limit must be at least 2\n"

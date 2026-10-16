"""The digest text as Telegram messages: MarkdownV2 with every special character escaped, split
to fit Telegram's message length limit.

The digest (F13) is plain text. Its section headings (`1. Heading` on a line of their own, with
no source in brackets) are made bold; everything else is sent as written. Splits fall between
paragraphs where possible, then between lines, then between words, and a heading stays with
the text after it.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

# Telegram allows 4096 characters per message after entity parsing. The limit is applied to
# the MarkdownV2 source, which is never shorter than the parsed text, so it is always safe.
MAX_LENGTH = 4096

# Characters that must be escaped everywhere in MarkdownV2 text, the backslash included.
# https://core.telegram.org/bots/api#markdownv2-style
SPECIAL = frozenset("_*[]()~`>#+-=|{}.!\\")

HEADING = re.compile(r"\d{1,2}\.\s+\S")
HEADING_MAX_CHARS = 80


def escape(text: str) -> str:
    return "".join(f"\\{ch}" if ch in SPECIAL else ch for ch in text)


def length(text: str) -> int:
    """Message length as Telegram counts it: in UTF-16 code units (an emoji counts as two)."""
    return len(text.encode("utf-16-le")) // 2


def is_heading(line: str) -> bool:
    """A section heading: `N. Heading`, short and without a source (`[...]`) or a link."""
    return (
        HEADING.match(line) is not None
        and len(line) <= HEADING_MAX_CHARS
        and "[" not in line
        and "://" not in line
    )


def paragraphs(text: str) -> list[list[str]]:
    """The text as paragraphs (separated by blank lines) of lines, trailing spaces removed."""
    result: list[list[str]] = [[]]
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if line:
            result[-1].append(line)
        elif result[-1]:
            result.append([])
    return [p for p in result if p]


@dataclass(frozen=True)
class _Piece:
    """A run of rendered text, the separator that goes before it when it does not start a
    message, and how to break it into smaller pieces when it does not fit in one."""

    sep: str
    text: str
    parts: Callable[[], list["_Piece"]] = field(default=lambda: [])


def _chars(word: str, limit: int) -> list[_Piece]:
    """A word too long for one message, cut between characters (never inside an escape)."""
    pieces, current = [], ""
    for ch in word:
        rendered = escape(ch)
        if current and length(current + rendered) > limit:
            pieces.append(_Piece("", current))
            current = ""
        current += rendered
    pieces.append(_Piece("", current))
    return pieces


def _line(line: str, sep: str, limit: int) -> _Piece:
    def words() -> list[_Piece]:
        return [
            _Piece(sep if i == 0 else " ", escape(w), lambda w=w: _chars(w, limit))
            for i, w in enumerate(line.split(" "))
        ]

    # A heading too long for the limit is split like any other line, without the bold.
    text = f"*{escape(line)}*" if is_heading(line) else escape(line)
    return _Piece(sep, text, words)


def _join(pieces: Sequence[_Piece]) -> str:
    return "".join((p.sep if i else "") + p.text for i, p in enumerate(pieces))


def _glue(head: _Piece, nxt: _Piece) -> _Piece:
    """`head` and `nxt` as one piece. Broken up, `head` stays glued to the first part of
    `nxt`, down to its first word."""

    def parts() -> list[_Piece]:
        rest = nxt.parts()
        if not rest:
            return [head, nxt]
        first = _Piece(nxt.sep, rest[0].text, rest[0].parts)
        return [_glue(head, first), *rest[1:]]

    return _Piece(head.sep, _join([head, nxt]), parts)


def _keep_with_next(pieces: list[_Piece], heading: list[bool]) -> list[_Piece]:
    """Glue each heading piece to the piece after it, so a message never ends with a
    heading."""
    result: list[_Piece] = []
    i = 0
    while i < len(pieces):
        if heading[i] and i + 1 < len(pieces):
            result.append(_glue(pieces[i], pieces[i + 1]))
            i += 2
        else:
            result.append(pieces[i])
            i += 1
    return result


def _paragraph(lines: list[str], sep: str, limit: int) -> _Piece:
    pieces = [_line(line, sep if j == 0 else "\n", limit) for j, line in enumerate(lines)]
    heading = [is_heading(line) for line in lines]
    return _Piece(sep, _join(pieces), lambda: _keep_with_next(pieces, heading))


class _Packer:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.messages: list[str] = []
        self.current = ""

    def add(self, piece: _Piece) -> None:
        joined = self.current + piece.sep + piece.text if self.current else piece.text
        if length(joined) <= self.limit:
            self.current = joined
        elif length(piece.text) <= self.limit:
            self.flush()
            self.current = piece.text
        else:
            parts = piece.parts()
            if not parts:
                raise ValueError(f"cannot fit {piece.text[:40]!r}... into {self.limit} chars")
            for i, part in enumerate(parts):
                self.add(_Piece(piece.sep, part.text, part.parts) if i == 0 else part)

    def flush(self) -> None:
        if self.current:
            self.messages.append(self.current)
        self.current = ""


def messages(text: str, limit: int = MAX_LENGTH) -> list[str]:
    """The digest as MarkdownV2 messages, each at most `limit` characters long."""
    if limit < 2:  # one escaped character
        raise ValueError("limit must be at least 2")
    paras = paragraphs(text)
    if not paras:
        raise ValueError("the digest is empty")
    pieces = [_paragraph(p, "\n\n", limit) for p in paras]
    heading = [len(p) == 1 and is_heading(p[0]) for p in paras]
    packer = _Packer(limit)
    for piece in _keep_with_next(pieces, heading):
        packer.add(piece)
    packer.flush()
    return packer.messages

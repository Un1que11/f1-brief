"""Telegram messages in HTML (`parse_mode=HTML`, F41), split to fit the message length limit.

Only `<`, `>` and `&` need escaping in Telegram's HTML (and `"` inside an attribute), so a
name or a headline never breaks a message. A message is built from `Block`s: an optional bold
heading and a list of `Line`s, optionally inside `<blockquote expandable>` so a long section
stays folded until it is opened. A `Line` is a list of `Span`s (plain, bold, a link or a
`<pre>` block) and always renders to balanced tags.

Splitting never cuts inside a tag or an entity: messages break between blocks, then between
lines, then between words of a line, and every piece is rendered on its own. A heading stays
with the first line after it, and a folded section that has to be split is folded again in
the next message.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

# Telegram allows 4096 characters per message after entity parsing. The limit is applied to
# the HTML source, which is never shorter than the parsed text, so it is always safe.
MAX_LENGTH = 4096
PARSE_MODE = "HTML"

BLOCK_SEP = "\n\n"
QUOTE_OPEN = "<blockquote expandable>"
QUOTE_CLOSE = "</blockquote>"
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def escape(text: str) -> str:
    """`text` safe as Telegram HTML text: only `&`, `<` and `>` are special."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(text: str) -> str:
    return escape(text).replace('"', "&quot;")


def length(text: str) -> int:
    """Message length as Telegram counts it: in UTF-16 code units (an emoji counts as two)."""
    return len(text.encode("utf-16-le")) // 2


def is_web_link(url: str) -> bool:
    return url.startswith(("https://", "http://"))


@dataclass(frozen=True)
class Span:
    """A run of text: plain, bold, a link (`href`) or a preformatted block (`pre`)."""

    text: str
    bold: bool = False
    href: str | None = None
    pre: bool = False

    def render(self) -> str:
        text = escape(self.text)
        if self.pre:
            return f"<pre>{text}</pre>"
        if self.bold:
            text = f"<b>{text}</b>"
        if self.href and is_web_link(self.href):
            text = f'<a href="{escape_attr(self.href)}">{text}</a>'
        return text


@dataclass(frozen=True)
class Line:
    spans: tuple[Span, ...]

    def render(self) -> str:
        return "".join(s.render() for s in self.spans)

    def words(self) -> list["Line"]:
        """The line cut into its words (each keeping its style), for when it does not fit in
        one message. A preformatted span is cut between its lines instead."""
        pieces: list[Line] = []
        for span in self.spans:
            sep = "\n" if span.pre else " "
            for part in span.text.split(sep):
                if part:
                    pieces.append(Line((Span(part, span.bold, span.href, span.pre),)))
        return pieces

    def chars(self, limit: int) -> list["Line"]:
        """A single word too long for a message, cut between characters."""
        pieces: list[Line] = []
        for span in self.spans:
            current = ""
            for ch in span.text:
                candidate = Span(current + ch, span.bold, span.href, span.pre)
                if current and length(candidate.render()) > limit:
                    pieces.append(Line((Span(current, span.bold, span.href, span.pre),)))
                    current = ""
                current += ch
            if current:
                pieces.append(Line((Span(current, span.bold, span.href, span.pre),)))
        return pieces


def text_line(text: str) -> Line:
    """Plain text as a line; `**…**` marks bold (an unmatched `**` stays as it is)."""
    spans: list[Span] = []
    pos = 0
    for match in _BOLD.finditer(text):
        if match.start() > pos:
            spans.append(Span(text[pos : match.start()]))
        spans.append(Span(match.group(1), bold=True))
        pos = match.end()
    if pos < len(text):
        spans.append(Span(text[pos:]))
    return Line(tuple(spans) or (Span(""),))


def plain(text: str) -> Line:
    """Text as one plain line, with no markup at all."""
    return Line((Span(text),))


@dataclass(frozen=True)
class Block:
    """A run of lines, with a heading kept with the first of them. `sep` goes between lines;
    `quote` folds the lines into an expandable blockquote."""

    lines: tuple[Line, ...]
    heading: Line | None = None
    quote: bool = False
    sep: str = "\n"


@dataclass
class _Segment:
    """A block, or the part of one, inside one message. `joins[i]` goes before `lines[i]`."""

    heading: str | None
    quote: bool
    lines: list[str] = field(default_factory=list)
    joins: list[str] = field(default_factory=list)

    def render(self) -> str:
        body = "".join(
            (j if i else "") + line
            for i, (j, line) in enumerate(zip(self.joins, self.lines, strict=True))
        )
        if self.quote and self.lines:
            body = f"{QUOTE_OPEN}{body}{QUOTE_CLOSE}"
        if self.heading is None:
            return body
        return f"{self.heading}\n{body}" if body else self.heading


@dataclass
class _Packer:
    limit: int
    messages: list[str] = field(default_factory=list)
    segments: list[_Segment] = field(default_factory=list)

    def _fits(self, segments: Sequence[_Segment]) -> bool:
        return length(BLOCK_SEP.join(s.render() for s in segments)) <= self.limit

    def flush(self) -> None:
        if self.segments:
            self.messages.append(BLOCK_SEP.join(s.render() for s in self.segments))
        self.segments = []

    def add_block(self, block: Block) -> None:
        heading = block.heading.render() if block.heading else None
        lines = [line.render() for line in block.lines]
        whole = _Segment(heading, block.quote, lines, [block.sep] * len(lines))
        if self._fits([*self.segments, whole]):
            self.segments.append(whole)
            return
        if self.segments and self._fits([whole]):
            self.flush()
            self.segments.append(whole)
            return
        if not block.lines:
            raise ValueError(f"cannot fit a heading into {self.limit} characters")
        segment: _Segment | None = None
        for i, line in enumerate(block.lines):
            segment = self._add(segment, heading if i == 0 else None, line, block, block.sep)

    def _add(
        self, segment: _Segment | None, heading: str | None, line: Line, block: Block, join: str
    ) -> _Segment:
        """Adds `line` after `segment` (joined by `join`) while that segment is still in the
        message being filled; otherwise starts a new segment, with `heading`, in this message
        or the next. Returns the segment the next line goes in."""
        rendered = line.render()
        current = self.segments[-1] if self.segments else None
        if segment is not None and segment is current and heading is None:
            segment.lines.append(rendered)
            segment.joins.append(join)
            if self._fits(self.segments):
                return segment
            segment.lines.pop()
            segment.joins.pop()
        fresh = _Segment(heading, block.quote, [rendered], [join])
        if self._fits([*self.segments, fresh]):
            self.segments.append(fresh)
            return fresh
        if self._fits([fresh]):
            self.flush()
            self.segments.append(fresh)
            return fresh
        # Too long for any message: cut it, filling the message being built first.
        pieces, gap = line.words(), "\n" if line.spans[0].pre else " "
        if len(pieces) <= 1:
            pieces, gap = line.chars(self._room(heading, block)), ""
            if len(pieces) <= 1:
                raise ValueError(f"cannot fit {rendered[:40]!r}... into {self.limit} chars")
        result: _Segment | None = None
        for j, piece in enumerate(pieces):
            # The first piece keeps the heading; the rest follow word by word.
            result = self._add(
                result, heading if j == 0 else None, piece, block, join if j == 0 else gap
            )
        assert result is not None
        return result

    def _room(self, heading: str | None, block: Block) -> int:
        """How many characters a line gets in an otherwise empty message."""
        empty = _Segment(heading, block.quote, [""], [""]).render()
        return max(1, self.limit - length(empty))


def pack(blocks: Iterable[Block], limit: int = MAX_LENGTH) -> list[str]:
    """The blocks as HTML messages, each at most `limit` characters long."""
    if limit < 16:
        raise ValueError("limit must be at least 16")
    packer = _Packer(limit)
    for block in blocks:
        if block.lines or block.heading is not None:
            packer.add_block(block)
    packer.flush()
    if not packer.messages:
        raise ValueError("nothing to send")
    return packer.messages


def paragraphs(text: str) -> list[Block]:
    """Plain text (no markup) as blocks: one per paragraph, one line per line."""
    blocks: list[Block] = []
    current: list[Line] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if line:
            current.append(plain(line))
        elif current:
            blocks.append(Block(tuple(current)))
            current = []
    if current:
        blocks.append(Block(tuple(current)))
    return blocks


def from_text(text: str, limit: int = MAX_LENGTH) -> list[str]:
    """Plain text, escaped, as HTML messages."""
    return pack(paragraphs(text), limit)

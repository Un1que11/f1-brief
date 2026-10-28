"""Telegram HTML (F41): escaping, inline markup and splits that never cut a tag or an entity."""

# Russian sample texts: Cyrillic letters that look like Latin ones are intended here.
# ruff: noqa: RUF001
from html.parser import HTMLParser

import pytest

from f1brief import telegram_html as th
from f1brief.telegram_html import Block, Line, Span

TAGS = {"b", "a", "pre", "blockquote"}


class _Balance(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in TAGS:
            self.problems.append(f"unknown tag {tag}")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            self.problems.append(f"unbalanced </{tag}>")

    def handle_entityref(self, name: str) -> None:
        if name not in ("amp", "lt", "gt", "quot"):
            self.problems.append(f"entity &{name};")


def assert_valid(message: str) -> None:
    """Balanced known tags, only Telegram's entities and no raw `<`/`&` in the text."""
    parser = _Balance()
    parser.feed(message)
    parser.close()
    assert parser.problems == [] and parser.stack == [], message
    # Nothing left of a cut entity ("&am") or tag ("<b") at either end.
    assert not message.rstrip().endswith(("&", "&a", "&am", "&amp", "<", "</")), message


def bold(text: str) -> Line:
    return Line((Span(text, bold=True),))


# --- escaping and inline markup -------------------------------------------------------------


def test_only_the_three_special_characters_are_escaped() -> None:
    assert th.escape('Haas & <Ocon> "P4" _*[]') == 'Haas &amp; &lt;Ocon&gt; "P4" _*[]'
    assert th.escape_attr('a"b&c') == "a&quot;b&amp;c"


def test_double_asterisks_mark_bold_and_the_text_inside_is_escaped() -> None:
    line = th.text_line("Ocon **6–3** Bearman, **<P4>** & **unclosed")
    assert line.render() == ("Ocon <b>6–3</b> Bearman, <b>&lt;P4&gt;</b> &amp; **unclosed")


def test_links_are_escaped_and_only_web_links_are_linked() -> None:
    url = 'https://example.com/a?x=1&y="2"'
    assert Span("F1 & co", href=url).render() == (
        '<a href="https://example.com/a?x=1&amp;y=&quot;2&quot;">F1 &amp; co</a>'
    )
    assert Span("label", href="javascript:alert(1)").render() == "label"


def test_length_counts_utf16_units() -> None:
    assert th.length("🏁 a") == 4


# --- packing --------------------------------------------------------------------------------


def test_blocks_go_in_one_message_when_they_fit() -> None:
    blocks = [
        Block((th.text_line("**P4**"),), heading=bold("📊 Зачёт")),
        Block((th.plain("a"), th.plain("b")), heading=bold("Новости"), quote=True),
    ]
    assert th.pack(blocks) == [
        "<b>📊 Зачёт</b>\n<b>P4</b>\n\n<b>Новости</b>\n<blockquote expandable>a\nb</blockquote>"
    ]


def test_a_block_that_fits_alone_starts_a_new_message() -> None:
    first = Block((th.plain("x" * 50),))
    second = Block((th.plain("y" * 40),), heading=bold("H"))
    assert th.pack([first, second], limit=80) == ["x" * 50, "<b>H</b>\n" + "y" * 40]


def test_a_heading_stays_with_its_first_line() -> None:
    blocks = [
        Block((th.plain("x" * 60),)),
        Block(tuple(th.plain(f"line {n} " + "z" * 20) for n in range(6)), heading=bold("Head")),
    ]
    messages = th.pack(blocks, limit=100)
    for message in messages:
        assert not message.endswith("<b>Head</b>")
    head = next(m for m in messages if "<b>Head</b>" in m)
    assert "<b>Head</b>\nline 0 " in head
    assert all(th.length(m) <= 100 for m in messages)


def test_a_folded_section_is_folded_again_in_the_next_message() -> None:
    lines = tuple(th.plain(f"item {n} " + "w " * 15) for n in range(8))
    messages = th.pack([Block(lines, heading=bold("Новости"), quote=True)], limit=150)
    assert len(messages) > 2
    assert messages[0].startswith("<b>Новости</b>\n<blockquote expandable>item 0")
    for message in messages:
        assert_valid(message)
        assert th.length(message) <= 150
        assert message.count("<blockquote expandable>") == 1
        assert message.endswith("</blockquote>")
    # Every item is there once, in order.
    joined = "".join(messages)
    assert [joined.index(f"item {n} ") for n in range(8)] == sorted(
        joined.index(f"item {n} ") for n in range(8)
    )


def test_a_line_longer_than_a_message_is_cut_between_words_keeping_its_tags() -> None:
    line = Line(
        (
            Span("• " + "слово & " * 40),
            Span("Отличный & жирный " * 10, bold=True),
            Span(" · "),
            Span("F1.com", href="https://example.com/a&b"),
        )
    )
    messages = th.pack([Block((line,), heading=bold("Head"))], limit=120)
    assert len(messages) > 3
    assert messages[0].startswith("<b>Head</b>\n• слово")
    for message in messages:
        assert_valid(message)
        assert th.length(message) <= 120
    assert messages[-1].endswith('<a href="https://example.com/a&amp;b">F1.com</a>')


def test_a_word_longer_than_a_message_is_cut_between_characters() -> None:
    word = "&<" * 40
    messages = th.pack([Block((th.plain(word),))], limit=30)
    for message in messages:
        assert_valid(message)
        assert th.length(message) <= 30
    assert "".join(messages) == th.escape(word)


def test_a_long_pre_block_is_cut_between_its_lines() -> None:
    table = "\n".join(f"Пт 23.10 19:{n:02d}  Практика" for n in range(20))
    messages = th.pack([Block((Line((Span(table, pre=True),)),))], limit=200)
    assert len(messages) > 1
    for message in messages:
        assert_valid(message)
        assert message.startswith("<pre>") and message.endswith("</pre>")


def test_emoji_count_double_against_the_limit() -> None:
    messages = th.pack([Block(tuple(th.plain("🏁" * 10) for _ in range(3)))], limit=45)
    assert all(th.length(m) <= 45 for m in messages)
    assert len(messages) == 2


def test_plain_text_becomes_escaped_paragraphs() -> None:
    assert th.from_text("a < b\n\n\nc & d\ne") == ["a &lt; b\n\nc &amp; d\ne"]


def test_nothing_to_send_and_a_tiny_limit_are_errors() -> None:
    with pytest.raises(ValueError, match="nothing to send"):
        th.pack([])
    with pytest.raises(ValueError, match="at least 16"):
        th.pack([Block((th.plain("a"),))], limit=8)

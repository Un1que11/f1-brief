from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from f1brief.config import DEFAULT_FAVOURITES, Favourite, Favourites
from f1brief.news import (
    Feed,
    NewsError,
    NewsItem,
    NewsReport,
    classify,
    deduplicate,
    default_since,
    fetch_news,
    format_report,
    parse_feed,
    select,
)

RSS = Path(__file__).parent / "fixtures" / "rss"
SINCE = datetime(2026, 10, 8, tzinfo=UTC)
# The official-style fixture uses an en dash (U+2013) in this headline.
OCON_TITLE = "Haas confirm Ocon contract talks for 2027 \u2013 what's next?"
FAVS = DEFAULT_FAVOURITES


def feed_bytes(name: str) -> bytes:
    return (RSS / name).read_bytes()


def all_items() -> list[NewsItem]:
    return (
        parse_feed(feed_bytes("official_like.xml"), "Formula 1", FAVS)
        + parse_feed(feed_bytes("motorsport_like.xml"), "Motorsport.com", FAVS)
        + parse_feed(feed_bytes("bbc_like.xml"), "BBC Sport", FAVS)
    )


def test_official_feed_without_dates_and_wrong_encoding_declaration() -> None:
    items = parse_feed(feed_bytes("official_like.xml"), "Formula 1", FAVS)

    assert len(items) == 3
    assert all(i.published is None for i in items)
    # Declared us-ascii but contains UTF-8 en dashes: decoded correctly.
    assert items[0].title == OCON_TITLE
    assert items[0].sources == ("Formula 1",)


def test_html_descriptions_are_cleaned_and_dates_parsed() -> None:
    russell = parse_feed(feed_bytes("motorsport_like.xml"), "Motorsport.com", FAVS)[0]

    assert russell.summary == "The stewards ruled after the session."
    assert russell.published == datetime(2026, 10, 14, 18, 30, tzinfo=UTC)
    bbc = parse_feed(feed_bytes("bbc_like.xml"), "BBC Sport", FAVS)[0]
    assert bbc.published == datetime(2026, 10, 15, 7, 15, tzinfo=UTC)


RUSSELL, OCON, BEARMAN = "George Russell", "Esteban Ocon", "Oliver Bearman"


@pytest.mark.parametrize(
    ("title", "summary", "favourites", "topics", "future"),
    [
        ("Russell handed five-place grid penalty", "", {RUSSELL}, {"penalties"}, False),
        ("McLaren bring new floor", "", {"McLaren"}, {"upgrades"}, False),
        ("FIA publishes technical directive", "", set(), {"regulations"}, False),
        ("Ocon signs for Alpine", "", {OCON}, {"contracts"}, True),
        ("Ocon on his future in F1", "", {OCON}, set(), True),
        ("Ocon fastest in practice", "", {OCON}, set(), False),
        # Only favourites with track_future in the config have their future followed.
        ("Russell signs for Ferrari", "", {RUSSELL}, {"contracts"}, False),
        ("Bearman and Haas agree new deal", "", {BEARMAN, "Haas"}, {"contracts"}, False),
        # Off-season (F18): tests and launches, but not every "test".
        ("McLaren complete Barcelona shakedown", "", {"McLaren"}, {"testing"}, False),
        ("Pre-season testing: day one", "", set(), {"testing"}, False),
        ("Russell put to the test by tyre wear", "", {RUSSELL}, set(), False),
        # Race-report wording that must not count as contract news.
        (
            "Verstappen shows signs of pace",
            "deal with it; corner exit; seat position",
            set(),
            set(),
            False,
        ),
        # Whole words only.
        ("Russellville and McLarenesque", "", set(), set(), False),
        ("mclaren and RUSSELL", "", {"McLaren", RUSSELL}, set(), False),
    ],
)
def test_classify(title: str, summary: str, favourites: set, topics: set, future: bool) -> None:
    assert classify(title, summary, FAVS, 2026) == (
        frozenset(favourites),
        frozenset(topics),
        future,
    )


def test_a_year_is_future_wording_only_when_after_the_items_own_year() -> None:
    items = {
        i.link.rsplit("/", 1)[1]: i for i in parse_feed(feed_bytes("years_like.xml"), "Y", FAVS)
    }

    # Published in 2027: "the 2027 Haas" is this season's car, not Ocon's future.
    assert not items["ocon-2027-car"].future
    # Published in 2027 and naming 2028: what comes next for him.
    assert items["ocon-2028"].future
    # Published in December 2026: 2027 is the next season.
    assert items["ocon-ahead"].future


def test_an_undated_item_takes_its_year_from_now() -> None:
    feed = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        b"<item><title>Ocon looks ahead to 2027</title><link>https://example.test/u</link>"
        b"</item></channel></rss>"
    )

    assert parse_feed(feed, "U", FAVS, now=datetime(2026, 10, 17, tzinfo=UTC))[0].future
    assert not parse_feed(feed, "U", FAVS, now=datetime(2027, 3, 1, tzinfo=UTC))[0].future


def test_classify_uses_the_favourites_it_is_given() -> None:
    mine = Favourites(
        teams=(Favourite("williams", "Williams", ("Williams", "Grove")),),
        drivers=(Favourite("max_verstappen", "Max Verstappen", ("Verstappen",), True),),
    )

    assert classify("Verstappen to leave Red Bull?", "", mine) == (
        frozenset({"Max Verstappen"}),
        frozenset({"contracts"}),
        True,
    )
    assert classify("Grove gets a new wind tunnel", "", mine)[0] == {"Williams"}
    # The default favourites mean nothing to it.
    assert classify("Ocon signs for Alpine", "", mine) == (frozenset(), {"contracts"}, False)
    assert classify("Ocon signs for Alpine", "", Favourites((), ())) == (
        frozenset(),
        {"contracts"},
        False,
    )


def test_same_story_from_two_feeds_is_merged_with_known_date() -> None:
    merged = deduplicate(all_items())
    ocon = next(i for i in merged if "Ocon" in i.title)
    floor = next(i for i in merged if "new floor" in i.title)

    # Official (undated, en dash) + BBC (dated, hyphen): one story, BBC's date.
    assert ocon.sources == ("Formula 1", "BBC Sport")
    assert ocon.published == datetime(2026, 10, 15, 7, 15, tzinfo=UTC)
    assert ocon.future
    assert floor.sources == ("Formula 1", "Motorsport.com")
    assert floor.published == datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
    assert floor.topics == {"upgrades"}


def test_select_keeps_relevant_recent_items_newest_first() -> None:
    chosen = select(all_items(), SINCE)

    assert [i.title for i in chosen] == [
        OCON_TITLE,
        "Russell handed five-place grid penalty",
        "McLaren bring new floor to Austin",
    ]
    # Not relevant (Verstappen, Hamilton, "five things") or too old (FIA, 5 Oct) are dropped.


def test_undated_relevant_item_without_dated_copy_is_kept_last() -> None:
    undated = NewsItem(
        "Ocon on his future",
        "https://x/1",
        None,
        "",
        ("Formula 1",),
        frozenset({"Ocon"}),
        frozenset(),
        True,
    )
    dated = NewsItem(
        "Russell penalty",
        "https://x/2",
        datetime(2026, 10, 9, tzinfo=UTC),
        "",
        ("BBC Sport",),
        frozenset({"Russell"}),
        frozenset({"penalties"}),
    )

    assert [i.link for i in select([undated, dated], SINCE)] == ["https://x/2", "https://x/1"]


def test_unreadable_feed_raises() -> None:
    with pytest.raises(NewsError):
        parse_feed(b"<html><body>Service unavailable</body>", "Broken", FAVS)


def _transport(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"].startswith("f1brief")
        return routes.get(str(request.url), httpx.Response(404))

    return httpx.MockTransport(handler)


FEEDS = (
    Feed("Formula 1", "https://feeds.test/official"),
    Feed("Motorsport.com", "https://feeds.test/ms"),
    Feed("BBC Sport", "https://feeds.test/bbc"),
)


def test_fetch_news_combines_feeds() -> None:
    routes = {
        "https://feeds.test/official": httpx.Response(200, content=feed_bytes("official_like.xml")),
        "https://feeds.test/ms": httpx.Response(200, content=feed_bytes("motorsport_like.xml")),
        "https://feeds.test/bbc": httpx.Response(200, content=feed_bytes("bbc_like.xml")),
    }

    with httpx.Client(transport=_transport(routes)) as client:
        report = fetch_news(SINCE, FAVS, FEEDS, client)

    assert len(report.items) == 3
    assert report.failed_feeds == ()
    assert [i.title for i in report.future] == [OCON_TITLE]


def test_failing_feed_is_reported_not_fatal() -> None:
    routes = {
        "https://feeds.test/official": httpx.Response(200, content=feed_bytes("official_like.xml")),
        "https://feeds.test/ms": httpx.Response(503),
        "https://feeds.test/bbc": httpx.Response(200, content=b"<html>maintenance</html>"),
    }

    with httpx.Client(transport=_transport(routes)) as client:
        report = fetch_news(SINCE, FAVS, FEEDS, client)

    assert report.failed_feeds == ("Motorsport.com", "BBC Sport")
    # Official items are undated, so the relevant ones are kept.
    assert {i.title for i in report.items} == {
        OCON_TITLE,
        "McLaren bring new floor to Austin",
    }


def test_all_feeds_failing_raises() -> None:
    with (
        httpx.Client(transport=_transport({})) as client,
        pytest.raises(NewsError, match="no feed"),
    ):
        fetch_news(SINCE, FAVS, FEEDS, client)


def test_since_must_be_aware() -> None:
    with pytest.raises(ValueError):
        fetch_news(datetime(2026, 10, 8), FAVS, FEEDS)


def test_default_since_is_a_week_back() -> None:
    assert default_since(datetime(2026, 10, 15, 12, tzinfo=UTC)) == datetime(
        2026, 10, 8, 12, tzinfo=UTC
    )


def test_format_report() -> None:
    report = NewsReport(
        since=SINCE, items=tuple(select(all_items(), SINCE)), failed_feeds=("RaceFans",)
    )

    text = format_report(report)

    assert text.splitlines()[0] == "F1 news since 08 Oct 00:00 UTC: 3 relevant items"
    assert "Could not read: RaceFans" in text
    ocon_section = text.split("Favourites' future")[1].split("Favourites and key topics")[0]
    assert "[15 Oct 07:15 UTC] Haas confirm Ocon contract talks" in ocon_section
    assert "(Formula 1, BBC Sport; Esteban Ocon, Haas, contracts)" in ocon_section
    assert (
        "[14 Oct 18:30 UTC] Russell handed five-place grid penalty"
        " (Motorsport.com; George Russell, penalties)" in text
    )


def test_format_empty_report() -> None:
    text = format_report(NewsReport(since=SINCE, items=(), failed_feeds=()))

    assert text.count("nothing new") == 2


def test_html_page_instead_of_feed_is_unreadable_but_empty_feed_is_fine() -> None:
    with pytest.raises(NewsError, match="not an RSS or Atom"):
        parse_feed(b"<html>maintenance</html>", "BBC Sport", FAVS)

    empty = b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
    assert parse_feed(empty, "Quiet feed", FAVS) == []

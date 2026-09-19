"""RSS adapter: real feeds, malformed feeds, and non-feeds."""

from __future__ import annotations

import pytest

from telescope.adapters.rss import FeedParseError, parse_feed


@pytest.mark.parametrize(
    "name",
    ["gamesindustry.xml", "eurogamer.xml", "gematsu.xml"],
)
def test_real_captured_feeds_parse(name, load_fixture):
    feed = parse_feed(load_fixture(name))
    assert len(feed.entries) > 0
    assert feed.title
    assert feed.version


def test_malformed_feed_raises(load_fixture):
    with pytest.raises(FeedParseError):
        parse_feed(load_fixture("malformed.xml"))


def test_truncated_xml_phantom_entry_is_rejected():
    # Regression: feedparser emits an entry object with title=None and id=None
    # for XML truncated mid-document. Treating that as a successful parse would
    # let a broken feed report itself as healthy, with zero items.
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>Broken</title>'
        b"<item><title>Cut off"
    )
    with pytest.raises(FeedParseError):
        parse_feed(body)


def test_html_page_is_not_a_feed(load_fixture):
    # A Cloudflare interstitial returns HTTP 200; it must not be mistaken for a
    # feed with zero items.
    with pytest.raises(FeedParseError):
        parse_feed(load_fixture("not_a_feed.html"))


def test_empty_body_raises():
    with pytest.raises(FeedParseError):
        parse_feed(b"")


def test_valid_but_empty_feed_raises():
    body = b'<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>'
    with pytest.raises(FeedParseError):
        parse_feed(body)


def test_bozo_but_usable_feed_still_returns_entries():
    # Trailing junk after a complete feed: feedparser flags bozo yet yields the
    # entry. Discarding it would lose a real story.
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>Ok</title>'
        b"<item><title>Story</title><guid>1</guid></item>"
        b"</channel></rss>garbage-after-root"
    )
    feed = parse_feed(body)
    assert len(feed.entries) == 1
    assert feed.bozo is True
    assert feed.warning is not None

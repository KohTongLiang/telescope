"""Normalization: URL hygiene, dates, identity, HTML, merge behaviour."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from telescope.adapters.rss import parse_feed
from telescope.config import Source
from telescope.normalize import (
    MAX_SUMMARY_CHARS,
    canonicalize_url,
    entry_guid,
    make_id,
    normalize_entries,
    normalize_entry,
    parse_entry_date,
    strip_html,
)

UTC = timezone.utc

SRC = Source(
    id="example",
    name="Example",
    url="https://example.com/feed",
    tier="enthusiast",
    axis="general",
)


# -- URL canonicalization ----------------------------------------------------


def test_canonicalize_strips_tracking_and_fragment():
    got = canonicalize_url(
        "https://Example.com/News/Story/?utm_source=rss&utm_medium=feed&id=42#top"
    )
    assert got == "https://example.com/News/Story?id=42"


def test_canonicalize_keeps_meaningful_params_sorted():
    assert canonicalize_url("http://x.test/a?b=2&a=1") == "http://x.test/a?a=1&b=2"


def test_canonicalize_drops_default_ports():
    assert canonicalize_url("https://x.test:443/a") == "https://x.test/a"
    assert canonicalize_url("http://x.test:80/a") == "http://x.test/a"


def test_canonicalize_leaves_odd_input_alone():
    assert canonicalize_url("") == ""
    assert canonicalize_url("/relative/path") == "/relative/path"


# -- HTML stripping ----------------------------------------------------------


def test_strip_html():
    assert strip_html("<p>Hello <strong>world</strong></p>") == "Hello world"
    assert strip_html("A &amp; B") == "A & B"
    assert strip_html("   spaced    out  ") == "spaced out"
    assert strip_html(None) is None
    assert strip_html("") is None


# -- Dates -------------------------------------------------------------------


def test_parse_entry_date_from_struct_time():
    entry = {"published_parsed": time.struct_time((2026, 9, 14, 9, 30, 0, 0, 0, 0))}
    assert parse_entry_date(entry) == datetime(2026, 9, 14, 9, 30, tzinfo=UTC)


def test_parse_entry_date_converts_offset_to_utc():
    entry = {"published": "Sat, 12 Sep 2026 23:59:59 +0900"}
    assert parse_entry_date(entry) == datetime(2026, 9, 12, 14, 59, 59, tzinfo=UTC)


def test_parse_entry_date_garbage_is_none():
    assert parse_entry_date({"published": "not a real date"}) is None
    assert parse_entry_date({}) is None


# -- Identity ----------------------------------------------------------------


def test_entry_guid_prefers_id_then_link_then_title():
    assert entry_guid({"id": "abc", "link": "https://x.test/1"}) == "abc"
    assert entry_guid({"link": "https://x.test/1"}) == "https://x.test/1"
    fallback = entry_guid({"title": "Hello", "published": "Mon, 14 Sep 2026 09:30:00 +0000"})
    assert fallback == make_id("fallback", "Hello|Mon, 14 Sep 2026 09:30:00 +0000")
    assert entry_guid({}) is None


def test_make_id_is_source_scoped_and_stable():
    assert make_id("a", "g") != make_id("b", "g")
    assert make_id("a", "g") == make_id("a", "g")


# -- Summary handling --------------------------------------------------------


def test_summary_is_truncated():
    entry = {"id": "long", "title": "T", "summary": "x" * (MAX_SUMMARY_CHARS + 500)}
    item = normalize_entry(SRC, entry, fetched_at=datetime(2026, 9, 15, tzinfo=UTC))
    assert item is not None
    assert item.summary is not None
    assert item.summary.endswith("…")
    assert len(item.summary) == MAX_SUMMARY_CHARS + 1


# -- Handcrafted edge-case fixture -------------------------------------------

ENTRY_COUNT = 7


@pytest.fixture
def quirky(load_fixture):
    return parse_feed(load_fixture("quirky.xml"))


@pytest.fixture
def quirky_items(quirky, now):
    items, stats = normalize_entries(
        SRC, quirky.entries, fetched_at=now, feed_title=quirky.title
    )
    return items, stats


def test_quirky_fixture_has_expected_entries(quirky):
    assert len(quirky.entries) == ENTRY_COUNT


def test_quirky_feed_title_read(quirky):
    assert quirky.title == "Quirky Feed"


def test_quirky_all_entries_normalize(quirky_items):
    items, stats = quirky_items
    assert len(items) == ENTRY_COUNT
    assert stats.skipped == 0


def test_quirky_missing_dates_counted(quirky_items):
    _, stats = quirky_items
    # entry 2 has no date; entry 7 has an unparseable one
    assert stats.missing_dates == 2


def test_quirky_html_and_tracking_stripped(quirky_items):
    items, _ = quirky_items
    first = items[0]
    assert first.title == "Studio announces Layoffs & restructuring"
    assert first.canonical_url == "https://example.com/news/layoffs?id=42"
    assert first.url.endswith("#top")  # original link preserved
    assert first.categories == ["Business"]
    assert first.image_url == "https://example.com/img/thumb.jpg"
    assert first.published_at == datetime(2026, 9, 14, 9, 30, tzinfo=UTC)


def test_quirky_duplicate_guid_collapses(quirky_items):
    items, _ = quirky_items
    assert items[0].guid == items[2].guid
    assert items[0].id == items[2].id


def test_quirky_missing_date_is_none(quirky_items):
    items, _ = quirky_items
    assert items[1].published_at is None


def test_quirky_relative_link_and_dc_date(quirky_items):
    items, _ = quirky_items
    relative = items[3]
    assert relative.url == "/news/relative"
    assert relative.published_at is not None
    assert relative.published_at.astimezone(UTC) == datetime(2026, 9, 13, 18, 0, tzinfo=UTC)


def test_quirky_title_only_entry_gets_fallback_identity(quirky_items):
    items, _ = quirky_items
    only_title = items[4]
    assert only_title.title == "Only a title here"
    assert only_title.url == ""
    assert only_title.guid == make_id("fallback", "Only a title here|Sun, 13 Sep 2026 07:00:00 GMT")


def test_quirky_unicode_preserved(quirky_items):
    items, _ = quirky_items
    unicode_item = items[5]
    assert "日本語のニュース" in unicode_item.title
    assert "café" in unicode_item.title
    assert unicode_item.published_at == datetime(2026, 9, 12, 14, 59, 59, tzinfo=UTC)


def test_quirky_whitespace_collapsed(quirky_items):
    items, _ = quirky_items
    assert items[6].title == "Whitespace everywhere"
    assert items[6].published_at is None


# -- Real captured feeds -----------------------------------------------------


@pytest.mark.parametrize("name", ["gamesindustry.xml", "eurogamer.xml", "gematsu.xml"])
def test_real_feeds_normalize_cleanly(name, load_fixture, now):
    feed = parse_feed(load_fixture(name))
    items, stats = normalize_entries(
        SRC, feed.entries, fetched_at=now, feed_title=feed.title
    )
    assert len(items) == len(feed.entries)
    assert stats.skipped == 0

    dated = [i for i in items if i.published_at is not None]
    assert len(dated) / len(items) > 0.9, f"{name}: too many missing dates"

    for item in items:
        assert item.id == make_id(SRC.id, item.guid)
        assert item.title
        if item.url:
            assert item.canonical_url.startswith("http")
        assert "utm_" not in item.canonical_url

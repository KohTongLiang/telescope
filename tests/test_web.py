"""Tests for the read-only web viewer.

The viewer is a thin layer over the same queries the CLI uses, so these tests
concentrate on the parts that are new and easy to get wrong: that filtering and
faceted counts agree with each other, that a hostile query or headline cannot
escape the page, and that the app never writes to the database.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from telescope.models import NormalizedItem
from telescope.normalize import make_id
from telescope.store import Store, utcnow
from telescope.web import markdown
from telescope.web.app import create_app, highlight, relative_age
from telescope.web.query import ItemFilter, WebQueries

# One digest-shaped document, exercising every construct the renderer must know:
# headings, a hard line break, a bold link, a blockquote, bullets, a rule.
DIGEST_MARKDOWN = """# Games industry digest — Tuesday 15 September 2026

**Window** 14 Sep 21:59 → 15 Sep 21:59 Asia/Singapore (24h)  
**Volume** 385 items → 291 stories  

## Top stories

**1. [Xbox layoffs hit multiple studios](https://example.com/xbox)**  
GamesIndustry.biz +7 others · 7.7h ago
> Sources say the cuts affect several teams.

## Layoffs & closures

- [More cuts reported](https://example.com/more) — Eurogamer · 2.1h ago

---

Generated 2026-09-15 21:59 SGT · 30 sources configured
"""


def make_item(
    title: str,
    *,
    source_id: str = "eurogamer",
    summary: str = "Something happened.",
    categories: tuple[str, ...] = (),
    topics: tuple[str, ...] = (),
    entities: tuple[str, ...] = (),
    hours_ago: float = 2,
) -> NormalizedItem:
    identifier = f"{source_id}:{title}"
    slug = title.lower().replace(" ", "-")[:60]
    url = f"https://example.com/{source_id}/{slug}"
    return NormalizedItem(
        id=make_id(source_id, identifier),
        source_id=source_id,
        guid=identifier,
        title=title,
        summary=summary,
        url=url,
        canonical_url=url,
        published_at=utcnow() - timedelta(hours=hours_ago),
        fetched_at=utcnow(),
        categories=list(categories),
        topics=list(topics),
        entities=list(entities),
    )


@pytest.fixture
def seeded(loaded_store):
    """A handful of items that differ on every filterable dimension."""
    loaded_store.save_items(
        [
            make_item(
                "Xbox layoffs hit multiple studios",
                source_id="gamesindustry",
                categories=("Xbox Series X/S", "Business"),
                topics=("Layoffs & closures",),
                entities=("Xbox Game Studios",),
            ),
            make_item(
                "Switch 2 price increase confirmed",
                source_id="eurogamer",
                categories=("Nintendo Switch 2",),
                topics=("Platform & store", "Hardware"),
            ),
            make_item(
                "Sega acquires a small studio",
                source_id="gamesindustry",
                categories=("Business",),
                topics=("M&A & funding",),
                summary="Terms were not disclosed.",
            ),
            make_item(
                "Old news from last month",
                source_id="eurogamer",
                categories=("PC",),
                topics=("Tools & engines",),
                hours_ago=24 * 40,
            ),
        ]
    )
    return loaded_store


@pytest.fixture
def client(settings, seeded):
    digest_dir = settings.digests_dir
    assert digest_dir is not None
    digest_dir.mkdir(parents=True, exist_ok=True)
    path = digest_dir / "2026-09-15.md"
    path.write_text(DIGEST_MARKDOWN, encoding="utf-8")

    now = utcnow()
    seeded.record_digest(
        generated_at=now,
        window_start=now - timedelta(hours=24),
        window_end=now,
        path=str(path),
        items_considered=385,
        stories=291,
        sources_ok=30,
        sources_total=30,
        stale_sources=0,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# ------------------------------------------------------------------- pages


def test_dashboard_reports_the_pipeline(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Overview" in response.text
    assert "291" in response.text  # stories in the latest digest
    assert "Xbox layoffs hit multiple studios" not in response.text


def test_items_page_lists_stored_items(client):
    response = client.get("/items")
    assert response.status_code == 200
    assert "Xbox layoffs hit multiple studios" in response.text
    assert "Old news from last month" in response.text


def test_topic_filter_narrows_the_list(client):
    response = client.get("/items", params={"topic": "Layoffs & closures"})
    assert "Xbox layoffs hit multiple studios" in response.text
    assert "Switch 2 price increase confirmed" not in response.text


def test_tag_filter_narrows_the_list(client):
    response = client.get("/items", params={"tag": "Nintendo Switch 2"})
    assert "Switch 2 price increase confirmed" in response.text
    assert "Xbox layoffs hit multiple studios" not in response.text


def test_multiple_topics_are_an_or_filter(client):
    response = client.get(
        "/items", params=[("topic", "Layoffs & closures"), ("topic", "M&A & funding")]
    )
    assert "Xbox layoffs hit multiple studios" in response.text
    assert "Sega acquires a small studio" in response.text
    assert "Switch 2 price increase confirmed" not in response.text


def test_source_filter_narrows_the_list(client):
    response = client.get("/items", params={"source": "gamesindustry"})
    assert "Sega acquires a small studio" in response.text
    assert "Switch 2 price increase confirmed" not in response.text


def test_window_filter_excludes_old_items(client):
    response = client.get("/items", params={"days": 7})
    assert "Xbox layoffs hit multiple studios" in response.text
    assert "Old news from last month" not in response.text


def test_search_matches_and_marks_the_hit(client):
    response = client.get("/items", params={"q": "layoffs"})
    assert response.status_code == 200
    # The hit is wrapped in <mark>, so assert on the marked-up title.
    assert "Xbox <mark>layoffs</mark> hit multiple studios" in response.text
    assert "Switch 2 price increase confirmed" not in response.text


def test_search_can_be_combined_with_a_facet(client):
    response = client.get("/items", params={"q": "studio", "topic": "M&A & funding"})
    assert response.status_code == 200
    assert "Sega acquires a small <mark>studio</mark>" in response.text
    assert "Switch 2" not in response.text


def test_malformed_query_degrades_to_like_instead_of_failing(client):
    response = client.get("/items", params={"q": '"unbalanced'})
    assert response.status_code == 200
    assert "Nothing matches" in response.text


def test_hostile_query_cannot_inject_markup(client):
    response = client.get("/items", params={"q": "<script>alert(1)</script>"})
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text


def test_facet_counts_do_not_collapse_to_the_selected_value(client):
    """Selecting one topic must still offer the others."""
    response = client.get("/items", params={"topic": "Layoffs & closures"})
    assert "M&amp;A &amp; funding" in response.text


def test_htmx_request_is_a_full_page_containing_the_swap_target(client):
    """htmx uses hx-select on this fragment, so the response must contain it."""
    response = client.get("/items", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert 'id="items-main"' in response.text
    assert "<!doctype html>" in response.text.lower()


def test_item_detail_page(client, seeded):
    item = seeded.recent_items(limit=1)[0]
    response = client.get(f"/items/{item['id']}")
    assert response.status_code == 200
    assert item["title"] in response.text


def test_unknown_item_renders_a_404_page(client):
    response = client.get("/items/does-not-exist")
    assert response.status_code == 404
    assert "no such item" in response.text


def test_stories_view_clusters_without_writing(client):
    response = client.get("/stories", params={"hours": 48})
    assert response.status_code == 200
    assert "Xbox layoffs hit multiple studios" in response.text


def test_sources_view_lists_the_registry(client):
    response = client.get("/sources")
    assert response.status_code == 200
    assert "GamesIndustry.biz" in response.text


def test_source_detail_shows_fetch_history(client):
    response = client.get("/sources/gamesindustry")
    assert response.status_code == 200
    assert "Stored items" in response.text


# ----------------------------------------------------------------- digests


def test_digest_list_links_to_the_digest(client):
    response = client.get("/digests")
    assert response.status_code == 200
    assert "2026-09-15" in response.text


def test_digest_detail_renders_the_stored_markdown(client):
    response = client.get("/digests/1")
    assert response.status_code == 200
    assert "<h2" in response.text
    assert 'href="https://example.com/xbox"' in response.text
    assert "<blockquote>" in response.text
    assert "<hr>" in response.text
    assert "GamesIndustry.biz +7 others" in response.text


def test_digest_raw_returns_markdown(client):
    response = client.get("/digests/1/raw")
    assert response.status_code == 200
    assert response.text.startswith("# Games industry digest")
    assert response.headers["content-type"].startswith("text/plain")


def test_digest_without_a_file_says_so(settings, seeded):
    digest_id = seeded.record_digest(
        generated_at=utcnow(),
        window_start=utcnow(),
        window_end=utcnow(),
        path=None,
        items_considered=0,
        stories=0,
        sources_ok=0,
        sources_total=0,
        stale_sources=0,
    )
    with TestClient(create_app(settings)) as test_client:
        response = test_client.get(f"/digests/{digest_id}")
    assert response.status_code == 200
    assert "no file path" in response.text


def test_missing_digest_is_a_404(client):
    response = client.get("/digests/999")
    assert response.status_code == 404


def test_missing_database_reports_503(settings):
    """A fresh checkout with no database must explain itself, not stack-trace."""
    with TestClient(create_app(settings)) as test_client:
        response = test_client.get("/items")
    assert response.status_code == 503
    assert "telescope sync" in response.text


# ------------------------------------------------------------------ safety


def test_read_only_store_refuses_writes(settings, store):
    store.set_state("probe", "value")
    store.close()

    readonly = Store(settings.db_path, readonly=True)
    try:
        assert readonly.readonly is True
        assert readonly.get_state("probe") == "value"
        with pytest.raises(sqlite3.OperationalError):
            readonly.conn.execute(
                "INSERT INTO run_state (key, value, updated_at) VALUES ('x', 'y', 'z')"
            )
    finally:
        readonly.close()


def test_highlight_only_wraps_plain_words():
    assert "<mark>layoffs</mark>" in highlight("Xbox layoffs hit", "layoffs")
    # Regex metacharacters in a query are matched literally, never executed.
    assert "<mark>" not in highlight("abc", "a.*")


def test_relative_age_accepts_strings_and_datetimes():
    assert relative_age(None) == "never"
    assert relative_age("not-a-date") == "never"
    assert relative_age(utcnow()).endswith("s ago")


# ----------------------------------------------------------------- markdown


def test_markdown_escapes_raw_html():
    rendered = markdown.render("## Hi\n\n<script>alert(1)</script>")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_markdown_refuses_javascript_links():
    rendered = markdown.render("[click me](javascript:alert(1))")
    assert "href" not in rendered
    assert "click me" in rendered


def test_markdown_links_with_brackets_in_the_label():
    """The digest escapes inner brackets, e.g. "[[Industry news] Headline](url)"."""
    rendered = markdown.render(r"- [\[Industry news\] Curve signs a deal](https://m.test/a)")
    assert '<a href="https://m.test/a"' in rendered
    assert "[Industry news] Curve signs a deal" in rendered
    assert "](https" not in rendered


def test_markdown_keeps_backslash_escapes_literal():
    rendered = markdown.render(r"\[Industry news\] and \*not emphasis\*")
    assert "[Industry news] and *not emphasis*" in rendered
    assert "<em>" not in rendered


def test_markdown_renders_the_shapes_the_digest_uses():
    rendered = markdown.render(DIGEST_MARKDOWN)
    assert "<h1>" in rendered
    assert 'id="top-stories"' in rendered
    assert "<blockquote>" in rendered
    assert "<ul>" in rendered
    assert "<li>" in rendered
    assert "<hr>" in rendered
    assert "<br>" in rendered  # the hard break in the header block


def test_markdown_sections_are_unique_and_linkable():
    found = markdown.sections("# Title\n\n## Same\n\n## Same\n")
    assert [title for title, _ in found] == ["Same", "Same"]
    assert markdown.slugify("Same", 1) == "same"
    assert markdown.slugify("Same", 2) == "same-2"
    assert markdown.slugify("Layoffs & closures") == "layoffs-closures"


# ------------------------------------------------------------------ filter


def test_filter_query_string_round_trips():
    filters = ItemFilter(
        q="layoffs", topics=("M&A & funding",), tags=("PS5",), sources=("eurogamer",), days=7
    )
    query = filters.query_string(offset=40)
    assert "q=layoffs" in query
    assert "topic=M%26A+%26+funding" in query
    assert "tag=PS5" in query
    assert "source=eurogamer" in query
    assert "days=7" in query
    assert "offset=40" in query


def test_filter_toggle_adds_then_removes():
    filters = ItemFilter()
    once = filters.toggled("topics", "Hardware")
    assert once.topics == ("Hardware",)
    twice = once.toggled("topics", "Hardware")
    assert twice.topics == ()


def test_facets_count_every_item_without_filters(seeded):
    facets = WebQueries(seeded).facets(ItemFilter())
    topics = {facet["name"]: facet["count"] for facet in facets["topics"]}
    assert topics["Layoffs & closures"] == 1
    assert topics["Platform & store"] == 1
    sources = {facet["id"]: facet["count"] for facet in facets["sources"]}
    assert sources["eurogamer"] == 2

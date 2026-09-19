"""Digest rendering: sections, health footer, and escaping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from telescope.cluster import Story
from telescope.config import DigestConfig, Watchlist
from telescope.health import build_health
from telescope.render.markdown import render_digest

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def render(stories, rows, *, config=None, watchlist=None, items=10):
    health = build_health(
        rows, items_considered=items, stories=len(stories), last_sync_at="2026-09-15T11:00:00+00:00"
    )
    return render_digest(
        stories=stories,
        health=health,
        window_start=NOW - timedelta(hours=24),
        window_end=NOW,
        generated_at=NOW,
        config=config or DigestConfig(),
        watchlist=watchlist,
    )


def story(article_factory, title, *, score=5.0, **kw) -> Story:
    built = Story(articles=[article_factory(title, **kw)])
    built.score = score
    return built


# -- structure ---------------------------------------------------------------


def test_digest_has_header_and_core_sections(article_factory, source_row_factory):
    stories = [story(article_factory, "Sony acquires Housemarque", tier="trade")]
    markdown = render(stories, [source_row_factory()])

    assert "# Games industry digest —" in markdown
    assert "**Window**" in markdown
    assert "**Volume**" in markdown
    assert "**Sources** 1/1 sources OK" in markdown
    # A story matching no topic still has to appear somewhere.
    assert "## Everything else" in markdown
    assert "## Source health" in markdown


def test_stories_are_rendered_as_headline_links(article_factory, source_row_factory):
    stories = [
        story(article_factory, "Sony acquires Housemarque", url="https://x.test/one"),
        story(article_factory, "Studio confirms layoffs", url="https://x.test/two"),
    ]
    markdown = render(stories, [source_row_factory()])

    assert "**[Sony acquires Housemarque](https://x.test/one)**" in markdown
    assert "**[Studio confirms layoffs](https://x.test/two)**" in markdown


def test_coverage_shows_breadth(article_factory, source_row_factory):
    wide = Story(
        articles=[
            article_factory("Major publisher acquires a studio", minutes_ago=90),
            article_factory(
                "Major publisher acquires a studio",
                source_id="ign",
                source_name="IGN",
                minutes_ago=60,
            ),
            article_factory(
                "Major publisher acquires a studio",
                source_id="gamespot",
                source_name="GameSpot",
                minutes_ago=30,
            ),
        ]
    )
    wide.score = 9.0
    markdown = render([wide], [source_row_factory()])
    assert "+2 others" in markdown


def test_single_source_story_has_no_others_suffix(article_factory, source_row_factory):
    markdown = render(
        [story(article_factory, "A solo scoop entirely")], [source_row_factory()]
    )
    assert "Eurogamer" in markdown
    assert "others" not in markdown


def test_summary_is_included_as_a_quote(article_factory, source_row_factory):
    stories = [
        story(article_factory, "A thing happened", summary="The detailed explanation.")
    ]
    markdown = render(stories, [source_row_factory()])
    assert "> The detailed explanation." in markdown


def test_story_without_summary_still_renders(article_factory, source_row_factory):
    stories = [story(article_factory, "A terse item", summary=None)]
    markdown = render(stories, [source_row_factory()])
    assert "A terse item" in markdown
    assert "> " not in markdown


# -- sections ----------------------------------------------------------------


def test_other_reports_are_listed_not_just_counted(article_factory, source_row_factory):
    wide = Story(
        articles=[
            article_factory("Major publisher acquires a studio", minutes_ago=90),
            article_factory(
                "Major publisher acquires a studio, confirmed",
                source_id="ign",
                source_name="IGN",
                url="https://ign.test/story",
                minutes_ago=60,
            ),
        ]
    )
    wide.score = 9.0
    markdown = render([wide], [source_row_factory()])

    # The count is there...
    assert "*1 other report*" in markdown
    # ...and so is the report itself, with its outlet and age.
    assert (
        "[Major publisher acquires a studio, confirmed](https://ign.test/story) — IGN"
        in markdown
    )


def test_topic_sections_follow_configured_order(article_factory, source_row_factory):
    stories = [
        story(article_factory, "Top one", score=9.0),
        story(article_factory, "Top two", score=8.0),
        story(article_factory, "Top three", score=7.0),
        story(
            article_factory,
            "Studio confirms layoffs",
            score=1.0,
            topics=["Layoffs & closures"],
        ),
    ]
    markdown = render(
        stories,
        [source_row_factory()],
        config=DigestConfig(max_top_stories=3),
        watchlist=Watchlist(topics=["Layoffs & closures", "M&A & funding"]),
    )
    assert "## Layoffs & closures" in markdown
    # A topic with no stories must not produce an empty heading.
    assert "## M&A & funding" not in markdown


def test_topic_sections_are_capped_and_overflow_reaches_everything_else(
    article_factory, source_row_factory
):
    stories = [
        story(
            article_factory,
            f"Layoff story number {index}",
            score=10.0 - index,
            topics=["Layoffs & closures"],
        )
        for index in range(12)
    ]
    markdown = render(
        stories,
        [source_row_factory()],
        config=DigestConfig(topic_limit=3, long_tail_limit=2),
        watchlist=Watchlist(topics=["Layoffs & closures"]),
    )

    section = markdown.split("## Layoffs & closures", 1)[1].split("## Everything else", 1)[0]
    assert section.count("**[Layoff story number") == 3

    # Nothing is lost: the overflow is picked up by "Everything else".
    tail = markdown.split("## Everything else", 1)[1]
    assert tail.count("**[Layoff story number") == 2


def test_everything_else_holds_the_long_tail(article_factory, source_row_factory):
    stories = [story(article_factory, f"Story number {index}") for index in range(12)]
    markdown = render(stories, [source_row_factory()], config=DigestConfig(long_tail_limit=40))
    assert "## Everything else" in markdown


def test_long_tail_respects_the_limit(article_factory, source_row_factory):
    stories = [story(article_factory, f"Story number {index}") for index in range(12)]
    markdown = render(
        stories,
        [source_row_factory()],
        config=DigestConfig(long_tail_limit=3),
    )
    tail = markdown.split("## Everything else", 1)[1].split("## Source health", 1)[0]
    assert tail.count("**[Story number") == 3


# -- quiet days and failures -------------------------------------------------


def test_quiet_day_is_called_out(article_factory, source_row_factory):
    stories = [story(article_factory, "The only story today")]
    markdown = render(stories, [source_row_factory()], config=DigestConfig(quiet_day_threshold=3))
    assert "Quiet window" in markdown


def test_nothing_new_section(article_factory, source_row_factory):
    markdown = render([], [source_row_factory()], items=0)
    assert "## Nothing new" in markdown
    assert "## Top stories" not in markdown


def test_everything_failed_is_loud(source_row_factory, article_factory):
    rows = [
        source_row_factory(consecutive_failures=2, last_error="HTTP 503"),
        source_row_factory(
            source_id="ign", name="IGN", consecutive_failures=1, last_error="HTTP 403"
        ),
    ]
    markdown = render([], rows, items=0)
    assert "**Every source failed.**" in markdown
    assert "not a quiet news day" in markdown


def test_stale_source_is_reported(article_factory, source_row_factory):
    rows = [
        source_row_factory(item_age_hours=1.0),
        source_row_factory(
            source_id="vg247", name="VG247", item_age_hours=2524.0, max_age_hours=48
        ),
    ]
    markdown = render([], rows, items=0)
    assert "⚠ **VG247**" in markdown
    assert "105.2d" in markdown
    assert "1/2 sources OK" in markdown


def test_failing_source_error_is_reported(article_factory, source_row_factory):
    rows = [source_row_factory(consecutive_failures=3, last_error="HTTP 503")]
    markdown = render([], rows, items=0)
    assert "✖ **Eurogamer**" in markdown
    assert "HTTP 503" in markdown


def test_disabled_sources_do_not_appear_as_failures(article_factory, source_row_factory):
    rows = [
        source_row_factory(item_age_hours=1.0),
        source_row_factory(source_id="rps", name="RPS", enabled=False, item_age_hours=None),
    ]
    markdown = render([], rows, items=0)
    assert "✖ **RPS**" not in markdown
    assert "⚠ **RPS**" not in markdown


# -- escaping ----------------------------------------------------------------


def test_markdown_special_characters_are_escaped(article_factory, source_row_factory):
    stories = [story(article_factory, "Valve's [big] *news* _drop_")]
    markdown = render(stories, [source_row_factory()])
    assert r"\[big\]" in markdown
    assert r"\*news\*" in markdown
    assert r"\_drop\_" in markdown


def test_pipes_and_angle_brackets_are_escaped(article_factory, source_row_factory):
    stories = [story(article_factory, "A | B <script> alert")]
    markdown = render(stories, [source_row_factory()])
    assert r"\|" in markdown
    assert r"\<script\>" in markdown

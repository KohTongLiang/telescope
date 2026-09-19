"""Deterministic ranking: every term, and the ordering as a whole."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from telescope.cluster import Story
from telescope.config import DigestConfig, Watchlist
from telescope.rank import KEYWORD_CAP, rank_stories, score_story

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def one(article_factory, title="Something happens", **kw) -> Story:
    return Story(articles=[article_factory(title, **kw)])


def score(story, *, config=None, watchlist=None):
    return score_story(
        story, config=config or DigestConfig(), watchlist=watchlist, now=NOW
    )


# -- individual terms --------------------------------------------------------


def test_breadth_is_logarithmic_and_raises_the_score(article_factory):
    single = one(article_factory, "Sony acquires Housemarque")
    pair = Story(
        articles=[
            article_factory("Sony acquires Housemarque", source_id="eurogamer"),
            article_factory(
                "Sony acquires Housemarque", source_id="ign", source_name="IGN"
            ),
        ]
    )
    config = DigestConfig()
    single_score = score(single, config=config)
    pair_score = score(pair, config=config)

    assert pair_score.breadth > single_score.breadth
    assert pair_score.breadth == pytest.approx(math.log2(3))
    assert pair_score.total > single_score.total


def test_tier_affects_the_score(article_factory):
    config = DigestConfig()
    trade = score(
        one(article_factory, "Studio announces a thing", tier="trade"), config=config
    )
    community = score(
        one(
            article_factory,
            "Studio announces a thing",
            source_id="reddit_games",
            source_name="Reddit r/Games",
            tier="community",
        ),
        config=config,
    )
    assert trade.tier > community.tier


def test_recency_decays_with_age(article_factory):
    config = DigestConfig()
    fresh = score(one(article_factory, "A thing happened", minutes_ago=30), config=config)
    old = score(
        one(article_factory, "A thing happened", minutes_ago=48 * 60), config=config
    )
    assert fresh.recency > old.recency
    assert old.recency > 0


def test_keywords_raise_the_score(article_factory):
    config = DigestConfig()
    plain = score(one(article_factory, "Studio announces a project"), config=config)
    charged = score(one(article_factory, "Studio announces layoffs"), config=config)
    assert charged.keywords > 0
    assert "layoffs" in charged.matched_keywords
    assert charged.total > plain.total


def test_demote_keywords_lower_the_score(article_factory):
    config = DigestConfig()
    plain = score(one(article_factory, "Studio announces a project"), config=config)
    rumoured = score(
        one(article_factory, "Rumor: Studio announces a project"), config=config
    )
    assert rumoured.demote > 0
    assert "rumor" in rumoured.matched_demotions
    assert rumoured.total < plain.total


def test_keyword_terms_respect_word_boundaries(article_factory):
    result = score(one(article_factory, "Wholesale pricing and quarterly guidance"))
    assert "sale" not in result.matched_demotions
    assert "guide" not in result.matched_demotions


def test_keyword_contribution_is_capped(article_factory):
    stuffed = (
        "Layoffs and redundancies and a shutdown and a closure and an acquisition "
        "and a merger and a lawsuit"
    )
    result = score(one(article_factory, stuffed))
    assert result.keywords <= DigestConfig().weights.keywords * KEYWORD_CAP + 1e-9


def test_watchlist_boost(article_factory):
    config = DigestConfig()
    watchlist = Watchlist(entities=["Larian Studios"])
    hit = score(
        one(article_factory, "Larian Studios teases a new RPG"),
        config=config,
        watchlist=watchlist,
    )
    miss = score(
        one(article_factory, "Another studio teases a new RPG"),
        config=config,
        watchlist=watchlist,
    )
    assert hit.watchlist > 0
    assert miss.watchlist == 0


def test_watchlist_also_matches_summary_text(article_factory):
    story = one(
        article_factory,
        "A publisher signs a new studio",
        summary="The deal covers Headphone Turtle and two others.",
    )
    result = score(
        story,
        config=DigestConfig(),
        watchlist=Watchlist(entities=["Headphone Turtle"]),
    )
    assert result.watchlist > 0


def test_first_party_bonus(article_factory):
    config = DigestConfig()
    first_party = score(
        one(
            article_factory,
            "An event is announced",
            source_id="psblog",
            source_name="PlayStation Blog",
            tier="first_party",
        ),
        config=config,
    )
    other = score(one(article_factory, "An event is announced"), config=config)
    assert first_party.first_party > 0
    assert other.first_party == 0


# -- explainability and ordering ---------------------------------------------


def test_components_explain_the_score(article_factory):
    story = one(article_factory, "Studio announces layoffs")
    breakdown = score(story)
    assert set(breakdown.as_dict()) == {
        "breadth",
        "tier",
        "recency",
        "keywords",
        "watchlist",
        "first_party",
        "demote",
        "total",
    }
    assert story.score == pytest.approx(breakdown.total)
    assert story.components["total"] == pytest.approx(round(breakdown.total, 3), abs=1e-3)


def test_ranking_is_deterministic(article_factory):
    stories = [
        one(article_factory, f"Story number {index}") for index in range(6)
    ]
    config = DigestConfig()
    first = [story.title for story in rank_stories(list(stories), config=config, now=NOW)]
    second = [story.title for story in rank_stories(list(stories), config=config, now=NOW)]
    assert first == second


def test_significant_news_outranks_a_review(article_factory):
    layoffs = one(article_factory, "Studio confirms layoffs")
    review = one(article_factory, "Review: a perfectly fine game")
    ranked = rank_stories([review, layoffs], config=DigestConfig(), now=NOW)
    assert ranked[0].title == "Studio confirms layoffs"


def test_widespread_story_outranks_a_single_source_scoop(article_factory):
    scoop = one(article_factory, "Obscure studio teases a project", minutes_ago=5)
    widespread = Story(
        articles=[
            article_factory(
                "Major publisher acquires a studio",
                source_id=source_id,
                source_name=source_name,
                minutes_ago=90,
            )
            for source_id, source_name in (
                ("eurogamer", "Eurogamer"),
                ("ign", "IGN"),
                ("gamespot", "GameSpot"),
                ("pcgamer", "PC Gamer"),
            )
        ]
    )
    ranked = rank_stories([scoop, widespread], config=DigestConfig(), now=NOW)
    assert ranked[0].title == "Major publisher acquires a studio"

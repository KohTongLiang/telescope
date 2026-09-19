"""Deterministic ranking.

No model, no API, no cost, and every score can be explained term by term — which
matters, because when the digest surfaces something silly the only way to fix it
is to see which rule did it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache

from .cluster import TIER_ORDER, Story
from .config import DigestConfig, Watchlist

TIER_WEIGHTS: dict[str, float] = {
    "first_party": 1.0,
    "trade": 0.9,
    "enthusiast": 0.7,
    "aggregator": 0.5,
    "community": 0.4,
}

# Keeps one keyword-stuffed headline from running away with the digest.
KEYWORD_CAP = 2.5


@dataclass
class ScoreBreakdown:
    """Every term that produced the score."""

    breadth: float = 0.0
    tier: float = 0.0
    recency: float = 0.0
    keywords: float = 0.0
    watchlist: float = 0.0
    first_party: float = 0.0
    demote: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)
    matched_demotions: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return (
            self.breadth
            + self.tier
            + self.recency
            + self.keywords
            + self.watchlist
            + self.first_party
            - self.demote
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "breadth": round(self.breadth, 3),
            "tier": round(self.tier, 3),
            "recency": round(self.recency, 3),
            "keywords": round(self.keywords, 3),
            "watchlist": round(self.watchlist, 3),
            "first_party": round(self.first_party, 3),
            "demote": round(self.demote, 3),
            "total": round(self.total, 3),
        }


@lru_cache(maxsize=512)
def _phrase_regex(phrase: str) -> re.Pattern:
    # Word-boundary anchored, so "sale" does not fire on "wholesale" and
    # "guide" does not fire on "guidance".
    return re.compile(rf"(?<![\w]){re.escape(phrase)}(?![\w])", re.IGNORECASE)


def _phrase_hits(text: str, weights: dict[str, float]) -> list[str]:
    return [phrase for phrase in weights if _phrase_regex(phrase).search(text)]


def _best_tier(story: Story) -> str:
    best_index = len(TIER_ORDER)
    for article in story.articles:
        if article.tier in TIER_ORDER:
            best_index = min(best_index, TIER_ORDER.index(article.tier))
    return TIER_ORDER[best_index] if best_index < len(TIER_ORDER) else "enthusiast"


def score_story(
    story: Story,
    *,
    config: DigestConfig,
    watchlist: Watchlist | None = None,
    now: datetime | None = None,
) -> ScoreBreakdown:
    """Score one story and write the result back onto it."""
    now = now or datetime.now(timezone.utc)
    weights = config.weights
    breakdown = ScoreBreakdown()

    # Breadth: logarithmic, so ten outlets is a stronger story than one but not
    # ten times stronger.
    breakdown.breadth = weights.breadth * math.log2(1 + story.breadth)

    breakdown.tier = weights.tier * TIER_WEIGHTS.get(_best_tier(story), 0.5)

    age_hours = max(0.0, (now - story.published_at).total_seconds() / 3600.0)
    half_life = max(1.0, config.recency_half_life_hours)
    breakdown.recency = weights.recency * math.exp(-age_hours / half_life)

    # Keywords and demotions read the primary headline only: a passing mention
    # in a long summary should not outweigh what the story is actually about.
    headline = story.primary.title
    hits = _phrase_hits(headline, config.keyword_weights)
    breakdown.matched_keywords = hits
    breakdown.keywords = weights.keywords * min(
        KEYWORD_CAP, sum(config.keyword_weights[phrase] for phrase in hits)
    )

    if watchlist and watchlist.entities:
        haystack = story.searchable_text()
        if any(_phrase_regex(name).search(haystack) for name in watchlist.entities):
            breakdown.watchlist = weights.watchlist

    if story.has_tier("first_party"):
        breakdown.first_party = weights.first_party

    demotions = _phrase_hits(headline, config.demote_keywords)
    breakdown.matched_demotions = demotions
    breakdown.demote = min(
        KEYWORD_CAP, sum(config.demote_keywords[phrase] for phrase in demotions)
    )

    story.score = breakdown.total
    story.components = breakdown.as_dict()
    return breakdown


def rank_stories(
    stories: list[Story],
    *,
    config: DigestConfig,
    watchlist: Watchlist | None = None,
    now: datetime | None = None,
) -> list[Story]:
    """Score every story and return them best-first.

    Ties break on recency then title, so the ordering is stable between runs.
    """
    now = now or datetime.now(timezone.utc)
    for story in stories:
        score_story(story, config=config, watchlist=watchlist, now=now)

    return sorted(
        stories,
        key=lambda story: (-story.score, -story.published_at.timestamp(), story.title),
    )

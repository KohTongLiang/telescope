"""Deterministic entity and topic extraction."""

from __future__ import annotations

from telescope.taxonomy import (
    DEFAULT_TOPIC_ORDER,
    classify_topics,
    entity_names,
    extract_entities,
)


def names(text: str) -> list[str]:
    return entity_names(extract_entities(text))


# -- platforms ---------------------------------------------------------------


def test_platform_aliases_are_canonicalised():
    assert "PlayStation 5" in names("PS5 version delayed")
    assert "PlayStation 5" in names("PlayStation 5 patch")
    assert "Xbox Series X|S" in names("Xbox Series X gets an update")
    assert "Nintendo Switch 2" in names("Switch 2 launch details")


def test_platform_matching_is_case_insensitive_for_unambiguous_names():
    assert "PlayStation 5" in names("the ps5 patch")


def test_ambiguous_platform_words_need_capitalisation():
    # "switch" is an ordinary verb; it must not become a platform.
    assert "Nintendo Switch" not in names("Studios switch engines mid-project")
    assert "Nintendo Switch" in names("Switch version dated")


# -- orgs --------------------------------------------------------------------


def test_orgs_are_detected():
    assert "Ubisoft" in names("Ubisoft restructures its editorial team")
    assert "FromSoftware" in names("FromSoftware teases a new project")


def test_ambiguous_words_are_not_treated_as_orgs():
    # These are everyday gaming words as well as (former) company names.
    assert not [n for n in names("Rare drop rates explained") if n == "Rare"]
    assert not [n for n in names("King of the hill mode returns") if n == "King"]
    assert not [n for n in names("How the meta build shifted") if n == "Meta"]


def test_ordering_is_platforms_then_orgs_then_games():
    found = extract_entities('Sony confirms "Elden Ring" for PlayStation 5')
    kinds = [entity.type for entity in found]
    assert kinds.index("platform") < kinds.index("game")
    assert "org" in kinds
    assert "game" in kinds


# -- games -------------------------------------------------------------------


def test_quoted_titles_become_game_entities():
    assert "Elden Ring" in names('"Elden Ring" gets a patch')
    assert "Hollow Knight: Silksong" in names("'Hollow Knight: Silksong' dated")


def test_unquoted_titles_are_not_guessed():
    # Under-detecting is the deliberate failure mode: a false entity would
    # corrupt cluster boundaries.
    assert names("Elden Ring gets a patch") == []


# -- topics ------------------------------------------------------------------


def test_topic_rules():
    assert "Layoffs & closures" in classify_topics("Studio announces layoffs")
    assert "Layoffs & closures" in classify_topics("Developer shut down after a decade")
    assert "M&A & funding" in classify_topics("Sony acquires Housemarque")
    assert "Releases & delays" in classify_topics("Game delayed to March")
    assert "Platform & store" in classify_topics("New refund policy on the storefront")
    assert "Tools & engines" in classify_topics("Unity ships a new SDK")
    assert "Legal & labour" in classify_topics("Workers file a lawsuit over crunch")
    assert "Hardware" in classify_topics("Console component supply improves")
    assert "Business & earnings" in classify_topics("Quarterly revenue beats forecast")


def test_investigation_requires_regulatory_context():
    # Bare "investigation" used to match game descriptions such as
    # "a bio-investigation romance", filing them under Legal & labour.
    assert "Legal & labour" not in classify_topics(
        "Creara Select is a Japanese bio-investigation romance"
    )
    assert "Legal & labour" in classify_topics(
        "The regulator opened an antitrust investigation into the merger"
    )


def test_unrelated_text_has_no_topics():
    assert classify_topics("A pleasant walk in the park") == []
    assert classify_topics("") == []
    assert classify_topics(None) == []


def test_topics_are_ordered_canonically():
    topics = classify_topics("Studio layoffs after an acquisition delayed the game")
    assert topics[0] == "Layoffs & closures"
    assert "M&A & funding" in topics
    assert "Releases & delays" in topics


def test_broad_release_topic_sorts_after_every_specific_topic():
    # "Releases & delays" matches almost every product story, so it must sort
    # last or it becomes a dumping ground that hides the specific topics.
    order = list(DEFAULT_TOPIC_ORDER)
    release_rank = order.index("Releases & delays")
    for specific in (
        "Layoffs & closures",
        "M&A & funding",
        "Legal & labour",
        "Platform & store",
        "Tools & engines",
        "Hardware",
        "Business & earnings",
    ):
        assert release_rank > order.index(specific), specific


def test_topic_count_is_capped():
    text = (
        "Layoffs follow the acquisition, delaying the game, as the storefront "
        "changes policy, the engine ships an SDK, a lawsuit is filed, console "
        "chip supply improves, and quarterly revenue beats forecast"
    )
    assert len(classify_topics(text)) == 3

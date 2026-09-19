"""Story clustering: merges that should happen, and merges that must not."""

from __future__ import annotations

from telescope.cluster import (
    Story,
    cluster_articles,
    containment,
    content_key,
    title_tokens,
)


# -- exact stage -------------------------------------------------------------


def test_same_canonical_url_merges(article_factory):
    first = article_factory(
        "Steam Deck 2 plans revealed", url="https://x.test/a", source_id="eurogamer"
    )
    second = article_factory(
        "Completely different headline",
        url="https://x.test/a",
        source_id="ign",
        source_name="IGN",
    )
    stories = cluster_articles([first, second])
    assert len(stories) == 1
    assert stories[0].breadth == 2


def test_syndicated_identical_title_merges_despite_different_urls(article_factory):
    first = article_factory("Sony acquires Housemarque", source_id="eurogamer")
    second = article_factory(
        "Sony acquires Housemarque",
        source_id="ign",
        source_name="IGN",
        url="https://ign.com/other-path",
    )
    assert len(cluster_articles([first, second])) == 1


def test_content_key_is_order_independent(article_factory):
    a = article_factory("Sony acquires Housemarque")
    b = article_factory("Housemarque acquires Sony")
    assert content_key(a) == content_key(b)


# -- near-duplicate stage ----------------------------------------------------


def test_reworded_headline_merges_across_sources(article_factory):
    first = article_factory(
        "Ubisoft delays Rayman Legends remake Retold a couple of months",
        source_id="eurogamer",
    )
    second = article_factory(
        "Rayman Legends remake Retold delayed to November",
        source_id="ign",
        source_name="IGN",
    )
    stories = cluster_articles([first, second])
    assert len(stories) == 1
    assert stories[0].breadth == 2


def test_unrelated_stories_sharing_a_platform_do_not_merge(article_factory):
    # Both mention "Switch 2" and nothing else in common. Without the shared
    # rare-token guard these would collapse into one story.
    first = article_factory(
        "Nintendo adds VRR support to docked Switch 2", source_id="eurogamer"
    )
    second = article_factory(
        "Kirby and the World Beyond coming to Switch 2 in Spring 2027",
        source_id="ign",
        source_name="IGN",
    )
    assert len(cluster_articles([first, second])) == 2


def test_same_source_different_subjects_still_stay_apart(article_factory):
    # Same outlet, genuinely different stories: the only shared tokens are the
    # game's name, so containment stays well under the same-source gate.
    first = article_factory("Nintendo Switch 2 update adds VRR", source_id="eurogamer")
    second = article_factory(
        "Nintendo Switch 2 sales pass 20 million", source_id="eurogamer"
    )
    assert len(cluster_articles([first, second])) == 2


def test_same_source_update_merges_with_its_own_original(article_factory):
    # An outlet restating its own story — an "[update: ...]" follow-up, a
    # re-written headline — is the most certain duplicate signal available.
    # This is why the same-source gate is 0.6 and not 0.85.
    first = article_factory(
        "Aliens: Fireteam Elite 2 gets a Switch 2 release date", source_id="nintendolife"
    )
    second = article_factory(
        "Aliens: Fireteam Elite 2's Switch 2 version delayed", source_id="nintendolife"
    )
    assert len(cluster_articles([first, second])) == 1


# -- tokenisation and the near-identical escape hatch ------------------------


def test_hyphen_and_space_forms_of_one_headline_merge(article_factory):
    # "Round-Up" and "Round Up" have to tokenise alike, or the same story
    # written both ways never merges.
    first = article_factory(
        "Fire Emblem: Fortune's Weave Review Round-Up", source_id="thegamer"
    )
    second = article_factory(
        "Round Up: The Reviews For Fire Emblem: Fortune's Weave",
        source_id="nintendolife",
    )
    assert len(cluster_articles([first, second])) == 1


def test_function_words_are_not_content_tokens():
    # These are rare in a feed, so they used to pass the "distinctive token"
    # test and bridge stories that had nothing to do with each other.
    assert "where" not in title_tokens("Where to find giants' meat in a game")
    assert "read" not in title_tokens("Where To Read The Prologue Manga")


def test_identical_headlines_merge_when_the_subject_is_too_common(article_factory):
    # Once enough outlets cover one game, its name exceeds the distinctive
    # document-frequency cutoff and the old rule refused to merge even
    # word-for-word identical headlines. Coverage must not defeat identity.
    crowd = [
        article_factory(
            f"Fire Emblem Fortune's Weave guide number {n}",
            source_id=f"src{n}",
            source_name=f"Source {n}",
        )
        for n in range(40)
    ]
    first = article_factory(
        "Fire Emblem: Fortune's Weave Review", source_id="ign", source_name="IGN"
    )
    second = article_factory(
        "Fire Emblem Fortune's Weave review", source_id="vgc", source_name="VGC"
    )
    stories = cluster_articles(crowd + [first, second])

    holding = [s for s in stories if any(a.id == first.id for a in s.articles)]
    assert len(holding) == 1
    assert any(a.id == second.id for a in holding[0].articles)


def test_time_window_blocks_near_duplicate_merge(article_factory):
    first = article_factory(
        "Ubisoft delays Rayman Legends remake Retold a couple of months",
        source_id="eurogamer",
        minutes_ago=10,
    )
    second = article_factory(
        "Rayman Legends remake Retold delayed to November",
        source_id="ign",
        source_name="IGN",
        minutes_ago=10 + 72 * 60,
    )
    assert len(cluster_articles([first, second])) == 2


def test_containment_ignores_length_padding():
    # Jaccard punishes the length difference between a terse headline and a
    # padded one, which is why containment is the decision metric.
    short = title_tokens("Rayman Legends Retold delayed to December 3")
    padded = title_tokens(
        "Ubisoft delays Rayman Legends remake Retold a couple of months "
        "to make sure it's worthy"
    )
    shared = len(short & padded)
    jaccard = shared / len(short | padded)
    assert jaccard < 0.4, "Jaccard would reject this pair"
    assert containment(short, padded) > 0.75


def test_multi_outlet_writeups_merge_without_swallowing_other_stories(article_factory):
    # Regression, in both directions at once. Under Jaccard these six split into
    # five clusters. Under a loose threshold with transitive closure they became
    # one blob that also swallowed the day's Rayman delay. Neither is acceptable.
    writeups = [
        ("gamerant", "Game Rant", "Marathon Season 3 Officially Delayed"),
        (
            "pushsquare",
            "Push Square",
            "Marathon's Future Questioned as Bungie Delays Big Update and Scraps Seasonal Schedule",
        ),
        (
            "gamesradar",
            "GamesRadar",
            "Bungie ends Marathon's seasonal schedule, delays a September update to December, and makes it more like Destiny 2",
        ),
        (
            "pcgamer",
            "PC Gamer",
            "Bungie delays next Marathon update and ends strict seasonal schedule as it goes all-in on Destiny-like features",
        ),
        (
            "eurogamer",
            "Eurogamer",
            "Marathon's permanent PvE mode and next major update delayed to December days before launch, and Bungie is moving away from a strict season schedule",
        ),
        (
            "kotaku",
            "Kotaku",
            "Marathon Delays New Content And Ditches Seasons As Bungie's Extraction Shooter Hits Turbulence",
        ),
    ]
    articles = [
        article_factory(title, source_id=source_id, source_name=name)
        for source_id, name, title in writeups
    ]
    unrelated = article_factory(
        "Rayman Legends Retold delayed to December 3",
        source_id="gematsu",
        source_name="Gematsu",
    )

    stories = cluster_articles(articles + [unrelated])

    marathon = [
        story
        for story in stories
        if any("marathon" in article.title.lower() for article in story.articles)
    ]
    # They collapse substantially rather than staying six singletons...
    assert len(marathon) < len(writeups)
    assert max(len(story.articles) for story in marathon) >= 3
    # ...and nothing unrelated is dragged in.
    for story in marathon:
        assert all(
            "rayman" not in article.title.lower() for article in story.articles
        )
    # The unrelated story stands alone.
    rayman = next(
        story
        for story in stories
        if any("rayman" in article.title.lower() for article in story.articles)
    )
    assert len(rayman.articles) == 1


# -- primary selection -------------------------------------------------------


def test_primary_avoids_google_news_redirects(article_factory):
    redirect = article_factory(
        "Steam Deck 2 plans unaffected by component crisis",
        source_id="gamedeveloper",
        source_name="Game Developer",
        tier="trade",
        url="https://news.google.com/rss/articles/ABC",
        summary=None,
        minutes_ago=5,
    )
    readable = article_factory(
        "Steam Deck 2 plans aren't affected by component crisis",
        source_id="eurogamer",
        url="https://eurogamer.net/steam-deck-2",
        minutes_ago=30,
    )
    stories = cluster_articles([redirect, readable])
    assert len(stories) == 1
    # Higher tier, but a redirect with no summary must not be what a reader lands on.
    assert stories[0].primary.source_id == "eurogamer"


def test_primary_prefers_authority_among_readable_sources(article_factory):
    enthusiast = article_factory(
        "Studio X lays off staff", source_id="eurogamer", tier="enthusiast", minutes_ago=5
    )
    trade = article_factory(
        "Studio X lays off staff",
        source_id="gamesindustry",
        source_name="GamesIndustry.biz",
        tier="trade",
        minutes_ago=60,
    )
    stories = cluster_articles([enthusiast, trade])
    assert stories[0].primary.tier == "trade"


# -- story shape -------------------------------------------------------------


def test_story_topics_are_unioned_and_ordered(article_factory):
    first = article_factory(
        "Studio lays off staff",
        source_id="eurogamer",
        topics=["Business & earnings"],
        entities=["PlayStation 5"],
    )
    second = article_factory(
        "Studio lays off staff",
        source_id="ign",
        source_name="IGN",
        topics=["Layoffs & closures"],
        entities=["Ubisoft"],
    )
    stories = cluster_articles([first, second])
    assert len(stories) == 1
    assert stories[0].topics == ["Layoffs & closures", "Business & earnings"]
    assert set(stories[0].entities) == {"PlayStation 5", "Ubisoft"}


def test_also_covered_by_excludes_primary(article_factory):
    first = article_factory("Sony acquires Housemarque", source_id="eurogamer")
    second = article_factory(
        "Sony acquires Housemarque", source_id="ign", source_name="IGN"
    )
    story = cluster_articles([first, second])[0]
    assert len(story.also_covered_by) == 1
    assert story.also_covered_by[0].id != story.primary.id


def test_stories_sorted_newest_first(article_factory):
    old = article_factory(
        "Studio confirms layoffs in Montreal", source_id="eurogamer", minutes_ago=600
    )
    fresh = article_factory(
        "Publisher delays its next game to March",
        source_id="ign",
        source_name="IGN",
        minutes_ago=5,
    )
    stories = cluster_articles([old, fresh])
    assert len(stories) == 2
    assert stories[0].title == "Publisher delays its next game to March"


def test_empty_input():
    assert cluster_articles([]) == []


# -- primitives --------------------------------------------------------------


def test_tokens_are_stemmed_so_tense_variants_collide():
    # Without this, "delayed" and "delays" are different words and the same
    # announcement written up twice reads as two stories.
    assert title_tokens("Game delayed again") == title_tokens("Game delays again")


def test_apostrophes_do_not_leak_into_tokens():
    assert title_tokens("Light's edge") == title_tokens("Light edge")


def test_stopwords_are_dropped():
    assert "the" not in title_tokens("The new game news")
    assert "game" not in title_tokens("The new game news")


def test_moderate_similarity_pair_is_still_merged(article_factory):
    # Regression: banded MinHash candidate generation never proposed this pair,
    # so the same 007 delay appeared four times in a single digest.
    first = article_factory(
        "007 First Light for Switch 2 delayed to March 2027",
        source_id="nintendolife",
        source_name="Nintendo Life",
    )
    second = article_factory(
        "007 First Light Delayed On Switch 2 Again, Will Now Arrive Almost A Year Later",
        source_id="pushsquare",
        source_name="Push Square",
    )
    stories = cluster_articles([first, second])
    assert len(stories) == 1
    assert stories[0].breadth == 2


def test_tense_variants_merge(article_factory):
    first = article_factory(
        "Rayman Legends Retold delayed to December 3", source_id="eurogamer"
    )
    second = article_factory(
        "Ubisoft delays Rayman Legends remake Retold a couple of months",
        source_id="ign",
        source_name="IGN",
    )
    assert len(cluster_articles([first, second])) == 1


def test_articles_link_to_the_anchor_not_to_any_member(article_factory):
    # A~B and B~C must not drag A and C together. Single-linkage clustering did
    # exactly that, sweeping a TCG Card Shop story into the Steam Frame cluster
    # and the whole Marathon delay into the Rayman one.
    anchor = article_factory(
        "Valve opens waiting list for Steam Frame starting at 1059",
        source_id="gamesindustry",
        source_name="GamesIndustry.biz",
        url="https://x.test/a",
        minutes_ago=180,
    )
    middle = article_factory(
        "Steam Frame review",
        source_id="eurogamer",
        url="https://x.test/b",
        minutes_ago=120,
    )
    unrelated = article_factory(
        "Steam review roundup for indie games",
        source_id="rps",
        source_name="RPS",
        url="https://x.test/c",
        minutes_ago=60,
    )
    stories = cluster_articles([anchor, middle, unrelated])

    assert len(stories) == 2
    sizes = sorted(len(story.articles) for story in stories)
    assert sizes == [1, 2]
    loner = next(story for story in stories if len(story.articles) == 1)
    assert loner.title == "Steam review roundup for indie games"


def test_exact_identity_joins_regardless_of_the_time_window(article_factory):
    # The same URL is not a judgement call, so the window does not apply.
    first = article_factory(
        "A story entirely", url="https://x.test/same", minutes_ago=10
    )
    second = article_factory(
        "A different headline entirely",
        url="https://x.test/same",
        source_id="ign",
        source_name="IGN",
        minutes_ago=10 + 72 * 60,
    )
    assert len(cluster_articles([first, second])) == 1


# -- acceptance rule ---------------------------------------------------------


def test_acceptance_rejects_a_single_common_shared_token():
    from collections import Counter

    from telescope.cluster import _accepts_pair

    df = Counter({"switch": 50, "nintendo": 30, "vrr": 40, "kirby": 12})
    # Shares only "switch", which is everywhere in this window.
    assert not _accepts_pair(
        frozenset({"switch", "nintendo"}),
        frozenset({"switch", "kirby"}),
        document_frequency=df,
        distinctive_cutoff=7,
        threshold=0.5,
    )


def test_acceptance_rejects_common_tokens_even_when_fully_contained():
    from collections import Counter

    from telescope.cluster import _accepts_pair

    # "Steam Frame review" is entirely contained in the longer headline, so
    # containment passes — but every shared token is common in a Steam-heavy
    # window, so it proves nothing about the subject.
    df = Counter(
        {"steam": 90, "frame": 60, "review": 40, "valve": 50, "wait": 20, "list": 20}
    )
    assert not _accepts_pair(
        frozenset({"steam", "frame", "review"}),
        frozenset({"steam", "frame", "valve", "wait", "list"}),
        document_frequency=df,
        distinctive_cutoff=7,
        threshold=0.5,
    )


def test_acceptance_allows_a_distinctive_contained_pair():
    from collections import Counter

    from telescope.cluster import _accepts_pair

    df = Counter(
        {
            "bungie": 6,
            "marathon": 6,
            "seasonal": 5,
            "schedule": 6,
            "delay": 40,
            "update": 60,
            "end": 10,
            "september": 20,
        }
    )
    assert _accepts_pair(
        frozenset({"bungie", "marathon", "seasonal", "schedule", "delay", "end"}),
        frozenset(
            {"bungie", "marathon", "seasonal", "schedule", "delay", "update", "september"}
        ),
        document_frequency=df,
        distinctive_cutoff=7,
        threshold=0.5,
    )


def test_acceptance_still_requires_containment():
    from collections import Counter

    from telescope.cluster import _accepts_pair

    df = Counter({"nintendo": 5, "switch": 200, "kirby": 4, "vrr": 4})
    # A distinctive shared token is not enough on its own: these two are not the
    # same story, and containment says so.
    assert not _accepts_pair(
        frozenset({"nintendo", "switch", "vrr", "docked", "update"}),
        frozenset({"nintendo", "switch", "kirby", "spring", "trailer"}),
        document_frequency=df,
        distinctive_cutoff=7,
        threshold=0.5,
    )

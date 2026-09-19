"""Story clustering: recognise the same event as covered by many outlets.

Similarity is judged by **containment with a distinctive shared token**::

    containment = |shared| / min(|a|, |b|)

Jaccard punishes length differences, and real headlines are long and padded. The
same Bungie delay written up six ways shares "bungie", "marathon", "seasonal",
"schedule" and "delay" but differs in a dozen filler words — a Jaccard of 0.27,
below any usable threshold, while containment sits at 0.5 and a human would call
it one story instantly. Containment asks the question that actually matters: is
the shorter headline's content present in the longer one?

Requiring one *distinctive* shared token on top of that is what stops two
unrelated headlines merging because both happen to contain "switch".

**Articles join a cluster's anchor, not any member.** A loose per-pair threshold
with transitive closure is single-linkage clustering, and it blobs: A~B and B~C
dragged A and C together, which in testing swept a TCG Card Shop story into the
Steam Frame cluster and the entire Marathon delay into the Rayman one. Star
clustering around an anchor keeps the tolerance for rewording without letting
unrelated stories chain.

Two other choices worth not undoing:

* **Candidate generation is an inverted index, not MinHash/LSH.** A banded LSH
  scheme was tried first and silently dropped roughly a fifth of true pairs at
  moderate similarity — the same 007 delay showed up four times in one digest.
  Headlines are short enough that exact token-set comparison is cheap, and exact
  beats approximate when a miss is visible in the output.
* **Tokens are crudely stemmed**, so "delays" and "delayed" collide. Without it
  the same announcement written up twice reads as two stories.

Known limitations, both accepted deliberately because under-merging is the safer
failure — a story shown twice is annoying, a story swallowed by an unrelated
cluster is invisible:

* The document-frequency test is bypassed for a *near-identical* pair, because a
  game covered by thirty outlets makes its own name too common to count as
  distinctive — the case where aggregation matters most. A pair that is merely
  paraphrased rather than near-identical is still judged by
  ``distinctive_token_df_ratio``, so two headlines sharing only generic words do
  not merge.
* Containment is measured against the anchor, so a very padded anchor headline
  can leave a terser rewrite standing alone. Anchoring on the oldest coverage is
  the journalistically sensible default; re-electing a shorter anchor was tried
  and it re-introduced blobs.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from .config import ClusteringConfig
from .models import Article
from .taxonomy import DEFAULT_TOPIC_ORDER

# Ordered best-first. Used both to pick a story's primary article and to score
# it, so the ordering lives in one place.
TIER_ORDER: tuple[str, ...] = (
    "first_party",
    "trade",
    "enthusiast",
    "aggregator",
    "community",
)

STOPWORDS = frozenset(
    """
    a an the and or but if then than that this these those is are was were be been
    being to of in on at by for with from into over after before about as it its
    their there here they them we you your his her he she not no nor do does did
    done has have had will would can could should may might must say says said
    new news report reports update updates video game games gaming
    where how why what when who whom which read find
    """.split()
)
# The final line is not filler. Those are function words that carry no topic,
# and because they are rare in a feed they passed the "distinctive token" test
# and bridged unrelated stories — "where" on its own was enough to pull a
# "where to find X" guide into a review cluster.

# Hyphens separate tokens rather than joining them. "Round-Up", "Round Up" and
# "roundup" must tokenise alike or the same story written two ways never merges;
# the same applies to "day one"/"day-one" and "co-op". Apostrophes are dropped
# for the same reason: "Light's" becomes "light", never "light'".
_TOKEN_RE = re.compile(r"[a-z0-9]+")
MIN_TOKEN_LEN = 3

# A pair this close is one headline with words added, not a judgement call, so
# document frequency must not veto it. See ``_accepts_pair``.
NEAR_IDENTICAL_CONTAINMENT = 0.9
NEAR_IDENTICAL_TOKENS = 3


def _stem(token: str) -> str:
    """Crush the plural and tense variants that headline rewrites introduce.

    Deliberately crude. A full Porter stemmer would be more accurate, but this
    is auditable, has no dependency, and covers the cases that actually occur.
    """
    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 4 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str | None) -> list[str]:
    """Lowercase content tokens: stopwords, noise and inflections removed."""
    return [
        _stem(token)
        for token in _TOKEN_RE.findall((text or "").lower())
        if len(token) >= MIN_TOKEN_LEN and token not in STOPWORDS
    ]


def title_tokens(title: str | None) -> frozenset[str]:
    """Token *set*, not sequence: headline rewrites reorder words constantly."""
    return frozenset(tokenize(title))


def containment(a: frozenset[str], b: frozenset[str]) -> float:
    """Fraction of the shorter headline's content tokens present in the other."""
    if not a or not b:
        return 0.0
    shared = len(a & b)
    if not shared:
        return 0.0
    return shared / min(len(a), len(b))


def content_key(article: Article) -> str:
    """Identity for syndicated copy: same words, order-independent."""
    tokens = title_tokens(article.title)
    if not tokens:
        return ""
    canonical = " ".join(sorted(tokens))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=8).hexdigest()


def _accepts_pair(
    token_a: frozenset[str],
    token_b: frozenset[str],
    *,
    document_frequency: Counter[str],
    distinctive_cutoff: int,
    threshold: float,
) -> bool:
    """Decide whether two headlines describe the same event.

    Containment answers "is the shorter headline's content present in the
    longer one", which tolerates the padding that separates two write-ups of one
    event. The distinctive-token requirement answers "are they about the same
    thing at all", which is what stops two unrelated headlines merging just
    because both contain a popular word.

    The distinctive test is skipped for a near-identical pair. Document
    frequency is a measure of *coverage*, not of sameness, so on a heavily
    covered story it inverts: once thirty outlets write about one game, the
    game's own name stops counting as distinctive and every review of it stands
    alone. Seven write-ups of a single review embargo failed to merge that way,
    even when two headlines were word-for-word identical.
    """
    shared = token_a & token_b
    if not shared:
        return False
    score = containment(token_a, token_b)
    if score < threshold:
        return False
    if score >= NEAR_IDENTICAL_CONTAINMENT and len(shared) >= NEAR_IDENTICAL_TOKENS:
        return True
    return any(document_frequency[token] <= distinctive_cutoff for token in shared)


def _looks_like_redirect(url: str) -> bool:
    return "news.google.com" in (url or "")


def _primary_key(article: Article) -> tuple:
    """Ordering that picks the best article to *show* for a story.

    Readability beats authority here, deliberately. A Google News item is a
    redirect with no summary — landing a reader on one when a real publisher
    link sits in the same cluster is the worst outcome available. So redirects
    sort last, then articles with summaries win, and only then does authority
    (and finally whoever broke it) decide.
    """
    return (
        1 if _looks_like_redirect(article.url) else 0,
        0 if article.summary else 1,
        TIER_ORDER.index(article.tier) if article.tier in TIER_ORDER else len(TIER_ORDER),
        article.effective_published,
    )


@dataclass
class Story:
    """One event, as covered by one or more outlets."""

    articles: list[Article]
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)

    @property
    def primary(self) -> Article:
        return min(self.articles, key=_primary_key)

    @property
    def title(self) -> str:
        return self.primary.title

    @property
    def url(self) -> str:
        return self.primary.url

    @property
    def summary(self) -> str | None:
        # Prefer the primary's summary, but fall back to any member that has
        # one rather than showing nothing.
        if self.primary.summary:
            return self.primary.summary
        for article in sorted(self.articles, key=_primary_key):
            if article.summary:
                return article.summary
        return None

    @property
    def published_at(self) -> datetime:
        return self.primary.effective_published

    @property
    def tier(self) -> str:
        return self.primary.tier

    @property
    def source_ids(self) -> set[str]:
        return {article.source_id for article in self.articles}

    @property
    def breadth(self) -> int:
        """Distinct outlets. The strongest signal available without a model."""
        return len(self.source_ids)

    @property
    def sources(self) -> list[str]:
        ordered = sorted(self.articles, key=_primary_key)
        names: list[str] = []
        for article in ordered:
            if article.source_name not in names:
                names.append(article.source_name)
        return names

    @property
    def also_covered_by(self) -> list[Article]:
        primary = self.primary
        return [a for a in sorted(self.articles, key=_primary_key) if a.id != primary.id]

    @property
    def topics(self) -> list[str]:
        found: list[str] = []
        for article in self.articles:
            for topic in article.topics:
                if topic not in found:
                    found.append(topic)
        found.sort(
            key=lambda t: DEFAULT_TOPIC_ORDER.index(t)
            if t in DEFAULT_TOPIC_ORDER
            else len(DEFAULT_TOPIC_ORDER)
        )
        return found

    @property
    def entities(self) -> list[str]:
        found: list[str] = []
        for article in self.articles:
            for name in article.entities:
                if name not in found:
                    found.append(name)
        return found

    def has_tier(self, tier: str) -> bool:
        return any(article.tier == tier for article in self.articles)

    def searchable_text(self) -> str:
        parts = [self.primary.title]
        if self.summary:
            parts.append(self.summary)
        return " ".join(parts)


def cluster_articles(
    articles: list[Article], config: ClusteringConfig | None = None
) -> list[Story]:
    """Group articles into stories, newest first.

    Articles are processed oldest-first and each joins the best-matching
    existing *anchor*, or becomes a new anchor. Exact identity — same canonical
    URL, or the same multiset of headline tokens — always joins, because that is
    not a judgement call.
    """
    config = config or ClusteringConfig()
    if not articles:
        return []

    count = len(articles)
    tokens = [title_tokens(article.title) for article in articles]

    document_frequency: Counter[str] = Counter()
    for token_set in tokens:
        document_frequency.update(token_set)

    index_cutoff = max(4, int(config.candidate_token_df_ratio * count))
    distinctive_cutoff = max(3, int(config.distinctive_token_df_ratio * count))
    window_seconds = config.window_hours * 3600

    # Oldest first, id as a stable tiebreak, so the breaking story anchors the
    # cluster and reruns produce identical groupings.
    order = sorted(
        range(count),
        key=lambda index: (articles[index].effective_published, articles[index].id),
    )

    members: dict[int, list[int]] = {}
    anchor_of: dict[int, int] = {}
    postings: dict[str, list[int]] = {}
    by_url: dict[str, int] = {}
    by_content: dict[str, int] = {}

    for index in order:
        article = articles[index]
        token_set = tokens[index]
        url_key = article.canonical_url or ""
        content = content_key(article)

        # -- exact identity ------------------------------------------------
        target: int | None = None
        if url_key and url_key in by_url:
            target = by_url[url_key]
        elif content and content in by_content:
            target = by_content[content]

        # -- best matching anchor -----------------------------------------
        if target is None and token_set:
            best: int | None = None
            best_score = 0.0
            considered: set[int] = set()

            for token in token_set:
                if document_frequency[token] > index_cutoff:
                    continue
                for candidate in postings.get(token, ()):
                    if candidate in considered:
                        continue
                    considered.add(candidate)

                    anchor_index = anchor_of[candidate]
                    anchor = articles[anchor_index]
                    if (
                        abs(
                            (
                                anchor.effective_published
                                - article.effective_published
                            ).total_seconds()
                        )
                        > window_seconds
                    ):
                        continue

                    same_source = anchor.source_id == article.source_id
                    threshold = (
                        config.same_source_containment_threshold
                        if same_source
                        else config.containment_threshold
                    )
                    if not _accepts_pair(
                        tokens[anchor_index],
                        token_set,
                        document_frequency=document_frequency,
                        distinctive_cutoff=distinctive_cutoff,
                        threshold=threshold,
                    ):
                        continue

                    score = containment(tokens[anchor_index], token_set)
                    if score > best_score or (score == best_score and best is not None and candidate < best):
                        best, best_score = candidate, score

            target = best

        # -- new anchor ----------------------------------------------------
        if target is None:
            target = len(members)
            members[target] = []
            anchor_of[target] = index
            for token in token_set:
                if document_frequency[token] <= index_cutoff:
                    postings.setdefault(token, []).append(target)

        members[target].append(index)
        if url_key:
            by_url.setdefault(url_key, target)
        if content:
            by_content.setdefault(content, target)

    stories = [
        Story(articles=[articles[index] for index in indexes])
        for indexes in members.values()
    ]
    stories.sort(key=lambda story: story.published_at, reverse=True)
    return stories

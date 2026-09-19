"""Markdown digest renderer.

The digest is the product, and it is organised the way the web viewer's Stories
page is: one entry per **story**, not one line per link. Each entry is an
aggregated event — the headline a reader should land on, how many outlets
carried it, and the other reports listed underneath — so a story covered by a
dozen sites reads once instead of a dozen times.

Sections are topics from ``config/watchlist.yaml``, each capped at
``topic_limit`` stories. A story may legitimately belong to more than one topic,
so it can appear in more than one section; that is deliberate, and it is what
keeps a layoff story visible under both "Layoffs & closures" and "Legal &
labour" rather than filed under whichever rule won.

Any story not already shown is picked up by "Everything else", so a story that
matches no topic is not silently dropped.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..cluster import Story
from ..config import DigestConfig, Watchlist
from ..health import FAILING, NEVER_FETCHED, STALE, HealthReport
from ..taxonomy import DEFAULT_TOPIC_ORDER

_MD_ESCAPE_RE = re.compile(r"([\\`*_\[\]<>|])")


def resolve_timezone(name: str) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return timezone.utc  # a bad timezone must not stop the digest


def _esc(text: str) -> str:
    return _MD_ESCAPE_RE.sub(r"\\\1", text or "")


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


def _age(published: datetime, now: datetime) -> str:
    hours = max(0.0, (now - published).total_seconds() / 3600.0)
    if hours < 1:
        return f"{hours * 60:.0f}m ago"
    if hours < 48:
        return f"{hours:.1f}h ago"
    return f"{hours / 24:.1f}d ago"


def _age_hours(hours: float | None) -> str:
    if hours is None:
        return "unknown"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _coverage(story: Story) -> str:
    """`Eurogamer +4 others`, or just the source when it stands alone."""
    sources = story.sources
    if not sources:
        return "unknown"
    others = len(sources) - 1
    if others <= 0:
        return sources[0]
    return f"{sources[0]} +{others} other" + ("s" if others > 1 else "")


def _story_block(story: Story, now: datetime, config: DigestConfig) -> list[str]:
    """One story: the entry the reader lands on, plus every other report.

    The other reports are *listed*, not merely counted. A bare "+11 others" the
    reader has to click through is exactly what made the old digest read as a
    list of links instead of a brief.
    """
    lines = [f"**[{_esc(story.title)}]({story.url})**"]
    lines.append(f"{_esc(_coverage(story))} · {_age(story.published_at, now)}")

    summary = _clip(story.summary or "", config.summary_chars)
    if summary:
        lines.append(f"> {summary}")

    others = story.also_covered_by
    if others:
        lines.append("")
        lines.append(f"*{_plural(len(others), 'other report')}*")
        for article in others:
            lines.append(
                f"- [{_esc(article.title)}]({article.url}) — "
                f"{_esc(article.source_name)} · {_age(article.effective_published, now)}"
            )

    lines.append("")
    return lines


def _topics(watchlist: Watchlist | None) -> list[str]:
    """Section order: the configured watchlist topics, else the built-in order."""
    if watchlist and watchlist.topics:
        return list(watchlist.topics)
    return list(DEFAULT_TOPIC_ORDER)


def render_digest(
    *,
    stories: list[Story],
    health: HealthReport,
    window_start: datetime,
    window_end: datetime,
    generated_at: datetime,
    config: DigestConfig,
    watchlist: Watchlist | None = None,
) -> str:
    """Render the whole digest as markdown."""
    tz = resolve_timezone(config.timezone)
    local_now = generated_at.astimezone(tz)
    local_start = window_start.astimezone(tz)
    local_end = window_end.astimezone(tz)
    window_hours = max(0.0, (window_end - window_start).total_seconds() / 3600.0)
    tz_label = getattr(tz, "key", None) or "UTC"

    displayed: set[str] = set()

    lines: list[str] = []
    lines.append(f"# Games industry digest — {local_now:%A %d %B %Y}")
    lines.append("")
    lines.append(
        f"**Window** {local_start:%d %b %H:%M} → {local_end:%d %b %H:%M} "
        f"{tz_label} ({window_hours:.0f}h)  "
    )
    lines.append(
        f"**Volume** {_plural(health.items_considered, 'item')} → "
        f"{_plural(health.stories, 'story', 'stories')}  "
    )
    lines.append(f"**Sources** {health.summary_line}  ")
    lines.append("")

    if health.everything_failed:
        lines.append("> **Every source failed.** This is a pipeline failure, not a quiet news day.")
        lines.append("> The window has not been advanced, so nothing will be lost once a sync succeeds.")
        lines.append("")

    if not stories:
        lines.append("## Nothing new")
        lines.append("")
        lines.append(
            "No items fell inside this window. Either it was genuinely quiet, or the "
            "last sync did not run — check `telescope sources`."
        )
        lines.append("")
    elif len(stories) < config.quiet_day_threshold:
        lines.append(
            f"*Quiet window — only {_plural(len(stories), 'story', 'stories')} in total.*"
        )
        lines.append("")

    # -- topic sections ----------------------------------------------------
    # A story can appear under more than one topic and that is intentional: it
    # is better to meet a layoff story twice, once under "Layoffs & closures"
    # and once under "Legal & labour", than to bury it under one of them.
    limit = max(0, config.topic_limit)
    topic_order = _topics(watchlist)
    for topic in topic_order:
        topic_stories = [story for story in stories if topic in story.topics][:limit]
        if not topic_stories:
            continue
        lines.append(f"## {topic}")
        lines.append("")
        for story in topic_stories:
            lines.extend(_story_block(story, generated_at, config))
            displayed.add(story.primary.id)

    # -- everything else ---------------------------------------------------
    # Whatever the topic sections did not show. This is what stops a story that
    # matches no topic rule at all from vanishing from the digest.
    remainder = [
        story for story in stories if story.primary.id not in displayed
    ][: max(0, config.long_tail_limit)]
    if remainder:
        lines.append("## Everything else")
        lines.append("")
        for story in remainder:
            lines.extend(_story_block(story, generated_at, config))

    # -- source health -----------------------------------------------------
    lines.append("## Source health")
    lines.append("")
    lines.append(f"- {health.summary_line}")
    if health.last_sync_at:
        lines.append(f"- last sync {_esc(health.last_sync_at)}")

    for source in health.sources:
        if source.state == STALE:
            lines.append(
                f"- ⚠ **{_esc(source.name)}** — stale, newest item "
                f"{_age_hours(source.item_age_hours)} old"
            )
    for source in health.sources:
        if source.state in (FAILING, NEVER_FETCHED):
            reason = source.last_error or source.state.replace("_", " ")
            lines.append(
                f"- ✖ **{_esc(source.name)}** — {_esc(reason)} "
                f"(x{source.consecutive_failures})"
            )

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        f"Generated {local_now:%Y-%m-%d %H:%M} {tz_label} · "
        f"{_plural(health.total, 'source')} configured · ranked by rules in `config/digest.yaml`"
    )
    return "\n".join(lines)

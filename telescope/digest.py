"""Digest orchestration: window, cluster, rank, render, write.

The window is driven by a stored watermark, never by wall-clock time. A missed
run — laptop asleep, job failed, machine off for a week — covers the gap on the
next run instead of silently losing those stories. A double run produces an
empty digest rather than duplicates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from .cluster import Story, cluster_articles
from .config import ConfigError, DigestConfig, Settings, Watchlist
from .deliver import post_webhook
from .health import HealthReport, build_health
from .rank import rank_stories
from .render.markdown import render_digest, resolve_timezone
from .store import Store, iso, parse_iso, utcnow

LAST_DIGEST_AT = "last_digest_at"
LAST_DIGEST_PATH = "last_digest_path"
LAST_SYNC_AT = "last_sync_at"


@dataclass
class DigestResult:
    markdown: str
    window_start: datetime
    window_end: datetime
    generated_at: datetime
    stories: list[Story]
    health: HealthReport
    config: DigestConfig
    watchlist: Watchlist
    path: Path | None = None
    marked: bool = False
    webhook_ok: bool | None = None
    webhook_error: str | None = None

    @property
    def window_hours(self) -> float:
        return max(0.0, (self.window_end - self.window_start).total_seconds() / 3600.0)

    @property
    def top_stories(self) -> list[Story]:
        return self.stories[: max(0, self.config.max_top_stories)]


def webhook_payload(result: DigestResult) -> dict:
    """A small JSON summary — enough to render a notification, not the digest."""
    return {
        "date": digest_filename(result).removesuffix(".md"),
        "generated_at": iso(result.generated_at),
        "path": str(result.path) if result.path else None,
        "window": {
            "start": iso(result.window_start),
            "end": iso(result.window_end),
            "hours": round(result.window_hours, 2),
        },
        "volume": {
            "items": result.health.items_considered,
            "stories": len(result.stories),
        },
        "sources": {
            "ok": result.health.ok_count,
            "total": result.health.total,
            "stale": len(result.health.stale),
            "failing": len(result.health.failing),
            "everything_failed": result.health.everything_failed,
        },
        "top_stories": [
            {
                "title": story.title,
                "url": story.url,
                "score": round(story.score, 2),
                "breadth": story.breadth,
                "sources": story.sources[:6],
                "topics": story.topics,
            }
            for story in result.top_stories
        ],
    }


def resolve_window(
    store: Store,
    config: DigestConfig,
    *,
    now: datetime,
    since: datetime | None = None,
    window_hours: float | None = None,
) -> tuple[datetime, datetime]:
    """Work out what period this digest covers."""
    if since is not None:
        return (since, now)

    recorded = parse_iso(store.get_state(LAST_DIGEST_AT))
    if recorded is not None and recorded < now:
        return (recorded, now)

    hours = window_hours if window_hours is not None else config.window_hours_default
    return (now - timedelta(hours=max(0.0, hours)), now)


def build_digest(
    settings: Settings,
    store: Store,
    *,
    now: datetime | None = None,
    since: datetime | None = None,
    window_hours: float | None = None,
) -> DigestResult:
    """Produce a rendered digest without touching the watermark or disk."""
    config = settings.load_digest_config()
    watchlist = settings.load_watchlist()
    now = now or utcnow()

    start, end = resolve_window(
        store, config, now=now, since=since, window_hours=window_hours
    )

    articles = store.items_between(start, end)
    stories = cluster_articles(articles, config.clustering)
    ranked = rank_stories(stories, config=config, watchlist=watchlist, now=now)

    health = build_health(
        store.list_sources(),
        items_considered=len(articles),
        stories=len(ranked),
        last_sync_at=store.get_state(LAST_SYNC_AT),
    )

    markdown = render_digest(
        stories=ranked,
        health=health,
        window_start=start,
        window_end=end,
        generated_at=now,
        config=config,
        watchlist=watchlist,
    )

    return DigestResult(
        markdown=markdown,
        window_start=start,
        window_end=end,
        generated_at=now,
        stories=ranked,
        health=health,
        config=config,
        watchlist=watchlist,
    )


def digest_filename(result: DigestResult) -> str:
    """Local-date filename: the date the reader sees, not UTC."""
    tz = resolve_timezone(result.config.timezone)
    return f"{result.generated_at.astimezone(tz):%Y-%m-%d}.md"


def write_digest(
    result: DigestResult, directory: Path, *, also_latest: bool = True
) -> Path:
    """Write the digest. Write-once, human-readable files only."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / digest_filename(result)
    path.write_text(result.markdown, encoding="utf-8")
    if also_latest:
        (directory / "latest.md").write_text(result.markdown, encoding="utf-8")
    return path


def run_digest(
    settings: Settings,
    store: Store,
    *,
    out_dir: Path | str | None = None,
    since: datetime | None = None,
    window_hours: float | None = None,
    now: datetime | None = None,
    write: bool = True,
    mark: bool = True,
    notify: bool = True,
    client: httpx.Client | None = None,
) -> DigestResult:
    """Build, write, and record a digest."""
    result = build_digest(
        settings, store, now=now, since=since, window_hours=window_hours
    )

    if not write:
        return result

    directory = Path(out_dir).expanduser() if out_dir else settings.digests_dir
    if directory is None:
        raise ConfigError(
            "no digest output directory configured; set output_dir in "
            "config/digest.yaml or the TELESCOPE_DIGEST_DIR environment variable"
        )

    result.path = write_digest(result, directory)
    store.record_digest(
        generated_at=result.generated_at,
        window_start=result.window_start,
        window_end=result.window_end,
        path=str(result.path),
        items_considered=result.health.items_considered,
        stories=len(result.stories),
        sources_ok=result.health.ok_count,
        sources_total=result.health.total,
        stale_sources=len(result.health.stale),
    )

    # Advance the watermark only if something actually worked. If every source
    # failed, advancing would skip this window forever and lose whatever news
    # lands in it once the feeds recover.
    if mark and not result.health.everything_failed:
        store.set_state(LAST_DIGEST_AT, iso(result.generated_at))
        store.set_state(LAST_DIGEST_PATH, str(result.path))
        result.marked = True

    # Notification is best-effort and last: the digest is already on disk, so a
    # failed ping must not make a successful run look like a failure.
    if notify and result.config.webhook_url:
        result.webhook_ok, result.webhook_error = post_webhook(
            result.config.webhook_url, webhook_payload(result), client=client
        )

    return result

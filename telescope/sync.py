"""Sync orchestration: fetch, parse, normalize, store, log.

Every source is isolated. One broken feed degrades the run and lands in the
fetch log; it never takes the run down with it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .adapters.rss import FeedParseError, parse_feed
from .config import Settings, Source
from .fetch import build_client, fetch_feed
from .models import NormalizedItem
from .normalize import normalize_entries
from .store import FetchRecord, Store, iso, utcnow


@dataclass
class SourceResult:
    """What happened with one source during a sync."""

    source_id: str
    name: str
    status: str
    http_status: int | None = None
    items_found: int = 0
    items_new: int = 0
    items_duplicate: int = 0
    items_unparsed_dates: int = 0
    newest_published_at: datetime | None = None
    item_age_hours: float | None = None
    duration_ms: int = 0
    error: str | None = None
    warning: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "not_modified")


@dataclass
class SyncReport:
    """Result of syncing every selected source."""

    started_at: datetime
    finished_at: datetime
    results: list[SourceResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> list[SourceResult]:
        return [r for r in self.results if not r.ok]

    @property
    def total_found(self) -> int:
        return sum(r.items_found for r in self.results)

    @property
    def total_new(self) -> int:
        return sum(r.items_new for r in self.results)

    @property
    def all_failed(self) -> bool:
        return bool(self.results) and self.ok_count == 0

    @property
    def duration_ms(self) -> int:
        return max(0, int((self.finished_at - self.started_at).total_seconds() * 1000))


def _newest(items: list[NormalizedItem]) -> NormalizedItem | None:
    dated = [i for i in items if i.published_at is not None]
    if not dated:
        return None
    return max(dated, key=lambda i: i.published_at)  # type: ignore[arg-type,return-value]


def _age_hours(published_at: datetime | None, now: datetime) -> float | None:
    if published_at is None:
        return None
    return max(0.0, (now - published_at).total_seconds() / 3600.0)


def sync_source(
    source: Source,
    store: Store,
    *,
    client: httpx.Client,
    now: datetime | None = None,
    sleep=time.sleep,
) -> SourceResult:
    """Fetch and ingest one source."""
    now = now or utcnow()
    state = store.get_source(source.id)
    started = utcnow()

    outcome = fetch_feed(
        source.url,
        client=client,
        etag=state.etag if state else None,
        last_modified=state.last_modified if state else None,
        sleep=sleep,
    )

    # 304: the source is healthy and simply has nothing new. Still a success,
    # and it still refreshes last_success_at.
    if outcome.status == "not_modified":
        finished = utcnow()
        store.record_fetch(
            FetchRecord(
                source_id=source.id,
                started_at=started,
                finished_at=finished,
                status="not_modified",
                success=True,
                http_status=304,
                etag=outcome.etag,
                last_modified=outcome.last_modified,
            )
        )
        return SourceResult(
            source_id=source.id,
            name=source.name,
            status="not_modified",
            http_status=304,
            duration_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )

    if not outcome.ok or outcome.body is None:
        finished = utcnow()
        store.record_fetch(
            FetchRecord(
                source_id=source.id,
                started_at=started,
                finished_at=finished,
                status=outcome.status,
                success=False,
                http_status=outcome.http_status,
                error=outcome.error,
            )
        )
        return SourceResult(
            source_id=source.id,
            name=source.name,
            status=outcome.status,
            http_status=outcome.http_status,
            error=outcome.error,
            duration_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )

    try:
        parsed = parse_feed(outcome.body)
    except FeedParseError as exc:
        finished = utcnow()
        store.record_fetch(
            FetchRecord(
                source_id=source.id,
                started_at=started,
                finished_at=finished,
                status="parse_error",
                success=False,
                http_status=outcome.http_status,
                error=str(exc),
            )
        )
        return SourceResult(
            source_id=source.id,
            name=source.name,
            status="parse_error",
            http_status=outcome.http_status,
            error=str(exc),
            duration_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )

    items, stats = normalize_entries(
        source, parsed.entries, fetched_at=now, feed_title=parsed.title
    )
    inserted, duplicates = store.save_items(items)

    newest = _newest(items)
    age = _age_hours(newest.published_at if newest else None, now)
    finished = utcnow()

    store.record_fetch(
        FetchRecord(
            source_id=source.id,
            started_at=started,
            finished_at=finished,
            status="ok",
            success=True,
            http_status=outcome.http_status,
            items_found=len(items),
            items_new=inserted,
            items_unparsed_dates=stats.missing_dates,
            error=None,
            etag=outcome.etag,
            last_modified=outcome.last_modified,
            item_age_hours=age,
            last_seen_guid=newest.guid if newest else None,
            last_seen_published_at=newest.published_at if newest else None,
        )
    )

    return SourceResult(
        source_id=source.id,
        name=source.name,
        status="ok",
        http_status=outcome.http_status,
        items_found=len(items),
        items_new=inserted,
        items_duplicate=duplicates,
        items_unparsed_dates=stats.missing_dates,
        newest_published_at=newest.published_at if newest else None,
        item_age_hours=age,
        duration_ms=max(0, int((finished - started).total_seconds() * 1000)),
        warning=parsed.warning,
    )


def run_sync(
    settings: Settings,
    store: Store,
    *,
    only: list[str] | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    sleep=time.sleep,
) -> SyncReport:
    """Sync every enabled source, or just those named in ``only``."""
    started = utcnow()
    now = now or started

    sources = settings.load_sources()
    store.sync_sources(sources)

    selected = [s for s in sources if s.enabled]
    if only:
        wanted = {name.strip() for name in only if name.strip()}
        known = {s.id for s in sources}
        unknown = wanted - known
        if unknown:
            raise ValueError(f"unknown source id(s): {', '.join(sorted(unknown))}")
        selected = [s for s in selected if s.id in wanted]

    owns_client = client is None
    if client is None:
        client = build_client()

    results: list[SourceResult] = []
    try:
        for source in selected:
            try:
                results.append(
                    sync_source(source, store, client=client, now=now, sleep=sleep)
                )
            except Exception as exc:  # noqa: BLE001 - isolation is the point
                finished = utcnow()
                message = f"{type(exc).__name__}: {exc}"
                store.record_fetch(
                    FetchRecord(
                        source_id=source.id,
                        started_at=started,
                        finished_at=finished,
                        status="internal_error",
                        success=False,
                        error=message,
                    )
                )
                results.append(
                    SourceResult(
                        source_id=source.id,
                        name=source.name,
                        status="internal_error",
                        error=message,
                        duration_ms=max(
                            0, int((finished - started).total_seconds() * 1000)
                        ),
                    )
                )
    finally:
        if owns_client:
            client.close()

    finished = utcnow()
    store.set_state("last_sync_at", iso(finished))

    return SyncReport(started_at=started, finished_at=finished, results=results)

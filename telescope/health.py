"""Source health for the digest footer.

Silent failure is the worst outcome for a scheduled tool. If every feed dies,
the digest must say so rather than quietly rendering an empty page — and if a
feed returns HTTP 200 with valid XML while its newest item is three months old,
that has to be visible too.
"""

from __future__ import annotations

from dataclasses import dataclass

from .store import SourceRow

OK = "ok"
STALE = "stale"
FAILING = "failing"
NEVER_FETCHED = "never_fetched"
DISABLED = "disabled"


@dataclass
class SourceHealth:
    source_id: str
    name: str
    tier: str
    state: str
    item_age_hours: float | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0

    @property
    def ok(self) -> bool:
        return self.state == OK


@dataclass
class HealthReport:
    sources: list[SourceHealth]
    items_considered: int = 0
    stories: int = 0
    last_sync_at: str | None = None

    @property
    def total(self) -> int:
        return len(self.sources)

    @property
    def ok_count(self) -> int:
        return sum(1 for source in self.sources if source.ok)

    @property
    def stale(self) -> list[SourceHealth]:
        return [source for source in self.sources if source.state == STALE]

    @property
    def failing(self) -> list[SourceHealth]:
        return [
            source
            for source in self.sources
            if source.state in (FAILING, NEVER_FETCHED)
        ]

    @property
    def everything_failed(self) -> bool:
        return bool(self.sources) and self.ok_count == 0

    @property
    def summary_line(self) -> str:
        if not self.sources:
            return "no sources enabled"
        line = f"{self.ok_count}/{self.total} sources OK"
        if self.stale:
            line += f" · {len(self.stale)} stale"
        if self.failing:
            line += f" · {len(self.failing)} failing"
        return line


def classify(row: SourceRow) -> str:
    """One state per source, most actionable first.

    Failing beats stale: a source returning 503 is a different problem from one
    that is healthy but has published nothing for a week.
    """
    if not row.enabled:
        return DISABLED
    if row.consecutive_failures > 0:
        return FAILING
    if row.last_success_at is None and row.last_fetch_at is None:
        return NEVER_FETCHED
    if row.stale():
        return STALE
    return OK


def build_health(
    rows: list[SourceRow],
    *,
    items_considered: int = 0,
    stories: int = 0,
    last_sync_at: str | None = None,
) -> HealthReport:
    sources = [
        SourceHealth(
            source_id=row.id,
            name=row.name,
            tier=row.tier,
            state=classify(row),
            item_age_hours=row.last_item_age_hours,
            last_success_at=row.last_success_at,
            last_error=row.last_error,
            consecutive_failures=row.consecutive_failures,
        )
        for row in rows
    ]
    return HealthReport(
        sources=sources,
        items_considered=items_considered,
        stories=stories,
        last_sync_at=last_sync_at,
    )

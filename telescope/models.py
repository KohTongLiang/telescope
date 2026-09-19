"""Canonical data shapes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

ItemKind = Literal["news", "release", "press_release"]


class NormalizedItem(BaseModel):
    """One feed entry mapped into the canonical schema.

    ``id`` is ``sha256(source_id + guid)``. The feed GUID is the identity, never
    the publication date: a meaningful number of publishers emit broken, absent
    or duplicated dates, and keying on them silently drops stories.
    """

    id: str
    source_id: str
    kind: ItemKind = "news"
    guid: str
    title: str
    summary: str | None = None
    url: str
    canonical_url: str
    author: str | None = None
    published_at: datetime | None = None
    fetched_at: datetime
    image_url: str | None = None
    categories: list[str] = Field(default_factory=list)
    # Derived at ingest by taxonomy.py. Recomputed (not refetched) by `reindex`
    # when the dictionaries change.
    entities: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    # Deliberately a trimmed record, not the full entry: feed bodies are
    # publisher copyright, and ingestion only needs enough to re-parse identity.
    raw: dict[str, Any] = Field(default_factory=dict)


def _as_list(raw: object) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(entry) for entry in value]


def _as_datetime(raw: object) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class Article:
    """A stored item joined with its source, ready for clustering and ranking.

    sqlite3.Row is awkward to pass around and carries no defaults, so rows are
    lifted into this once and everything downstream uses it.
    """

    id: str
    source_id: str
    source_name: str
    tier: str
    title: str
    summary: str | None
    url: str
    canonical_url: str
    published_at: datetime | None
    fetched_at: datetime
    image_url: str | None = None
    entities: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: Any) -> "Article":
        return cls(
            id=row["id"],
            source_id=row["source_id"],
            source_name=row["source_name"],
            tier=row["tier"],
            title=row["title"],
            summary=row["summary"],
            url=row["url"],
            canonical_url=row["canonical_url"],
            published_at=_as_datetime(row["published_at"]),
            fetched_at=_as_datetime(row["fetched_at"]) or datetime.now(timezone.utc),
            image_url=row["image_url"],
            entities=_as_list(row["entities"]),
            topics=_as_list(row["topics"]),
        )

    @property
    def effective_published(self) -> datetime:
        """Undated items still need an ordering key."""
        return self.published_at or self.fetched_at

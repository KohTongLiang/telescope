"""Read-only queries behind the web UI.

These statements live here rather than in ``store.py`` because their shapes —
faceted counts, paged filtering, search relevance, an "everything except this
dimension" count that keeps facet counts honest — exist only to serve the
viewer. Every statement is a ``SELECT``; the connection itself is opened with
``PRAGMA query_only``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Iterable
from urllib.parse import urlencode

from ..store import Store, iso, utcnow

DEFAULT_LIMIT = 40
FACET_LIMIT = 40

TOPICS = "topics"
TAGS = "tags"
SOURCES = "sources"
DIMENSIONS = (TOPICS, TAGS, SOURCES)
# The facet dimension names double as attribute names; the query parameters the
# routes declare are singular. Keeping the mapping in one place is what stops a
# chip link from silently dropping its own filter.
PARAM_NAMES = {TOPICS: "topic", TAGS: "tag", SOURCES: "source"}


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _json_list(raw: object) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        value = json.loads(str(raw))
    except ValueError:
        return ()
    return tuple(str(entry) for entry in value) if isinstance(value, list) else ()


def _json_any(column: str, count: int) -> str:
    """``EXISTS`` clause matching any of ``count`` values in a JSON array column."""
    placeholders = ", ".join("?" * count)
    return (
        f"EXISTS (SELECT 1 FROM json_each({column}) "
        f"WHERE json_each.value IN ({placeholders}))"
    )


@dataclass(frozen=True)
class ItemView:
    """A stored item, with its JSON columns already decoded.

    Templates get a real object rather than a ``sqlite3.Row``, so a missing
    column is an AttributeError in development instead of a silent blank cell.
    """

    id: str
    title: str
    summary: str | None
    url: str
    canonical_url: str
    source_id: str
    source_name: str
    tier: str
    kind: str
    author: str | None
    image_url: str | None
    published_at: str | None
    fetched_at: str
    guid: str
    topics: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ItemView":
        return cls(
            id=row["id"],
            title=row["title"],
            summary=row["summary"],
            url=row["url"],
            canonical_url=row["canonical_url"],
            source_id=row["source_id"],
            source_name=row["source_name"],
            tier=row["tier"],
            kind=row["kind"],
            author=row["author"],
            image_url=row["image_url"],
            published_at=row["published_at"],
            fetched_at=row["fetched_at"],
            guid=row["guid"],
            topics=_json_list(row["topics"]),
            categories=_json_list(row["categories"]),
            entities=_json_list(row["entities"]),
        )

    @property
    def when(self) -> str:
        """Undated items still need something to sort and show."""
        return self.published_at or self.fetched_at


@dataclass(frozen=True)
class DigestView:
    """A row from the ``digests`` table."""

    id: int
    generated_at: str
    window_start: str
    window_end: str
    path: str | None
    items_considered: int
    stories: int
    sources_ok: int
    sources_total: int
    stale_sources: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "DigestView":
        return cls(
            id=int(row["id"]),
            generated_at=row["generated_at"],
            window_start=row["window_start"],
            window_end=row["window_end"],
            path=row["path"],
            items_considered=int(row["items_considered"]),
            stories=int(row["stories"]),
            sources_ok=int(row["sources_ok"]),
            sources_total=int(row["sources_total"]),
            stale_sources=int(row["stale_sources"]),
        )

    @property
    def coverage(self) -> str:
        return f"{self.sources_ok}/{self.sources_total}"

    @property
    def day(self) -> str:
        return self.generated_at[:10]


@dataclass(frozen=True)
class ItemFilter:
    """Everything the items page can filter on, in one hashable shape."""

    q: str = ""
    topics: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    kind: str = ""
    days: int = 0
    offset: int = 0
    limit: int = DEFAULT_LIMIT

    @property
    def active(self) -> bool:
        return bool(
            self.q or self.topics or self.tags or self.sources or self.kind or self.days
        )

    @property
    def active_count(self) -> int:
        return sum(
            bool(value)
            for value in (self.q, self.topics, self.tags, self.sources, self.kind, self.days)
        )

    def at_offset(self, offset: int) -> "ItemFilter":
        return replace(self, offset=max(0, offset))

    def toggled(self, dimension: str, value: str) -> "ItemFilter":
        """Add or remove one facet value — what a facet link does."""
        current = list(getattr(self, dimension))
        if value in current:
            current.remove(value)
        else:
            current.append(value)
        return replace(self, **{dimension: tuple(current)}, offset=0)

    def cleared(self) -> "ItemFilter":
        return ItemFilter(limit=self.limit)

    def query_string(self, *, offset: int | None = None) -> str:
        """Rebuild the querystring, so every view stays a linkable URL."""
        pairs: list[tuple[str, str]] = []
        if self.q:
            pairs.append(("q", self.q))
        for dimension in DIMENSIONS:
            pairs.extend(
                (PARAM_NAMES[dimension], value) for value in getattr(self, dimension)
            )
        if self.kind:
            pairs.append(("kind", self.kind))
        if self.days:
            pairs.append(("days", str(self.days)))
        if offset:
            pairs.append(("offset", str(offset)))
        return urlencode(pairs)


class WebQueries:
    """Read-only view onto a :class:`~telescope.store.Store`."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.conn = store.conn

    # -- search mode -------------------------------------------------------

    def search_mode(self, query: str) -> str:
        """``fts``, ``like``, or ``none``.

        FTS5 raises on a malformed expression — an unbalanced quote typed into
        the search box, say — so the mode is probed once per request and the
        LIKE path takes over rather than a 500.
        """
        query = (query or "").strip()
        if not query:
            return "none"
        if not self.store.fts_enabled:
            return "like"
        try:
            self.conn.execute(
                "SELECT rowid FROM items_fts WHERE items_fts MATCH ? LIMIT 1", (query,)
            ).fetchone()
        except sqlite3.OperationalError:
            return "like"
        return "fts"

    def _from(self, mode: str) -> str:
        sql = "FROM items i JOIN sources s ON s.id = i.source_id"
        if mode == "fts":
            sql += " JOIN items_fts f ON f.rowid = i.rowid"
        return sql

    def _where(
        self, filters: ItemFilter, mode: str, *, skip: str = ""
    ) -> tuple[list[str], list[object]]:
        clauses: list[str] = []
        params: list[object] = []

        if mode == "fts":
            clauses.append("items_fts MATCH ?")
            params.append(filters.q)
        elif mode == "like":
            like = f"%{_escape_like(filters.q)}%"
            clauses.append("(i.title LIKE ? ESCAPE '\\' OR i.summary LIKE ? ESCAPE '\\')")
            params.extend([like, like])

        if filters.topics and skip != TOPICS:
            clauses.append(_json_any("i.topics", len(filters.topics)))
            params.extend(filters.topics)

        if filters.tags and skip != TAGS:
            clauses.append(_json_any("i.categories", len(filters.tags)))
            params.extend(filters.tags)

        if filters.sources and skip != SOURCES:
            clauses.append(f"i.source_id IN ({', '.join('?' * len(filters.sources))})")
            params.extend(filters.sources)

        if filters.kind:
            clauses.append("i.kind = ?")
            params.append(filters.kind)

        if filters.days > 0:
            clauses.append("COALESCE(i.published_at, i.fetched_at) >= ?")
            params.append(iso(utcnow() - timedelta(days=filters.days)))

        return clauses, params

    @staticmethod
    def _clause(clauses: Iterable[str]) -> str:
        joined = " AND ".join(clauses)
        return f" WHERE {joined}" if joined else ""

    # -- items -------------------------------------------------------------

    def items(self, filters: ItemFilter) -> tuple[list[ItemView], int]:
        """One page of matching items, plus the total for the pager."""
        mode = self.search_mode(filters.q)
        clauses, params = self._where(filters, mode)
        source = self._from(mode)
        where = self._clause(clauses)

        total = int(
            self.conn.execute(f"SELECT COUNT(*) {source}{where}", params).fetchone()[0]
        )

        # With a query, FTS rank (bm25, lower is better) is the useful order.
        # Without one, newest first.
        order = "f.rank" if mode == "fts" else "COALESCE(i.published_at, i.fetched_at) DESC"
        rows = self.conn.execute(
            f"SELECT i.*, s.name AS source_name, s.tier AS tier {source}{where} "
            f"ORDER BY {order}, i.title LIMIT ? OFFSET ?",
            [*params, filters.limit, filters.offset],
        )
        return [ItemView.from_row(row) for row in rows], total

    def item(self, item_id: str) -> ItemView | None:
        row = self.conn.execute(
            "SELECT i.*, s.name AS source_name, s.tier AS tier "
            "FROM items i JOIN sources s ON s.id = i.source_id WHERE i.id = ?",
            (item_id,),
        ).fetchone()
        return ItemView.from_row(row) if row else None

    def kinds(self) -> list[str]:
        return [
            str(row["kind"])
            for row in self.conn.execute("SELECT DISTINCT kind FROM items ORDER BY kind")
        ]

    # -- facets ------------------------------------------------------------

    def facets(self, filters: ItemFilter) -> dict[str, list[dict[str, Any]]]:
        """Counts per facet value.

        Each dimension is counted with every *other* filter applied but not its
        own, so a selected topic does not collapse the topic list to itself and
        the counts answer "what would I get if I added this?".
        """
        return {
            TOPICS: self._facet_json(filters, "i.topics", skip=TOPICS),
            TAGS: self._facet_json(filters, "i.categories", skip=TAGS),
            SOURCES: self._facet_sources(filters, skip=SOURCES),
        }

    def _facet_json(
        self, filters: ItemFilter, column: str, *, skip: str
    ) -> list[dict[str, Any]]:
        mode = self.search_mode(filters.q)
        clauses, params = self._where(filters, mode, skip=skip)
        where = self._clause(clauses)
        rows = self.conn.execute(
            f"SELECT j.value AS name, COUNT(*) AS n {self._from(mode)} "
            f"JOIN json_each({column}) j{where} "
            f"GROUP BY j.value ORDER BY n DESC, name LIMIT ?",
            [*params, FACET_LIMIT],
        )
        return [{"name": row["name"], "count": int(row["n"])} for row in rows]

    def _facet_sources(self, filters: ItemFilter, *, skip: str) -> list[dict[str, Any]]:
        mode = self.search_mode(filters.q)
        clauses, params = self._where(filters, mode, skip=skip)
        where = self._clause(clauses)
        rows = self.conn.execute(
            f"SELECT s.id AS id, s.name AS name, COUNT(*) AS n {self._from(mode)}"
            f"{where} GROUP BY s.id, s.name ORDER BY n DESC, s.name LIMIT ?",
            [*params, FACET_LIMIT],
        )
        return [
            {"id": row["id"], "name": row["name"], "count": int(row["n"])}
            for row in rows
        ]

    # -- digests -----------------------------------------------------------

    def digests(
        self, *, limit: int = 60, offset: int = 0
    ) -> tuple[list[DigestView], int]:
        total = int(self.conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0])
        rows = self.conn.execute(
            "SELECT * FROM digests ORDER BY generated_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [DigestView.from_row(row) for row in rows], total

    def digest(self, digest_id: int) -> DigestView | None:
        row = self.conn.execute(
            "SELECT * FROM digests WHERE id = ?", (digest_id,)
        ).fetchone()
        return DigestView.from_row(row) if row else None

    def latest_digest(self) -> DigestView | None:
        row = self.conn.execute(
            "SELECT * FROM digests ORDER BY generated_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return DigestView.from_row(row) if row else None

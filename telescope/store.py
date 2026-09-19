"""SQLite storage: schema, migrations, watermarks and the fetch log.

The database is the tool's memory. It lives outside Google Drive on purpose:
Drive syncing a SQLite file mid-write is a well-known route to corruption.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Source
from .models import Article, NormalizedItem

SCHEMA_V1 = """
CREATE TABLE sources (
    id                      TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    url                     TEXT NOT NULL,
    tier                    TEXT NOT NULL,
    axis                    TEXT NOT NULL,
    enabled                 INTEGER NOT NULL DEFAULT 1,
    max_age_hours           INTEGER NOT NULL DEFAULT 48,
    etag                    TEXT,
    last_modified           TEXT,
    last_http_status        INTEGER,
    last_fetch_at           TEXT,
    last_success_at         TEXT,
    last_item_age_hours     REAL,
    last_error              TEXT,
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    last_seen_guid          TEXT,
    last_seen_published_at  TEXT,
    updated_at              TEXT NOT NULL
);

CREATE TABLE items (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL DEFAULT 'news',
    guid            TEXT NOT NULL,
    title           TEXT NOT NULL,
    summary         TEXT,
    url             TEXT NOT NULL DEFAULT '',
    canonical_url   TEXT NOT NULL DEFAULT '',
    author          TEXT,
    published_at    TEXT,
    fetched_at      TEXT NOT NULL,
    image_url       TEXT,
    categories      TEXT NOT NULL DEFAULT '[]',
    raw             TEXT NOT NULL DEFAULT '{}',
    first_seen_at   TEXT NOT NULL,
    UNIQUE (source_id, guid)
);
CREATE INDEX idx_items_published ON items (published_at DESC);
CREATE INDEX idx_items_source ON items (source_id, published_at DESC);
CREATE INDEX idx_items_canonical ON items (canonical_url);

CREATE TABLE fetch_log (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id            TEXT NOT NULL,
    started_at           TEXT NOT NULL,
    finished_at          TEXT NOT NULL,
    duration_ms          INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL,
    http_status          INTEGER,
    items_found          INTEGER NOT NULL DEFAULT 0,
    items_new            INTEGER NOT NULL DEFAULT 0,
    items_unparsed_dates INTEGER NOT NULL DEFAULT 0,
    error                TEXT
);
CREATE INDEX idx_fetch_log_source ON fetch_log (source_id, started_at DESC);

CREATE TABLE run_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

FTS_V1 = """
CREATE VIRTUAL TABLE items_fts USING fts5(
    title,
    summary,
    content='items',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts (rowid, title, summary) VALUES (new.rowid, new.title, new.summary);
END;

CREATE TRIGGER items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts (items_fts, rowid, title, summary)
    VALUES ('delete', old.rowid, old.title, old.summary);
END;

CREATE TRIGGER items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts (items_fts, rowid, title, summary)
    VALUES ('delete', old.rowid, old.title, old.summary);
    INSERT INTO items_fts (rowid, title, summary) VALUES (new.rowid, new.title, new.summary);
END;
"""

SCHEMA_V2 = """
ALTER TABLE items ADD COLUMN entities TEXT NOT NULL DEFAULT '[]';
ALTER TABLE items ADD COLUMN topics TEXT NOT NULL DEFAULT '[]';

CREATE TABLE digests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at      TEXT NOT NULL,
    window_start      TEXT NOT NULL,
    window_end        TEXT NOT NULL,
    path              TEXT,
    items_considered  INTEGER NOT NULL DEFAULT 0,
    stories           INTEGER NOT NULL DEFAULT 0,
    sources_ok        INTEGER NOT NULL DEFAULT 0,
    sources_total     INTEGER NOT NULL DEFAULT 0,
    stale_sources     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_digests_generated ON digests (generated_at DESC);
"""

# Applied in order; PRAGMA user_version records how far we got, so an existing
# database upgrades in place rather than needing to be rebuilt.
MIGRATIONS: tuple[tuple[int, str], ...] = ((1, SCHEMA_V1), (2, SCHEMA_V2))

ITEM_COLUMNS = (
    "id, source_id, kind, guid, title, summary, url, canonical_url, author, "
    "published_at, fetched_at, image_url, categories, entities, topics, raw, "
    "first_seen_at"
)
ITEM_PLACEHOLDERS = ", ".join("?" * len(ITEM_COLUMNS.split(",")))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class SourceRow:
    """A source plus everything the last fetch learned about it."""

    id: str
    name: str
    url: str
    tier: str
    axis: str
    enabled: bool
    max_age_hours: int
    etag: str | None = None
    last_modified: str | None = None
    last_http_status: int | None = None
    last_fetch_at: str | None = None
    last_success_at: str | None = None
    last_item_age_hours: float | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    last_seen_guid: str | None = None
    last_seen_published_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SourceRow":
        return cls(
            id=row["id"],
            name=row["name"],
            url=row["url"],
            tier=row["tier"],
            axis=row["axis"],
            enabled=bool(row["enabled"]),
            max_age_hours=row["max_age_hours"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            last_http_status=row["last_http_status"],
            last_fetch_at=row["last_fetch_at"],
            last_success_at=row["last_success_at"],
            last_item_age_hours=row["last_item_age_hours"],
            last_error=row["last_error"],
            consecutive_failures=row["consecutive_failures"],
            last_seen_guid=row["last_seen_guid"],
            last_seen_published_at=row["last_seen_published_at"],
        )

    def stale(self, threshold_hours: float | None = None) -> bool:
        """True when the newest item is older than this source should ever be.

        This is the check that catches a feed returning HTTP 200 with valid XML
        while having quietly died months ago.
        """
        if self.last_item_age_hours is None:
            return False
        limit = self.max_age_hours if threshold_hours is None else threshold_hours
        return self.last_item_age_hours > limit


@dataclass
class FetchRecord:
    """Outcome of one fetch attempt, logged and folded into source state."""

    source_id: str
    started_at: datetime
    finished_at: datetime
    status: str
    success: bool = False
    http_status: int | None = None
    items_found: int = 0
    items_new: int = 0
    items_unparsed_dates: int = 0
    error: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    item_age_hours: float | None = None
    last_seen_guid: str | None = None
    last_seen_published_at: datetime | None = None

    @property
    def duration_ms(self) -> int:
        return max(0, int((self.finished_at - self.started_at).total_seconds() * 1000))


class Store:
    """Thin, explicit wrapper around SQLite."""

    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        self.path = Path(path)
        self.readonly = readonly

        if readonly:
            # The web viewer must never write: not a journal, not a WAL, not a
            # migration. `mode=ro` plus `query_only` makes that structural rather
            # than a matter of discipline.
            self.conn = self._connect_readonly()
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA query_only=ON")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.fts_enabled = self._has_fts()
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.fts_enabled = False
        self._configure()
        self._migrate()

    def _connect_readonly(self) -> sqlite3.Connection:
        """Open the database read-only, with a conservative fallback.

        Even a reader of a WAL database needs its ``-shm`` file, so the URI open
        can fail on an unusual filesystem. The fallback still refuses writes
        through ``PRAGMA query_only``.
        """
        try:
            return sqlite3.connect(f"{self.path.absolute().as_uri()}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            return sqlite3.connect(str(self.path))

    def _has_fts(self) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items_fts'"
            ).fetchone()
            is not None
        )

    # -- lifecycle ---------------------------------------------------------

    def _configure(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for target, script in MIGRATIONS:
            if version < target:
                with self.conn:
                    self.conn.executescript(script)
                version = target
                self.conn.execute(f"PRAGMA user_version={int(target)}")
        self._ensure_fts()

    def _ensure_fts(self) -> None:
        if self._has_fts():
            self.fts_enabled = True
            return
        try:
            with self.conn:
                self.conn.executescript(FTS_V1)
            self.fts_enabled = True
        except sqlite3.OperationalError:
            # FTS5 unavailable in this SQLite build: everything still works,
            # but `search` falls back to LIKE.
            self.fts_enabled = False

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- sources -----------------------------------------------------------

    def sync_sources(self, sources: list[Source]) -> int:
        """Upsert the registry, preserving all learned fetch state."""
        now = iso(utcnow())
        rows = [
            (
                s.id,
                s.name,
                s.url,
                s.tier,
                s.axis,
                int(s.enabled),
                s.max_age_hours,
                now,
            )
            for s in sources
        ]
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO sources (id, name, url, tier, axis, enabled, max_age_hours, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    url = excluded.url,
                    tier = excluded.tier,
                    axis = excluded.axis,
                    enabled = excluded.enabled,
                    max_age_hours = excluded.max_age_hours,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def list_sources(self, *, enabled_only: bool = False) -> list[SourceRow]:
        sql = "SELECT * FROM sources"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY tier, name"
        return [SourceRow.from_row(r) for r in self.conn.execute(sql)]

    def get_source(self, source_id: str) -> SourceRow | None:
        row = self.conn.execute(
            "SELECT * FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        return SourceRow.from_row(row) if row else None

    # -- items -------------------------------------------------------------

    def save_items(self, items: list[NormalizedItem]) -> tuple[int, int]:
        """Insert items, ignoring ones already stored. Returns (new, duplicate)."""
        if not items:
            return (0, 0)
        now = iso(utcnow())
        rows = [
            (
                item.id,
                item.source_id,
                item.kind,
                item.guid,
                item.title,
                item.summary,
                item.url,
                item.canonical_url,
                item.author,
                iso(item.published_at) if item.published_at else None,
                iso(item.fetched_at),
                item.image_url,
                json.dumps(item.categories, ensure_ascii=False),
                json.dumps(item.entities, ensure_ascii=False),
                json.dumps(item.topics, ensure_ascii=False),
                json.dumps(item.raw, ensure_ascii=False, default=str),
                now,
            )
            for item in items
        ]
        with self.conn:
            cursor = self.conn.executemany(
                f"INSERT OR IGNORE INTO items ({ITEM_COLUMNS}) "
                f"VALUES ({ITEM_PLACEHOLDERS})",
                rows,
            )
        # rowcount, never total_changes. The FTS sync triggers below write their
        # own rows, which inflates total_changes to roughly 5 changes per stored
        # item and would report a batch of 2 as 10 new items.
        inserted = max(0, cursor.rowcount)
        return (inserted, len(items) - inserted)

    def recent_items(self, *, limit: int = 20, source_id: str | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT i.*, s.name AS source_name FROM items i "
            "JOIN sources s ON s.id = i.source_id"
        )
        params: list[object] = []
        if source_id:
            sql += " WHERE i.source_id = ?"
            params.append(source_id)
        # Undated items sort last. They must not masquerade as the newest thing
        # simply because we happened to fetch them a moment ago.
        sql += (
            " ORDER BY (i.published_at IS NULL) ASC, "
            "COALESCE(i.published_at, i.fetched_at) DESC LIMIT ?"
        )
        params.append(limit)
        return list(self.conn.execute(sql, params))

    def search(self, query: str, *, limit: int = 20) -> list[sqlite3.Row]:
        """Full-text search, degrading to LIKE if FTS is unavailable or angry."""
        query = (query or "").strip()
        if not query:
            return []

        if self.fts_enabled:
            try:
                return list(
                    self.conn.execute(
                        """
                        SELECT i.*, s.name AS source_name
                        FROM items_fts f
                        JOIN items i ON i.rowid = f.rowid
                        JOIN sources s ON s.id = i.source_id
                        WHERE items_fts MATCH ?
                        ORDER BY f.rank
                        LIMIT ?
                        """,
                        (query, limit),
                    )
                )
            except sqlite3.OperationalError:
                pass  # malformed FTS expression: fall through to LIKE

        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        return list(
            self.conn.execute(
                """
                SELECT i.*, s.name AS source_name
                FROM items i
                JOIN sources s ON s.id = i.source_id
                WHERE i.title LIKE ? ESCAPE '\\' OR i.summary LIKE ? ESCAPE '\\'
                ORDER BY (i.published_at IS NULL) ASC,
                         COALESCE(i.published_at, i.fetched_at) DESC
                LIMIT ?
                """,
                (like, like, limit),
            )
        )

    def items_between(
        self, start: datetime, end: datetime, *, limit: int = 5000
    ) -> list[Article]:
        """Items published inside ``[start, end)``, newest first, with source."""
        rows = self.conn.execute(
            """
            SELECT i.*, s.name AS source_name, s.tier AS tier
            FROM items i
            JOIN sources s ON s.id = i.source_id
            WHERE COALESCE(i.published_at, i.fetched_at) >= ?
              AND COALESCE(i.published_at, i.fetched_at) < ?
            ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
            LIMIT ?
            """,
            (iso(start), iso(end), limit),
        )
        return [Article.from_row(row) for row in rows]

    def article_derived_rows(self, *, limit: int = 200000) -> list[sqlite3.Row]:
        """Identity plus derived fields, for recomputing entities and topics."""
        return list(
            self.conn.execute(
                "SELECT id, title, summary, entities, topics FROM items LIMIT ?",
                (limit,),
            )
        )

    def update_item_derived(
        self, item_id: str, entities: list[str], topics: list[str]
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE items SET entities = ?, topics = ? WHERE id = ?",
                (
                    json.dumps(entities, ensure_ascii=False),
                    json.dumps(topics, ensure_ascii=False),
                    item_id,
                ),
            )

    # -- digests -----------------------------------------------------------

    def record_digest(
        self,
        *,
        generated_at: datetime,
        window_start: datetime,
        window_end: datetime,
        path: str | None,
        items_considered: int,
        stories: int,
        sources_ok: int,
        sources_total: int,
        stale_sources: int,
    ) -> int:
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO digests (
                    generated_at, window_start, window_end, path, items_considered,
                    stories, sources_ok, sources_total, stale_sources
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    iso(generated_at),
                    iso(window_start),
                    iso(window_end),
                    path,
                    items_considered,
                    stories,
                    sources_ok,
                    sources_total,
                    stale_sources,
                ),
            )
        return int(cursor.lastrowid or 0)

    def recent_digests(self, *, limit: int = 5) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM digests ORDER BY generated_at DESC LIMIT ?", (limit,)
            )
        )

    # -- fetch log ---------------------------------------------------------

    def record_fetch(self, record: FetchRecord) -> None:
        """Log the attempt and fold the outcome into source state, atomically."""
        now = iso(utcnow())
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO fetch_log (
                    source_id, started_at, finished_at, duration_ms, status,
                    http_status, items_found, items_new, items_unparsed_dates, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.source_id,
                    iso(record.started_at),
                    iso(record.finished_at),
                    record.duration_ms,
                    record.status,
                    record.http_status,
                    record.items_found,
                    record.items_new,
                    record.items_unparsed_dates,
                    record.error,
                ),
            )

            if record.success:
                self.conn.execute(
                    """
                    UPDATE sources SET
                        etag = COALESCE(?, etag),
                        last_modified = COALESCE(?, last_modified),
                        last_http_status = ?,
                        last_fetch_at = ?,
                        last_success_at = ?,
                        last_item_age_hours = COALESCE(?, last_item_age_hours),
                        last_error = NULL,
                        consecutive_failures = 0,
                        last_seen_guid = COALESCE(?, last_seen_guid),
                        last_seen_published_at = COALESCE(?, last_seen_published_at),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        record.etag,
                        record.last_modified,
                        record.http_status,
                        iso(record.finished_at),
                        iso(record.finished_at),
                        record.item_age_hours,
                        record.last_seen_guid,
                        iso(record.last_seen_published_at)
                        if record.last_seen_published_at
                        else None,
                        now,
                        record.source_id,
                    ),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE sources SET
                        last_http_status = ?,
                        last_fetch_at = ?,
                        last_error = ?,
                        consecutive_failures = consecutive_failures + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        record.http_status,
                        iso(record.finished_at),
                        record.error,
                        now,
                        record.source_id,
                    ),
                )

    def source_fetch_history(self, source_id: str, *, limit: int = 5) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM fetch_log WHERE source_id = ? ORDER BY started_at DESC LIMIT ?",
                (source_id, limit),
            )
        )

    # -- run state ---------------------------------------------------------

    def set_state(self, key: str, value: str) -> None:
        now = iso(utcnow())
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO run_state (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM run_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    # -- reporting ---------------------------------------------------------

    def stats(self) -> dict[str, object]:
        def scalar(sql: str, params: tuple = ()) -> int:
            return int(self.conn.execute(sql, params).fetchone()[0])

        return {
            "sources": scalar("SELECT COUNT(*) FROM sources"),
            "sources_enabled": scalar("SELECT COUNT(*) FROM sources WHERE enabled = 1"),
            "items": scalar("SELECT COUNT(*) FROM items"),
            "items_without_date": scalar(
                "SELECT COUNT(*) FROM items WHERE published_at IS NULL"
            ),
            "fetch_attempts": scalar("SELECT COUNT(*) FROM fetch_log"),
            "digests": scalar("SELECT COUNT(*) FROM digests"),
            "fts_enabled": self.fts_enabled,
            "db_path": str(self.path),
        }

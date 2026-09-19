"""Storage: schema, dedupe, watermarks, fetch log, search."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from telescope.config import Source
from telescope.models import NormalizedItem
from telescope.normalize import make_id
from telescope.store import MIGRATIONS, SCHEMA_V1, FetchRecord, Store, iso

UTC = timezone.utc
NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)


def src(**overrides) -> Source:
    base = {
        "id": "gamesindustry",
        "name": "GamesIndustry.biz",
        "url": "https://www.gamesindustry.biz/feed",
        "tier": "trade",
        "axis": "business",
    }
    base.update(overrides)
    return Source(**base)


def item(guid="g1", *, title="Studio announces layoffs", published=NOW, source_id="gamesindustry"):
    return NormalizedItem(
        id=make_id(source_id, guid),
        source_id=source_id,
        guid=guid,
        title=title,
        url="https://example.com/a",
        canonical_url="https://example.com/a",
        published_at=published,
        fetched_at=NOW,
    )


def rec(source_id="gamesindustry", *, success=True, status="ok", **kw) -> FetchRecord:
    kw.setdefault("http_status", 200)
    return FetchRecord(
        source_id=source_id,
        started_at=NOW,
        finished_at=NOW + timedelta(milliseconds=250),
        status=status,
        success=success,
        **kw,
    )


def test_migration_creates_schema(settings):
    with Store(settings.db_path) as store:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == max(
            target for target, _ in MIGRATIONS
        )
        names = {
            row[0]
            for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"sources", "items", "fetch_log", "run_state", "digests"} <= names


def test_upgrade_from_v1_preserves_existing_items(tmp_path):
    """An existing v1 database must upgrade in place, not need rebuilding."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_V1)
    conn.execute("PRAGMA user_version=1")
    stamp = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO sources (id, name, url, tier, axis, enabled, max_age_hours, updated_at) "
        "VALUES ('s', 'S', 'https://x.test/feed', 'trade', 'business', 1, 48, ?)",
        (stamp,),
    )
    conn.execute(
        "INSERT INTO items (id, source_id, kind, guid, title, url, canonical_url, "
        "fetched_at, first_seen_at) VALUES ('i', 's', 'news', 'g', 'T', '', '', ?, ?)",
        (stamp, stamp),
    )
    conn.commit()
    conn.close()

    with Store(path) as store:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == max(
            target for target, _ in MIGRATIONS
        )
        assert store.stats()["items"] == 1
        row = store.conn.execute("SELECT entities, topics FROM items").fetchone()
        # New columns arrive with defaults; `reindex` backfills them.
        assert row["entities"] == "[]"
        assert row["topics"] == "[]"


def test_migration_is_idempotent(settings):
    first = Store(settings.db_path)
    first.sync_sources([src()])
    first.close()
    second = Store(settings.db_path)
    assert second.get_source("gamesindustry") is not None
    second.close()


def test_save_items_dedupes_on_guid(store):
    store.sync_sources([src()])
    assert store.save_items([item("a"), item("b")]) == (2, 0)
    assert store.save_items([item("a"), item("b")]) == (0, 2)


def test_duplicate_guid_within_one_batch(store):
    store.sync_sources([src()])
    assert store.save_items([item("dup"), item("dup")]) == (1, 1)


def test_registry_resync_preserves_learned_state(store):
    store.sync_sources([src()])
    store.record_fetch(
        rec(
            etag='"v1"',
            items_found=5,
            items_new=5,
            item_age_hours=1.5,
            last_seen_guid="g9",
            last_seen_published_at=NOW,
        )
    )
    assert store.get_source("gamesindustry").etag == '"v1"'

    store.sync_sources([src(name="GamesIndustry.biz (renamed)")])
    after = store.get_source("gamesindustry")
    assert after.name == "GamesIndustry.biz (renamed)"
    assert after.etag == '"v1"'
    assert after.last_seen_guid == "g9"
    assert after.last_item_age_hours == 1.5


def test_failure_increments_and_success_resets(store):
    store.sync_sources([src()])
    store.record_fetch(rec(success=False, status="http_error", http_status=503, error="HTTP 503"))
    assert store.get_source("gamesindustry").consecutive_failures == 1
    store.record_fetch(rec(success=False, status="http_error", http_status=503, error="HTTP 503"))
    assert store.get_source("gamesindustry").consecutive_failures == 2

    store.record_fetch(rec())
    row = store.get_source("gamesindustry")
    assert row.consecutive_failures == 0
    assert row.last_error is None
    assert row.last_success_at == iso(NOW + timedelta(milliseconds=250))


def test_304_keeps_previous_etag_and_age(store):
    store.sync_sources([src()])
    store.record_fetch(rec(etag='"v1"', item_age_hours=2.5))
    store.record_fetch(rec(status="not_modified", http_status=304, etag=None, item_age_hours=None))

    row = store.get_source("gamesindustry")
    assert row.etag == '"v1"'
    assert row.last_item_age_hours == 2.5
    assert row.consecutive_failures == 0


def test_fetch_log_is_written(store):
    store.sync_sources([src()])
    store.record_fetch(rec(items_found=10, items_new=3, items_unparsed_dates=1))
    log = store.source_fetch_history("gamesindustry")
    assert len(log) == 1
    assert log[0]["items_found"] == 10
    assert log[0]["items_new"] == 3
    assert log[0]["items_unparsed_dates"] == 1
    assert log[0]["duration_ms"] == 250
    assert log[0]["status"] == "ok"


def test_search_matches_title(store):
    store.sync_sources([src()])
    store.save_items(
        [item("a", title="Studio announces layoffs"), item("b", title="New release date")]
    )
    hits = store.search("layoffs")
    assert len(hits) == 1
    assert "layoffs" in hits[0]["title"].lower()
    assert hits[0]["source_name"] == "GamesIndustry.biz"


def test_search_malformed_expression_does_not_raise(store):
    store.sync_sources([src()])
    store.save_items([item("a")])
    assert isinstance(store.search('"unclosed quote'), list)


def test_search_like_fallback_when_fts_disabled(store):
    store.sync_sources([src()])
    store.save_items([item("a", title="Studio announces layoffs")])
    store.fts_enabled = False
    hits = store.search("layoffs")
    assert len(hits) == 1


def test_search_empty_query(store):
    assert store.search("") == []
    assert store.search("   ") == []


def test_state_roundtrip(store):
    assert store.get_state("missing") is None
    assert store.get_state("missing", "fallback") == "fallback"
    store.set_state("last_digest_at", iso(NOW))
    assert store.get_state("last_digest_at") == iso(NOW)
    store.set_state("last_digest_at", iso(NOW + timedelta(hours=1)))
    assert store.get_state("last_digest_at") == iso(NOW + timedelta(hours=1))


def test_staleness_detection(store):
    store.sync_sources([src(max_age_hours=24)])
    store.record_fetch(rec(item_age_hours=100.0))
    assert store.get_source("gamesindustry").stale() is True

    store.record_fetch(rec(item_age_hours=2.0))
    assert store.get_source("gamesindustry").stale() is False

    store.record_fetch(rec(item_age_hours=None))
    # COALESCE keeps the last known age rather than blanking it
    assert store.get_source("gamesindustry").last_item_age_hours == 2.0


def test_staleness_unknown_age_is_not_stale(store):
    store.sync_sources([src()])
    assert store.get_source("gamesindustry").stale() is False


def test_recent_items_orders_by_newest(store):
    store.sync_sources([src()])
    store.save_items(
        [
            item("old", published=NOW - timedelta(days=2)),
            item("new", published=NOW),
            item("undated", published=None),
        ]
    )
    rows = store.recent_items(limit=10)
    assert rows[0]["guid"] == "new"
    assert rows[-1]["guid"] == "undated"


def test_stats(store):
    store.sync_sources([src()])
    store.save_items([item("a"), item("b", published=None)])
    data = store.stats()
    assert data["sources"] == 1
    assert data["sources_enabled"] == 1
    assert data["items"] == 2
    assert data["items_without_date"] == 1
    assert data["fetch_attempts"] == 0


def test_disabled_source_listing(store):
    store.sync_sources([src(), src(id="other", name="Other", enabled=False)])
    assert len(store.list_sources()) == 2
    assert len(store.list_sources(enabled_only=True)) == 1

"""Digest orchestration: window semantics, watermark, and written output."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from telescope.config import ConfigError, Settings
from telescope.digest import (
    build_digest,
    digest_filename,
    resolve_window,
    run_digest,
    webhook_payload,
    write_digest,
)
from telescope.store import FetchRecord, iso

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def fail_every_source(store) -> None:
    for row in store.list_sources():
        store.record_fetch(
            FetchRecord(
                source_id=row.id,
                started_at=NOW,
                finished_at=NOW,
                status="http_error",
                success=False,
                http_status=503,
                error="HTTP 503",
            )
        )


def mark_healthy(store) -> None:
    row = store.list_sources()[0]
    store.record_fetch(
        FetchRecord(
            source_id=row.id,
            started_at=NOW,
            finished_at=NOW,
            status="ok",
            success=True,
            http_status=200,
            item_age_hours=1.0,
        )
    )


# -- window resolution -------------------------------------------------------


def test_window_falls_back_to_default_hours(settings, loaded_store):
    start, end = resolve_window(loaded_store, settings.load_digest_config(), now=NOW)
    assert end == NOW
    assert end - start == timedelta(hours=24)


def test_window_follows_the_stored_watermark(settings, loaded_store):
    loaded_store.set_state("last_digest_at", iso(NOW - timedelta(hours=6)))
    start, end = resolve_window(loaded_store, settings.load_digest_config(), now=NOW)
    assert start == NOW - timedelta(hours=6)
    assert end == NOW


def test_since_overrides_the_watermark(settings, loaded_store):
    loaded_store.set_state("last_digest_at", iso(NOW - timedelta(hours=6)))
    start, _ = resolve_window(
        loaded_store,
        settings.load_digest_config(),
        now=NOW,
        since=NOW - timedelta(hours=2),
    )
    assert start == NOW - timedelta(hours=2)


def test_future_watermark_cannot_invert_the_window(settings, loaded_store):
    # A clock change must not produce a window that starts after it ends.
    loaded_store.set_state("last_digest_at", iso(NOW + timedelta(hours=5)))
    start, end = resolve_window(loaded_store, settings.load_digest_config(), now=NOW)
    assert start < end
    assert end - start == timedelta(hours=24)


# -- building vs writing -----------------------------------------------------


def test_build_digest_touches_neither_disk_nor_watermark(
    settings, loaded_store, item_factory
):
    loaded_store.save_items([item_factory("Sony acquires Housemarque")])
    result = build_digest(settings, loaded_store, now=NOW, window_hours=24)

    assert result.path is None
    assert loaded_store.get_state("last_digest_at") is None
    assert settings.digests_dir is not None
    assert not settings.digests_dir.exists()


def test_run_digest_writes_daily_and_latest_files(settings, loaded_store, item_factory):
    loaded_store.save_items([item_factory("Sony acquires Housemarque")])
    mark_healthy(loaded_store)

    result = run_digest(settings, loaded_store, now=NOW, window_hours=24)

    assert result.path is not None
    assert result.path.exists()
    assert result.path.name == "2026-09-15.md"
    latest = result.path.parent / "latest.md"
    assert latest.exists()
    assert latest.read_text(encoding="utf-8") == result.path.read_text(encoding="utf-8")


def test_digest_filename_uses_the_configured_timezone(settings, loaded_store):
    result = build_digest(settings, loaded_store, now=NOW, window_hours=24)
    # 12:00 UTC is 20:00 in Asia/Singapore, so the reader's date is the same day.
    assert digest_filename(result) == "2026-09-15.md"


def test_run_digest_records_history(settings, loaded_store, item_factory):
    loaded_store.save_items([item_factory("Sony acquires Housemarque")])
    mark_healthy(loaded_store)
    run_digest(settings, loaded_store, now=NOW, window_hours=24)

    rows = loaded_store.recent_digests()
    assert len(rows) == 1
    assert rows[0]["stories"] >= 1
    assert rows[0]["items_considered"] >= 1
    assert rows[0]["path"].endswith("2026-09-15.md")


def test_write_digest_creates_nested_directories(
    settings, loaded_store, item_factory, tmp_path
):
    loaded_store.save_items([item_factory("Sony acquires Housemarque")])
    result = build_digest(settings, loaded_store, now=NOW, window_hours=24)

    target = tmp_path / "nested" / "deeper"
    path = write_digest(result, target, also_latest=False)

    assert path.exists()
    assert not (target / "latest.md").exists()


def test_missing_output_directory_raises(tmp_path, monkeypatch, store):
    monkeypatch.delenv("TELESCOPE_DIGEST_DIR", raising=False)
    empty_config = tmp_path / "empty"
    empty_config.mkdir()
    settings = Settings(
        project_root=tmp_path, data_root=tmp_path, config_dir=empty_config
    )
    with pytest.raises(ConfigError, match="output directory"):
        run_digest(settings, store, now=NOW, window_hours=24)


# -- watermark discipline ----------------------------------------------------


def test_watermark_advances_when_a_source_is_healthy(
    settings, loaded_store, item_factory
):
    loaded_store.save_items([item_factory("Sony acquires Housemarque")])
    mark_healthy(loaded_store)

    result = run_digest(settings, loaded_store, now=NOW, window_hours=24)

    assert result.marked is True
    assert loaded_store.get_state("last_digest_at") == iso(NOW)
    assert loaded_store.get_state("last_digest_path") is not None


def test_watermark_does_not_advance_when_every_source_failed(
    settings, loaded_store, item_factory
):
    loaded_store.save_items([item_factory("Sony acquires Housemarque")])
    fail_every_source(loaded_store)

    result = run_digest(settings, loaded_store, now=NOW, window_hours=24)

    assert result.health.everything_failed
    assert result.marked is False
    # Advancing here would skip this window forever and lose whatever news lands
    # in it once the feeds recover.
    assert loaded_store.get_state("last_digest_at") is None
    # ...but the failure is still written out, because silence is worse.
    assert result.path is not None and result.path.exists()


def test_no_mark_leaves_the_watermark_alone(settings, loaded_store, item_factory):
    loaded_store.save_items([item_factory("Sony acquires Housemarque")])
    mark_healthy(loaded_store)

    result = run_digest(settings, loaded_store, now=NOW, window_hours=24, mark=False)

    assert result.marked is False
    assert loaded_store.get_state("last_digest_at") is None


# -- selection and rendering -------------------------------------------------


def test_duplicate_stories_collapse_in_the_digest(settings, loaded_store, item_factory):
    loaded_store.save_items(
        [
            item_factory("Sony acquires Housemarque", source_id="eurogamer"),
            item_factory("Sony acquires Housemarque", source_id="ign"),
        ]
    )
    result = build_digest(settings, loaded_store, now=NOW, window_hours=24)
    assert len(result.stories) == 1
    assert result.stories[0].breadth == 2


def test_window_excludes_older_items(settings, loaded_store, item_factory):
    loaded_store.save_items(
        [
            item_factory("Recent story here", published=NOW - timedelta(hours=2)),
            item_factory("Ancient story here", published=NOW - timedelta(days=5)),
        ]
    )
    titles = [story.title for story in build_digest(
        settings, loaded_store, now=NOW, window_hours=24
    ).stories]
    assert "Recent story here" in titles
    assert "Ancient story here" not in titles


def test_empty_window_renders_gracefully(settings, loaded_store):
    mark_healthy(loaded_store)
    result = run_digest(settings, loaded_store, now=NOW, window_hours=24)
    assert result.stories == []
    assert "## Nothing new" in result.markdown


def test_markdown_contains_the_ranked_story(settings, loaded_store, item_factory):
    loaded_store.save_items([item_factory("Sony acquires Housemarque")])
    result = build_digest(settings, loaded_store, now=NOW, window_hours=24)
    assert "Sony acquires Housemarque" in result.markdown
    assert "## Everything else" in result.markdown


# -- webhook wiring ----------------------------------------------------------


def _configure_webhook(settings, monkeypatch, url="https://hooks.test/telescope"):
    config = settings.load_digest_config()
    config.webhook_url = url
    monkeypatch.setattr(settings, "load_digest_config", lambda: config)
    return config


def test_webhook_is_posted_when_configured(
    settings, loaded_store, item_factory, make_client, monkeypatch
):
    _configure_webhook(settings, monkeypatch)
    loaded_store.save_items([item_factory("Sony acquires Housemarque")])
    mark_healthy(loaded_store)

    posted: dict = {}

    def handler(request):
        posted["body"] = request.content
        return httpx.Response(200)

    with make_client(handler) as client:
        result = run_digest(settings, loaded_store, now=NOW, window_hours=24, client=client)

    assert result.webhook_ok is True
    assert b"top_stories" in posted["body"]
    assert b"Housemarque" in posted["body"]


def test_webhook_failure_does_not_fail_the_digest(
    settings, loaded_store, item_factory, make_client, monkeypatch
):
    _configure_webhook(settings, monkeypatch)
    loaded_store.save_items([item_factory("Sony acquires Housemarque")])
    mark_healthy(loaded_store)

    def handler(request):
        return httpx.Response(500)

    with make_client(handler) as client:
        result = run_digest(settings, loaded_store, now=NOW, window_hours=24, client=client)

    assert result.webhook_ok is False
    assert result.webhook_error == "HTTP 500"
    # The digest is already on disk, so a failed ping must not look like a
    # failed run.
    assert result.path is not None and result.path.exists()
    assert result.marked is True


def test_no_webhook_configured_means_no_request(
    settings, loaded_store, make_client
):
    requested: list = []

    def handler(request):
        requested.append(request)
        return httpx.Response(200)

    mark_healthy(loaded_store)
    with make_client(handler) as client:
        result = run_digest(settings, loaded_store, now=NOW, window_hours=24, client=client)

    assert result.webhook_ok is None
    assert requested == []


def test_webhook_payload_has_the_expected_shape(settings, loaded_store, item_factory):
    loaded_store.save_items([item_factory("Sony acquires Housemarque")])
    result = build_digest(settings, loaded_store, now=NOW, window_hours=24)
    payload = webhook_payload(result)

    assert payload["date"] == "2026-09-15"
    assert payload["volume"]["items"] >= 1
    assert payload["sources"]["total"] >= 1
    assert payload["top_stories"][0]["title"] == "Sony acquires Housemarque"
    assert payload["window"]["hours"] == 24.0

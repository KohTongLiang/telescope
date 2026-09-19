"""Sync orchestration: end-to-end ingestion and failure isolation."""

from __future__ import annotations

import httpx
import pytest

from telescope.sync import run_sync

# The three sources with captured fixtures. The full registry is deliberately
# larger, so these tests name the sources they exercise instead of depending on
# how many feeds happen to be configured.
FIXTURED = ["gamesindustry", "eurogamer", "gematsu"]


def test_sync_ingests_every_selected_source(settings, store, fixture_handler, make_client, now):
    with make_client(fixture_handler) as client:
        report = run_sync(
            settings, store, only=FIXTURED, client=client, now=now, sleep=lambda _: None
        )

    assert len(report.results) == len(FIXTURED)
    assert report.ok_count == len(FIXTURED)
    assert not report.all_failed
    assert report.total_found > 0
    assert report.total_new == report.total_found
    assert all(r.status == "ok" for r in report.results)
    assert all(r.item_age_hours is not None for r in report.results)

    assert store.stats()["items"] == report.total_new


def test_watermarks_and_validators_are_recorded(settings, store, fixture_handler, make_client, now):
    with make_client(fixture_handler) as client:
        run_sync(
            settings, store, only=FIXTURED, client=client, now=now, sleep=lambda _: None
        )

    for source in store.list_sources():
        if source.id not in FIXTURED:
            continue
        assert source.etag is not None, source.id
        assert source.last_success_at is not None, source.id
        assert source.last_seen_guid is not None, source.id
        assert source.last_seen_published_at is not None, source.id
        assert source.consecutive_failures == 0
        assert source.last_error is None


def test_second_sync_adds_nothing(settings, store, fixture_handler, make_client, now):
    with make_client(fixture_handler) as client:
        first = run_sync(
            settings, store, only=FIXTURED, client=client, now=now, sleep=lambda _: None
        )
        second = run_sync(
            settings, store, only=FIXTURED, client=client, now=now, sleep=lambda _: None
        )

    assert first.total_new > 0
    assert second.total_new == 0
    assert second.ok_count == len(FIXTURED)
    assert store.stats()["items"] == first.total_new


def test_304_is_treated_as_healthy(settings, store, make_client, now):
    def handler(request):
        return httpx.Response(304)

    with make_client(handler) as client:
        report = run_sync(
            settings, store, only=FIXTURED, client=client, now=now, sleep=lambda _: None
        )

    assert report.ok_count == len(FIXTURED)
    assert all(r.status == "not_modified" for r in report.results)
    assert store.get_source("eurogamer").last_success_at is not None


def test_one_broken_source_does_not_stop_the_rest(
    settings, store, load_fixture, feed_fixtures, make_client, now
):
    def handler(request):
        if request.url.host == "www.gematsu.com":
            return httpx.Response(503)
        return httpx.Response(200, content=load_fixture(feed_fixtures[request.url.host]))

    with make_client(handler) as client:
        report = run_sync(
            settings, store, only=FIXTURED, client=client, now=now, sleep=lambda _: None
        )

    assert report.ok_count == 2
    assert not report.all_failed
    assert [r.source_id for r in report.failed] == ["gematsu"]
    assert report.failed[0].http_status == 503
    assert store.get_source("gematsu").consecutive_failures == 1
    assert store.get_source("gematsu").last_error is not None
    # the healthy sources still stored their items
    assert store.stats()["items"] > 0


def test_html_response_is_a_parse_error_not_a_silent_empty(
    settings, store, load_fixture, make_client, now
):
    def handler(request):
        return httpx.Response(200, content=load_fixture("not_a_feed.html"))

    with make_client(handler) as client:
        report = run_sync(settings, store, client=client, now=now, sleep=lambda _: None)

    assert report.all_failed
    assert all(r.status == "parse_error" for r in report.results)
    assert store.stats()["items"] == 0


def test_all_sources_failing_is_loud(settings, store, make_client, now):
    def handler(request):
        raise httpx.ConnectError("network down")

    with make_client(handler) as client:
        report = run_sync(settings, store, client=client, now=now, sleep=lambda _: None)

    assert report.all_failed
    assert report.ok_count == 0
    assert all(r.status == "network_error" for r in report.results)


def test_only_restricts_selection(settings, store, fixture_handler, make_client, now):
    with make_client(fixture_handler) as client:
        report = run_sync(
            settings, store, only=["gematsu"], client=client, now=now, sleep=lambda _: None
        )

    assert [r.source_id for r in report.results] == ["gematsu"]
    assert store.stats()["items"] > 0


def test_unknown_source_id_raises(settings, store):
    with pytest.raises(ValueError, match="unknown source id"):
        run_sync(settings, store, only=["not_a_source"])


def test_last_sync_at_is_recorded(settings, store, fixture_handler, make_client, now):
    with make_client(fixture_handler) as client:
        run_sync(settings, store, client=client, now=now, sleep=lambda _: None)
    assert store.get_state("last_sync_at") is not None


def test_sync_report_duration_is_measured(settings, store, fixture_handler, make_client, now):
    with make_client(fixture_handler) as client:
        report = run_sync(settings, store, client=client, now=now, sleep=lambda _: None)
    assert report.duration_ms >= 0
    assert report.finished_at >= report.started_at

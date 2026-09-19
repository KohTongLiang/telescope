"""Fetching: conditional GET, retries, and error classification."""

from __future__ import annotations

import httpx

from telescope.fetch import fetch_feed


def test_200_returns_body_and_validators(make_client):
    def handler(request):
        return httpx.Response(
            200, content=b"<rss/>", headers={"ETag": '"v1"', "Last-Modified": "Mon, 14 Sep 2026 09:30:00 GMT"}
        )

    with make_client(handler) as client:
        result = fetch_feed("https://x.test/feed", client=client)

    assert result.status == "ok"
    assert result.ok
    assert result.body == b"<rss/>"
    assert result.etag == '"v1"'
    assert result.last_modified == "Mon, 14 Sep 2026 09:30:00 GMT"
    assert result.attempts == 1


def test_conditional_headers_are_sent(make_client):
    seen: dict[str, str] = {}

    def handler(request):
        seen.update(dict(request.headers))
        return httpx.Response(304)

    with make_client(handler) as client:
        result = fetch_feed(
            "https://x.test/feed",
            client=client,
            etag='"v1"',
            last_modified="Mon, 14 Sep 2026 09:30:00 GMT",
        )

    assert result.status == "not_modified"
    assert result.usable
    assert seen["if-none-match"] == '"v1"'
    assert seen["if-modified-since"] == "Mon, 14 Sep 2026 09:30:00 GMT"


def test_retries_server_error_then_succeeds(make_client):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, content=b"ok")

    with make_client(handler) as client:
        result = fetch_feed("https://x.test/feed", client=client, sleep=lambda _: None)

    assert result.status == "ok"
    assert result.attempts == 2


def test_gives_up_after_max_attempts(make_client):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    with make_client(handler) as client:
        result = fetch_feed(
            "https://x.test/feed", client=client, max_attempts=3, sleep=lambda _: None
        )

    assert result.status == "http_error"
    assert result.http_status == 503
    assert calls["n"] == 3
    assert result.attempts == 3


def test_404_is_http_error(make_client):
    def handler(request):
        return httpx.Response(404)

    with make_client(handler) as client:
        result = fetch_feed("https://x.test/feed", client=client, sleep=lambda _: None)

    assert result.status == "http_error"
    assert result.http_status == 404
    assert result.usable is False


def test_403_is_not_retried(make_client):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(403)

    with make_client(handler) as client:
        result = fetch_feed("https://x.test/feed", client=client, sleep=lambda _: None)

    # Cloudflare does not reward persistence.
    assert result.status == "http_error"
    assert calls["n"] == 1


def test_network_error_is_reported_not_raised(make_client):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with make_client(handler) as client:
        result = fetch_feed("https://x.test/feed", client=client, sleep=lambda _: None)

    assert result.status == "network_error"
    assert result.body is None
    assert "connection refused" in (result.error or "")


def test_retry_after_header_is_honoured(make_client):
    slept: list[float] = []
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, content=b"ok")

    with make_client(handler) as client:
        result = fetch_feed(
            "https://x.test/feed", client=client, sleep=slept.append
        )

    assert result.status == "ok"
    assert slept == [2.0]


def test_absurd_retry_after_falls_back_to_backoff(make_client):
    slept: list[float] = []
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3600"})
        return httpx.Response(200, content=b"ok")

    with make_client(handler) as client:
        result = fetch_feed(
            "https://x.test/feed", client=client, sleep=slept.append, backoff=0.5
        )

    assert result.status == "ok"
    assert slept == [0.5]

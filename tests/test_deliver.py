"""Webhook delivery: transport behaviour and digest wiring."""

from __future__ import annotations

import httpx

from telescope.deliver import post_webhook


def test_successful_post(make_client):
    seen: dict = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    with make_client(handler) as client:
        ok, error = post_webhook("https://hooks.test/t", {"a": 1}, client=client)

    assert ok is True
    assert error is None
    assert seen["url"] == "https://hooks.test/t"
    assert b'"a"' in seen["body"]


def test_http_error_is_reported_not_raised(make_client):
    def handler(request):
        return httpx.Response(500)

    with make_client(handler) as client:
        ok, error = post_webhook("https://hooks.test/t", {}, client=client)

    assert ok is False
    assert error == "HTTP 500"


def test_network_error_is_reported_not_raised(make_client):
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with make_client(handler) as client:
        ok, error = post_webhook("https://hooks.test/t", {}, client=client)

    assert ok is False
    assert "no route to host" in (error or "")


def test_client_is_closed_when_created_internally(monkeypatch):
    # Passing no client must not leak one.
    created: list[httpx.Client] = []
    real_client = httpx.Client

    def spy(*args, **kwargs):
        client = real_client(transport=httpx.MockTransport(lambda r: httpx.Response(200)), **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", spy)
    ok, _ = post_webhook("https://hooks.test/t", {})
    assert ok is True
    assert created and created[0].is_closed

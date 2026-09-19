"""HTTP fetching: conditional GET, retries and backoff.

Uses httpx rather than stdlib urllib. This machine's Python 3.13 framework build
has no CA bundle, so every urllib HTTPS request fails certificate verification;
httpx ships certifi and just works.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

DEFAULT_USER_AGENT = "Telescope/0.1 (personal games-industry digest)"

ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
    "text/xml;q=0.8, */*;q=0.7"
)

# 403 is deliberately absent: Cloudflare does not reward persistence, and
# hammering a blocked host is both rude and useless.
RETRY_STATUS = {408, 429, 500, 502, 503, 504}

MAX_RETRY_AFTER_SECONDS = 30.0


@dataclass
class FetchResult:
    """Outcome of fetching one feed."""

    status: str  # ok | not_modified | http_error | network_error
    http_status: int | None = None
    body: bytes | None = None
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def usable(self) -> bool:
        """A 304 still counts as a healthy source."""
        return self.status in ("ok", "not_modified")


def build_client(
    *, user_agent: str = DEFAULT_USER_AGENT, timeout: float = 20.0
) -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": user_agent,
            "Accept": ACCEPT,
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=timeout,
        follow_redirects=True,
    )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    if 0 <= value <= MAX_RETRY_AFTER_SECONDS:
        return value
    return None


def fetch_feed(
    url: str,
    *,
    client: httpx.Client | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout: float = 20.0,
    max_attempts: int = 3,
    backoff: float = 0.7,
    sleep=time.sleep,
) -> FetchResult:
    """Fetch a feed, sending conditional headers when state exists.

    Retries transient failures with exponential backoff. Never raises for
    network problems: the caller gets a status and decides.
    """
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    owns_client = client is None
    if client is None:
        client = build_client(timeout=timeout)

    attempts = 0
    last_error: str | None = None
    last_status: int | None = None

    try:
        while attempts < max(1, max_attempts):
            attempts += 1
            try:
                response = client.get(url, headers=headers, timeout=timeout)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                last_status = None
                if attempts < max_attempts:
                    sleep(backoff * (2 ** (attempts - 1)))
                    continue
                return FetchResult(
                    status="network_error",
                    error=last_error,
                    attempts=attempts,
                )

            last_status = response.status_code

            if response.status_code == 304:
                return FetchResult(
                    status="not_modified",
                    http_status=304,
                    etag=response.headers.get("etag") or etag,
                    last_modified=response.headers.get("last-modified") or last_modified,
                    attempts=attempts,
                )

            if response.status_code == 200:
                return FetchResult(
                    status="ok",
                    http_status=200,
                    body=response.content,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                    attempts=attempts,
                )

            if response.status_code in RETRY_STATUS and attempts < max_attempts:
                delay = _retry_after_seconds(response)
                sleep(delay if delay is not None else backoff * (2 ** (attempts - 1)))
                continue

            return FetchResult(
                status="http_error",
                http_status=response.status_code,
                error=f"HTTP {response.status_code}",
                attempts=attempts,
            )

        return FetchResult(
            status="http_error",
            http_status=last_status,
            error=last_error or f"HTTP {last_status}",
            attempts=attempts,
        )
    finally:
        if owns_client:
            client.close()

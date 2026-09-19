"""Optional webhook notification.

Off unless ``webhook_url`` is set in ``config/digest.yaml``. This is transport
only — the payload is assembled by the digest module.

A webhook failure is reported but never fatal. The digest is already written to
disk by then, and losing a notification is not a reason to make a successful run
look like a failed one.
"""

from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = 10.0


def post_webhook(
    url: str,
    payload: dict,
    *,
    client: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bool, str | None]:
    """POST the payload. Returns ``(ok, error)`` and never raises."""
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        response = client.post(url, json=payload, timeout=timeout)
        if response.status_code >= 400:
            return False, f"HTTP {response.status_code}"
        return True, None
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if owns_client:
            client.close()

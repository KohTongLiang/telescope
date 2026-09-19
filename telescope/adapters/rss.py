"""RSS/Atom adapter.

One generic adapter covers every feed in the registry, so adding a source is a
YAML entry rather than a new class.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import feedparser


class FeedParseError(RuntimeError):
    """Raised when a body is not usable as a feed at all."""


@dataclass
class ParsedFeed:
    """A parsed feed plus whatever feedparser complained about."""

    entries: list = field(default_factory=list)
    title: str | None = None
    version: str | None = None
    bozo: bool = False
    bozo_exception: str | None = None

    @property
    def warning(self) -> str | None:
        """Non-fatal parse trouble, worth logging but not worth failing on."""
        return self.bozo_exception if self.bozo else None


def is_usable_entry(entry: dict) -> bool:
    """An entry with no identity and no title cannot become an item.

    feedparser emits phantom entries when XML is truncated mid-document: an
    entry object with ``title=None`` and ``id=None``. Counting those as a
    successful parse would let a broken feed report itself as healthy.
    """
    return bool(
        entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title")
    )


def parse_feed(body: bytes) -> ParsedFeed:
    """Parse feed bytes.

    feedparser is lenient by design, which is why it is used here. The rule is:
    fail only when there is genuinely nothing to work with. A feed that trips
    the bozo flag but still yields usable entries is treated as usable, because
    that is extremely common in the wild and discarding those entries would lose
    real stories.
    """
    if not body:
        raise FeedParseError("empty response body")

    parsed = feedparser.parse(body)
    feed = parsed.feed or {}
    title = feed.get("title")
    entries = [e for e in (parsed.entries or []) if is_usable_entry(e)]

    bozo = bool(parsed.get("bozo"))
    bozo_exception = None
    if bozo:
        raw = parsed.get("bozo_exception")
        bozo_exception = f"{type(raw).__name__}: {raw}" if raw else "unknown parse error"

    if not entries:
        if bozo:
            raise FeedParseError(
                f"no usable entries and feed is malformed: {bozo_exception}"
            )
        raise FeedParseError("feed parsed but contains no usable entries")

    return ParsedFeed(
        entries=entries,
        title=title,
        version=parsed.get("version"),
        bozo=bozo,
        bozo_exception=bozo_exception,
    )

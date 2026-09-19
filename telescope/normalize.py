"""Normalization: turn feed entries into the canonical item schema.

This module is where ingestion quality is won or lost. Feed data is genuinely
messy — missing dates, relative URLs, tracking parameters, HTML in summaries,
and entries identified only by title — so every function here is defensive and
has a defined fallback.
"""

from __future__ import annotations

import calendar
import email.utils
import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import Source
from .models import NormalizedItem
from .taxonomy import classify_topics, entity_names, extract_entities

# Query parameters that identify a campaign, not a document. Stripping them is
# what lets the same story from two sources collapse to one canonical URL in a
# later phase.
TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "cmpid",
    "ref",
    "ref_src",
    "source",
    "campaign",
    "at_medium",
    "at_campaign",
}

MAX_SUMMARY_CHARS = 1200

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)


@dataclass
class NormalizeStats:
    """What happened while normalizing one feed's entries."""

    total: int = 0
    skipped: int = 0
    missing_dates: int = 0


def make_id(source_id: str, guid: str) -> str:
    """Stable primary key for an item."""
    digest = hashlib.sha256(f"{source_id}\x00{guid}".encode("utf-8"))
    return digest.hexdigest()[:32]


def canonicalize_url(url: str) -> str:
    """Strip tracking noise so the same article has one stable form."""
    if not url:
        return ""
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.netloc:
        return url

    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    query.sort()

    return urlunsplit((scheme, netloc, path, urlencode(query, doseq=True), ""))


def strip_html(text: str | None) -> str | None:
    """Reduce feed HTML to readable plain text."""
    if not text:
        return None
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = html.unescape(cleaned)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    return cleaned or None


def _struct_to_datetime(value: object) -> datetime | None:
    """feedparser hands back UTC ``time.struct_time`` tuples."""
    try:
        return datetime.fromtimestamp(calendar.timegm(value[:6]), tz=timezone.utc)  # type: ignore[index]
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_date_string(raw: str) -> datetime | None:
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        parsed = None
    if parsed is not None:
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_entry_date(entry: dict) -> datetime | None:
    """Best-effort publication date, always timezone-aware UTC."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            parsed = _struct_to_datetime(value)
            if parsed is not None:
                return parsed

    for key in ("published", "updated", "created", "dc_date", "date"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            parsed = _parse_date_string(value.strip())
            if parsed is not None:
                return parsed
    return None


def entry_guid(entry: dict) -> str | None:
    """Identity for an entry, falling back to title+date when no GUID exists."""
    for key in ("id", "guid", "link"):
        value = entry.get(key)
        if value and str(value).strip():
            return str(value).strip()

    title = strip_html(entry.get("title"))
    if not title:
        return None
    published = entry.get("published") or entry.get("updated") or ""
    return make_id("fallback", f"{title}|{published}")


def _entry_body(entry: dict) -> str | None:
    summary = entry.get("summary")
    if summary:
        return summary
    content = entry.get("content") or []
    if content:
        first = content[0] or {}
        value = first.get("value")
        if value:
            return value
    return None


def extract_image(entry: dict) -> str | None:
    """First usable image URL: media RSS, then enclosures, then inline HTML."""
    for thumb in entry.get("media_thumbnail") or []:
        url = (thumb or {}).get("url")
        if url:
            return str(url)

    for media in entry.get("media_content") or []:
        media = media or {}
        url = media.get("url")
        if not url:
            continue
        media_type = str(media.get("type") or "")
        if not media_type or media_type.startswith("image"):
            return str(url)

    for enclosure in entry.get("enclosures") or []:
        enclosure = enclosure or {}
        if str(enclosure.get("type") or "").startswith("image"):
            url = enclosure.get("href") or enclosure.get("url")
            if url:
                return str(url)

    for blob in (_entry_body(entry),):
        if blob:
            match = _IMG_RE.search(blob)
            if match:
                return match.group(1)
    return None


def extract_categories(entry: dict) -> list[str]:
    categories: list[str] = []
    for tag in entry.get("tags") or []:
        term = (tag or {}).get("term")
        if term and term not in categories:
            categories.append(str(term))
    return categories[:20]


def trimmed_raw(entry: dict, feed_title: str | None) -> dict:
    """A small provenance record — not the article body."""
    return {
        "guid": entry.get("id") or entry.get("guid"),
        "link": entry.get("link"),
        "published": entry.get("published"),
        "updated": entry.get("updated"),
        "author": entry.get("author"),
        "tags": [t.get("term") for t in (entry.get("tags") or []) if t.get("term")],
        "feed_title": feed_title,
    }


def normalize_entry(
    source: Source,
    entry: dict,
    *,
    fetched_at: datetime,
    feed_title: str | None = None,
) -> NormalizedItem | None:
    """Map one feed entry, or return ``None`` if it has no usable identity."""
    guid = entry_guid(entry)
    if not guid:
        return None

    link = str(entry.get("link") or "").strip()
    if not link and guid.startswith("http"):
        link = guid

    title = strip_publisher_suffix(strip_html(entry.get("title")) or "(untitled)", entry)

    summary = strip_html(_entry_body(entry))
    if summary and len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS].rstrip() + "…"

    entities, topics = derive_fields(title, summary)

    return NormalizedItem(
        id=make_id(source.id, guid),
        source_id=source.id,
        guid=guid,
        title=title,
        summary=summary,
        url=link,
        canonical_url=canonicalize_url(link) or link,
        author=strip_html(entry.get("author")),
        published_at=parse_entry_date(entry),
        fetched_at=fetched_at,
        image_url=extract_image(entry),
        categories=extract_categories(entry),
        entities=entities,
        topics=topics,
        raw=trimmed_raw(entry, feed_title),
    )


_PUBLISHER_SUFFIX_SEPARATORS = (" - ", " \u2013 ", " \u2014 ")


def strip_publisher_suffix(title: str, entry: dict) -> str:
    """Drop the " - Publisher" tail that Google News appends to every headline.

    The publisher is already available structurally in ``entry.source``, so the
    suffix is redundant noise — and it would skew title clustering by adding a
    constant token to every item from one publisher.
    """
    source = entry.get("source")
    if not isinstance(source, dict):
        return title
    publisher = (source.get("title") or "").strip()
    if not publisher:
        return title
    for separator in _PUBLISHER_SUFFIX_SEPARATORS:
        suffix = f"{separator}{publisher}"
        if title.endswith(suffix):
            return title[: -len(suffix)].strip()
    return title


def derive_fields(title: str, summary: str | None) -> tuple[list[str], list[str]]:
    """Entities and topics for one item.

    Entities come from the headline only: they feed clustering, where a false
    entity corrupts cluster boundaries. Topics come from headline plus summary,
    where extra recall only moves an item into a section it belongs in anyway.
    """
    return (
        entity_names(extract_entities(title)),
        classify_topics(f"{title} {summary or ''}"),
    )


def normalize_entries(
    source: Source,
    entries: list[dict],
    *,
    fetched_at: datetime,
    feed_title: str | None = None,
) -> tuple[list[NormalizedItem], NormalizeStats]:
    """Normalize a batch, reporting what could not be used."""
    stats = NormalizeStats(total=len(entries))
    items: list[NormalizedItem] = []
    for entry in entries:
        item = normalize_entry(
            source, entry, fetched_at=fetched_at, feed_title=feed_title
        )
        if item is None:
            stats.skipped += 1
            continue
        if item.published_at is None:
            stats.missing_dates += 1
        items.append(item)
    return items, stats

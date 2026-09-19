"""Shared fixtures.

Real captured feed XML lives in ``fixtures/``. Live feeds cannot be controlled,
and feedparser tolerates a great deal, so the awkward cases only show up in
fixtures captured on purpose.
"""

from __future__ import annotations

import pathlib
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from telescope.config import Settings, Source
from telescope.models import Article, NormalizedItem
from telescope.normalize import make_id
from telescope.store import SourceRow, Store, iso

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

# host -> captured fixture file
FEED_FIXTURES = {
    "www.gamesindustry.biz": "gamesindustry.xml",
    "www.eurogamer.net": "eurogamer.xml",
    "www.gematsu.com": "gematsu.xml",
}


@pytest.fixture
def load_fixture():
    def load(name: str) -> bytes:
        return (FIXTURES / name).read_bytes()

    return load


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    """Real config directory, throwaway data and digest directories."""
    monkeypatch.setenv("TELESCOPE_DIGEST_DIR", str(tmp_path / "digests"))
    return Settings(project_root=PROJECT_ROOT, data_root=tmp_path)


@pytest.fixture
def store(settings):
    instance = Store(settings.db_path)
    yield instance
    instance.close()


@pytest.fixture
def source() -> Source:
    return Source(
        id="gamesindustry",
        name="GamesIndustry.biz",
        url="https://www.gamesindustry.biz/feed",
        tier="trade",
        axis="business",
    )


@pytest.fixture
def make_client():
    """Build an httpx client backed by a mock transport."""

    def build(handler) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    return build


@pytest.fixture
def feed_fixtures() -> dict[str, str]:
    return dict(FEED_FIXTURES)


@pytest.fixture
def fixture_handler(load_fixture):
    """Serve the captured feeds, recording every request it saw."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        name = FEED_FIXTURES.get(request.url.host)
        if name is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(
            200, content=load_fixture(name), headers={"ETag": f'"{name}"'}
        )

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = iso(NOW)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


@pytest.fixture
def article_factory():
    """Build an in-memory Article for clustering, ranking and rendering tests."""

    def build(
        title: str,
        *,
        source_id: str = "eurogamer",
        source_name: str = "Eurogamer",
        tier: str = "enthusiast",
        summary: str | None = "Something happened.",
        url: str | None = None,
        published: datetime | None = None,
        minutes_ago: float = 60,
        entities: tuple[str, ...] | list[str] = (),
        topics: tuple[str, ...] | list[str] = (),
    ) -> Article:
        link = url if url is not None else f"https://example.com/{source_id}/{_slug(title)}"
        return Article(
            id=f"{source_id}:{_slug(title)}",
            source_id=source_id,
            source_name=source_name,
            tier=tier,
            title=title,
            summary=summary,
            url=link,
            canonical_url=link,
            published_at=published or (NOW - timedelta(minutes=minutes_ago)),
            fetched_at=NOW,
            entities=list(entities),
            topics=list(topics),
        )

    return build


@pytest.fixture
def item_factory():
    """Build a NormalizedItem for storage and digest tests."""

    def build(
        title: str,
        *,
        source_id: str = "eurogamer",
        summary: str | None = "Something happened.",
        published: datetime | None = None,
        guid: str | None = None,
        entities: tuple[str, ...] | list[str] = (),
        topics: tuple[str, ...] | list[str] = (),
    ) -> NormalizedItem:
        identifier = guid or f"{source_id}:{title}"
        slug = _slug(title)
        return NormalizedItem(
            id=make_id(source_id, identifier),
            source_id=source_id,
            guid=identifier,
            title=title,
            summary=summary,
            url=f"https://example.com/{source_id}/{slug}",
            canonical_url=f"https://example.com/{source_id}/{slug}",
            published_at=published or (NOW - timedelta(hours=1)),
            fetched_at=NOW,
            entities=list(entities),
            topics=list(topics),
        )

    return build


@pytest.fixture
def source_row_factory():
    """Build a SourceRow with healthy defaults, for health-report tests."""

    def build(
        source_id: str = "eurogamer",
        name: str = "Eurogamer",
        *,
        tier: str = "enthusiast",
        enabled: bool = True,
        max_age_hours: int = 24,
        item_age_hours: float | None = 1.0,
        last_success_at: str | None = NOW_ISO,
        last_fetch_at: str | None = NOW_ISO,
        consecutive_failures: int = 0,
        last_error: str | None = None,
    ) -> SourceRow:
        return SourceRow(
            id=source_id,
            name=name,
            url="https://example.com/feed",
            tier=tier,
            axis="general",
            enabled=enabled,
            max_age_hours=max_age_hours,
            last_item_age_hours=item_age_hours,
            last_success_at=last_success_at,
            last_fetch_at=last_fetch_at,
            consecutive_failures=consecutive_failures,
            last_error=last_error,
        )

    return build


@pytest.fixture
def loaded_store(store, settings):
    """A store with the real registry applied, so item foreign keys resolve."""
    store.sync_sources(settings.load_sources())
    return store

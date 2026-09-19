"""The telescope web app: browse digests, search items, filter by category.

Run it with ``telescope web``. The app is a read-only viewer over the same
SQLite database the digest pipeline writes, so there is no second copy of the
data and nothing to keep in sync.

htmx does the small, targeted updates — typing in the search box or ticking a
facet re-renders only the browsing column, and the URL is pushed so every view
stays linkable. Without JavaScript the same form is an ordinary GET.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Annotated, Iterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from ..cluster import cluster_articles
from ..config import Settings
from ..health import build_health
from ..rank import rank_stories
from ..store import Store, parse_iso, utcnow
from . import markdown
from .query import DEFAULT_LIMIT, ItemFilter, WebQueries

HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

WINDOW_CHOICES = ((24, "24 hours"), (48, "48 hours"), (168, "7 days"))
DAY_CHOICES = ((0, "All time"), (1, "24 hours"), (7, "7 days"), (30, "30 days"))

_HIGHLIGHT_SAFE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z '\-]*$")


# ---------------------------------------------------------------- jinja filters


def as_datetime(value: object) -> datetime | None:
    """Filters accept both stored ISO strings and live ``datetime`` objects.

    Stored values are ISO strings, but the stories view passes real datetimes
    straight from the clustering code, and both should print the same way.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return parse_iso(value if isinstance(value, str) else None)


def relative_age(value: object) -> str:
    """``3.2h ago``, matching the digest's own wording."""
    parsed = as_datetime(value)
    if parsed is None:
        return "never"
    seconds = max(0.0, (utcnow() - parsed).total_seconds())
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


def local_time(value: object, fmt: str = "%Y-%m-%d %H:%M") -> str:
    parsed = as_datetime(value)
    return parsed.astimezone().strftime(fmt) if parsed else "—"


def short_time(value: object, fmt: str = "%d %b %H:%M") -> str:
    parsed = as_datetime(value)
    return parsed.astimezone().strftime(fmt) if parsed else "—"


def highlight(text: str | None, query: str) -> Markup:
    """Bold the searched words.

    Escaping happens first and only plain words are highlighted, so a query full
    of HTML or regex metacharacters can never inject markup.
    """
    escaped = html_escape(text or "")
    terms = {t for t in (query or "").split() if _HIGHLIGHT_SAFE.match(t)}
    for term in sorted(terms, key=len, reverse=True):
        escaped = re.sub(
            f"({re.escape(html_escape(term))})",
            r"<mark>\1</mark>",
            escaped,
            flags=re.IGNORECASE,
        )
    return Markup(escaped)


# ---------------------------------------------------------------------- factory


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app. ``settings`` is injectable so tests stay sandboxed."""
    settings = settings or Settings.load()

    app = FastAPI(title="Telescope", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["age"] = relative_age
    templates.env.filters["local"] = local_time
    templates.env.filters["clock"] = short_time
    templates.env.filters["highlight"] = highlight

    def get_store(request: Request) -> Iterator[Store]:
        """One short-lived read-only connection per request.

        A connection per request keeps SQLite's thread affinity out of the
        picture — sync endpoints run in a threadpool — and costs almost nothing
        for a local file.
        """
        db_path = request.app.state.settings.db_path
        try:
            store = Store(db_path, readonly=True)
        except sqlite3.Error as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"cannot open the database at {db_path} ({exc}). "
                    "Run `telescope sync` first."
                ),
            ) from exc
        try:
            yield store
        finally:
            store.close()

    def render(request: Request, name: str, *, title: str, page_id: str, **context):
        return templates.TemplateResponse(
            request,
            name,
            {
                "request": request,
                "title": title,
                "page": page_id,
                "q": "",
                "digest_dir": settings.digests_dir,
                **context,
            },
        )

    # -- overview ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, store: Store = Depends(get_store)) -> HTMLResponse:
        queries = WebQueries(store)
        digest = queries.latest_digest()
        recent, digest_total = queries.digests(limit=5)
        last_sync_at = store.get_state("last_sync_at")
        stats = store.stats()
        health = build_health(
            store.list_sources(),
            items_considered=int(stats["items"]),  # type: ignore[arg-type]
            stories=digest.stories if digest else 0,
            last_sync_at=last_sync_at,
        )
        return render(
            request,
            "dashboard.html",
            title="Overview",
            page_id="dashboard",
            digest=digest,
            digest_total=digest_total,
            digests=recent,
            stats=stats,
            last_sync_at=last_sync_at,
            last_digest_at=store.get_state("last_digest_at"),
            topics=queries.facets(ItemFilter())["topics"][:10],
            health=health,
        )

    # -- digests -----------------------------------------------------------

    @app.get("/digests", response_class=HTMLResponse)
    def digest_list(
        request: Request,
        store: Store = Depends(get_store),
        offset: int = Query(0, ge=0),
    ) -> HTMLResponse:
        rows, total = WebQueries(store).digests(limit=60, offset=offset)
        return render(
            request,
            "digests.html",
            title="Digests",
            page_id="digests",
            digests=rows,
            total=total,
            offset=offset,
            limit=60,
            has_next=offset + 60 < total,
            prev_offset=max(0, offset - 60),
            next_offset=offset + 60,
        )

    @app.get("/digests/{digest_id}", response_class=HTMLResponse)
    def digest_detail(
        request: Request, store: Store = Depends(get_store), digest_id: int = 0
    ) -> HTMLResponse:
        digest = WebQueries(store).digest(digest_id)
        if digest is None:
            raise HTTPException(status_code=404, detail=f"no digest with id {digest_id}")

        body: str | None = None
        error: str | None = None
        sections: list[dict[str, str]] = []

        if digest.path:
            try:
                text = Path(digest.path).read_text(encoding="utf-8")
            except OSError as exc:
                error = f"could not read {digest.path} — {exc}"
            else:
                body = markdown.render(text)
                sections = [
                    {"title": title, "anchor": markdown.slugify(title, occurrence)}
                    for title, occurrence in markdown.sections(text)
                ]

        return render(
            request,
            "digest_detail.html",
            title=f"Digest {digest.day}",
            page_id="digests",
            digest=digest,
            body=body,
            error=error,
            sections=sections,
        )

    @app.get("/digests/{digest_id}/raw", response_class=PlainTextResponse)
    def digest_raw(
        store: Store = Depends(get_store), digest_id: int = 0
    ) -> PlainTextResponse:
        digest = WebQueries(store).digest(digest_id)
        if digest is None or not digest.path:
            raise HTTPException(status_code=404, detail="this digest has no stored file")
        try:
            text = Path(digest.path).read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return PlainTextResponse(text)

    # -- items -------------------------------------------------------------

    @app.get("/items", response_class=HTMLResponse)
    def item_list(
        request: Request,
        store: Store = Depends(get_store),
        q: str = Query(""),
        topic: Annotated[list[str], Query()] = [],
        tag: Annotated[list[str], Query()] = [],
        source: Annotated[list[str], Query()] = [],
        kind: str = Query(""),
        days: int = Query(0),
        offset: int = Query(0, ge=0),
    ) -> HTMLResponse:
        filters = ItemFilter(
            q=(q or "").strip(),
            topics=tuple(topic),
            tags=tuple(tag),
            sources=tuple(source),
            kind=kind,
            days=max(0, days),
            offset=max(0, offset),
            limit=DEFAULT_LIMIT,
        )
        queries = WebQueries(store)
        items, total = queries.items(filters)
        facets = queries.facets(filters)

        return render(
            request,
            "items.html",
            title="Items",
            page_id="items",
            filters=filters,
            items=items,
            total=total,
            facets=facets,
            kinds=queries.kinds(),
            day_choices=DAY_CHOICES,
            showing_from=filters.offset + 1 if total else 0,
            showing_to=min(filters.offset + filters.limit, total),
            has_prev=filters.offset > 0,
            has_next=filters.offset + filters.limit < total,
            prev_offset=max(0, filters.offset - filters.limit),
            next_offset=filters.offset + filters.limit,
        )

    @app.get("/items/{item_id}", response_class=HTMLResponse)
    def item_detail(
        request: Request, store: Store = Depends(get_store), item_id: str = ""
    ) -> HTMLResponse:
        queries = WebQueries(store)
        item = queries.item(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="no such item")
        related, _ = queries.items(ItemFilter(sources=(item.source_id,), limit=8))
        return render(
            request,
            "item_detail.html",
            title=item.title,
            page_id="items",
            item=item,
            related=[row for row in related if row.id != item.id][:7],
        )

    # -- stories -----------------------------------------------------------

    @app.get("/stories", response_class=HTMLResponse)
    def story_list(
        request: Request,
        store: Store = Depends(get_store),
        hours: int = Query(24),
        topic: Annotated[list[str], Query()] = [],
        q: str = Query(""),
    ) -> HTMLResponse:
        """The digest's building blocks: clustered and ranked, nothing written."""
        config = settings.load_digest_config()
        watchlist = settings.load_watchlist()
        now = utcnow()
        window = max(1, min(hours, 24 * 14))

        articles = store.items_between(now - timedelta(hours=window), now)
        stories = rank_stories(
            cluster_articles(articles, config.clustering),
            config=config,
            watchlist=watchlist,
            now=now,
        )

        selected = set(topic)
        if selected:
            stories = [s for s in stories if selected & set(s.topics)]
        needle = (q or "").strip().lower()
        if needle:
            stories = [s for s in stories if needle in s.searchable_text().lower()]

        return render(
            request,
            "stories.html",
            title="Stories",
            page_id="stories",
            q=q,
            stories=stories[:150],
            total=len(stories),
            items_considered=len(articles),
            hours=window,
            window_choices=WINDOW_CHOICES,
            selected_topics=selected,
            topic_counts=WebQueries(store).facets(ItemFilter(days=max(1, window // 24)))[
                "topics"
            ],
            config=config,
        )

    # -- sources -----------------------------------------------------------

    @app.get("/sources", response_class=HTMLResponse)
    def source_list(
        request: Request, store: Store = Depends(get_store)
    ) -> HTMLResponse:
        sources = store.list_sources()
        health = build_health(sources, last_sync_at=store.get_state("last_sync_at"))
        return render(
            request,
            "sources.html",
            title="Sources",
            page_id="sources",
            rows=health.sources,
            by_id={row.id: row for row in sources},
            health=health,
            stats=store.stats(),
        )

    @app.get("/sources/{source_id}", response_class=HTMLResponse)
    def source_detail(
        request: Request, store: Store = Depends(get_store), source_id: str = ""
    ) -> HTMLResponse:
        row = store.get_source(source_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such source")
        items, total = WebQueries(store).items(
            ItemFilter(sources=(source_id,), limit=25)
        )
        return render(
            request,
            "source_detail.html",
            title=row.name,
            page_id="sources",
            source=row,
            history=store.source_fetch_history(source_id, limit=10),
            items=items,
            total=total,
        )

    # -- errors ------------------------------------------------------------

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if "text/html" not in request.headers.get("accept", ""):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "request": request,
                "title": str(exc.status_code),
                "page": "",
                "q": "",
                "digest_dir": settings.digests_dir,
                "status": exc.status_code,
                "detail": exc.detail,
            },
            status_code=exc.status_code,
        )

    return app


__all__ = ["create_app"]

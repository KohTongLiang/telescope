# Telescope

Games industry news and release digest. Fetches RSS/Atom feeds, normalizes them
into one canonical schema, and stores them in SQLite.

Phase 0 scope: ingestion, normalization, dedupe by GUID, and a working `sync`.
Ranking, clustering, rendering and scheduling arrive in later phases.

See `DESIGN.md` in the Drive folder for the full design.

## Install

```sh
uv sync
```

## Use

```sh
uv run telescope sync              # fetch all enabled sources
uv run telescope sync --only gematsu
uv run telescope digest            # build and write the daily digest
uv run telescope digest --stdout   # print it instead of writing
uv run telescope stories           # ranked stories (the tuning view)
uv run telescope stories --explain # ...with every score component
uv run telescope sources           # health and staleness per source
uv run telescope search "layoffs"  # FTS over stored items
uv run telescope recent --limit 20
uv run telescope reindex           # recompute entities/topics, no refetch
uv run telescope stats
uv run telescope web               # local UI: digests, search, categories
```

## Web UI

`uv run telescope web` serves a read-only viewer at <http://127.0.0.1:8765/>:

- **Digests** — every row in the `digests` table, with the markdown rendered inline
- **Items** — full-text search (FTS5) with faceted filters for topic, feed tag,
  source, kind and time window
- **Stories** — the clustered, ranked stories the digest is built from
- **Sources** — per-source health, staleness and fetch history

It opens the same SQLite file the pipeline writes, using `PRAGMA query_only`, so
it is safe to leave running while `sync` and `digest` work. FastAPI + Jinja2 +
htmx, with no build step and no CDN — htmx is vendored in
`telescope/web/static/`.

## Scheduling

Two launchd agents keep it current: `com.telescope.sync` every 3 hours, and
`com.telescope.digest` daily at 08:00 local.

```sh
uv run telescope schedule show         # preview, changes nothing
uv run telescope schedule install      # write the plists and load them
uv run telescope schedule status       # loaded? last exit code? log times?
uv run telescope schedule kickstart    # run the sync agent now
uv run telescope schedule logs         # tail both logs
uv run telescope schedule uninstall    # stop and remove
```

**Full start/stop/monitor/troubleshooting guide:** `SCHEDULING.md`, in the Drive
folder next to the digests.

## Layout

- `telescope/` — the package
- `telescope/web/` — the local viewer: FastAPI app, templates, vendored htmx
- `config/sources.yaml` — feed registry; adding a source is a YAML entry
- `config/digest.yaml` — ranking weights, thresholds, digest output, webhook
- `config/watchlist.yaml` — entities that earn a boost and their own section
- `var/telescope.db` — SQLite database (never synced to Drive)
- `~/Library/Logs/Telescope/` — agent stdout/stderr
- `tests/fixtures/` — captured real feed XML, including broken cases

## Notes

- `id` is `sha256(source_id + guid)`, and the feed **GUID is the primary key, not
  the date** — a meaningful number of publishers emit broken or absent dates.
- HTTP uses `httpx`, not stdlib `urllib`: this machine's Python 3.13 framework
  build has no CA bundle, so `urllib` fails TLS verification on every request.
- `~/Library/LaunchAgents/` holds the two agent plists; logs live in
  `~/Library/Logs/Telescope/`. Neither is inside Drive.

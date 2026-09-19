"""Command line interface."""

from __future__ import annotations

import json as jsonlib
import os
import subprocess
import threading
import webbrowser
from datetime import datetime, timedelta, timezone

import typer

from . import __version__
from .cluster import Story, cluster_articles
from .config import ConfigError, Settings
from .digest import DigestResult, build_digest, run_digest
from .normalize import derive_fields
from .rank import rank_stories
from .schedule import (
    DEFAULT_DIGEST_HOUR,
    DEFAULT_DIGEST_MINUTE,
    DEFAULT_SYNC_MINUTE,
    agent_status,
    default_specs,
    describe,
    launch_agents_dir,
    tail,
)
from .schedule import install as install_agents
from .schedule import uninstall as uninstall_agents
from .store import Store, parse_iso, utcnow
from .sync import SyncReport, run_sync

app = typer.Typer(
    add_completion=False,
    help="Games industry news and release digest.",
    no_args_is_help=True,
)

STATUS_MARK = {
    "ok": "ok",
    "not_modified": "unchanged",
    "http_error": "HTTP ERROR",
    "network_error": "NETWORK",
    "parse_error": "PARSE ERROR",
    "internal_error": "INTERNAL",
}


def _settings() -> Settings:
    return Settings.load()


def _open_store(settings: Settings) -> Store:
    return Store(settings.db_path)


def _age(hours: float | None) -> str:
    if hours is None:
        return "-"
    if hours < 1:
        return f"{hours * 60:.0f}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _since(timestamp: str | None, now: datetime) -> str:
    parsed = parse_iso(timestamp)
    if parsed is None:
        return "never"
    seconds = max(0.0, (now - parsed).total_seconds())
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


def _clip(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _fail(message: str, code: int = 2) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


@app.command()
def sync(
    only: list[str] = typer.Option(
        None, "--only", "-o", help="Limit to these source ids. Repeatable."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Fetch every enabled source and store anything new."""
    settings = _settings()
    try:
        with _open_store(settings) as store:
            report = run_sync(settings, store, only=only)
    except (ConfigError, ValueError) as exc:
        _fail(str(exc))

    if as_json:
        typer.echo(jsonlib.dumps(_report_json(report), indent=2))
        raise typer.Exit(0 if not report.all_failed else 1)

    _print_report(report)
    raise typer.Exit(0 if not report.all_failed else 1)


def _report_json(report: SyncReport) -> dict:
    return {
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "duration_ms": report.duration_ms,
        "ok_count": report.ok_count,
        "total_sources": len(report.results),
        "total_found": report.total_found,
        "total_new": report.total_new,
        "all_failed": report.all_failed,
        "sources": [
            {
                "id": r.source_id,
                "name": r.name,
                "status": r.status,
                "http_status": r.http_status,
                "items_found": r.items_found,
                "items_new": r.items_new,
                "items_duplicate": r.items_duplicate,
                "items_unparsed_dates": r.items_unparsed_dates,
                "item_age_hours": r.item_age_hours,
                "duration_ms": r.duration_ms,
                "error": r.error,
                "warning": r.warning,
            }
            for r in report.results
        ],
    }


def _print_report(report: SyncReport) -> None:
    typer.secho(
        f"Telescope sync — {report.finished_at.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC",
        bold=True,
    )
    header = f"{'source':<26}{'status':<13}{'found':>6}{'new':>6}{'newest':>8}{'ms':>7}"
    typer.echo(header)
    typer.echo("-" * len(header))

    for r in report.results:
        line = (
            f"{_clip(r.name, 25):<26}"
            f"{STATUS_MARK.get(r.status, r.status):<13}"
            f"{r.items_found if r.items_found else '-':>6}"
            f"{r.items_new if r.items_new else '-':>6}"
            f"{_age(r.item_age_hours):>8}"
            f"{r.duration_ms:>7}"
        )
        if r.ok and not r.warning:
            typer.echo(line)
        else:
            typer.secho(line, fg=typer.colors.YELLOW if r.ok else typer.colors.RED)
        if r.error:
            typer.secho(f"    ↳ {_clip(r.error, 110)}", fg=typer.colors.RED)
        if r.warning:
            typer.secho(f"    ↳ parse warning: {_clip(r.warning, 100)}", fg=typer.colors.YELLOW)

    typer.echo("")
    summary = (
        f"{report.ok_count}/{len(report.results)} sources OK · "
        f"{report.total_found} items → {report.total_new} new · "
        f"{report.duration_ms}ms"
    )
    if report.all_failed:
        typer.secho(summary, fg=typer.colors.RED, bold=True)
        typer.secho(
            "every source failed — this is a failure, not a quiet news day",
            fg=typer.colors.RED,
        )
    else:
        typer.secho(summary, fg=typer.colors.GREEN if not report.failed else typer.colors.YELLOW)


@app.command()
def sources() -> None:
    """Show registry health: last result, newest item age, staleness."""
    settings = _settings()
    now = utcnow()
    try:
        with _open_store(settings) as store:
            settings.load_sources()
            store.sync_sources(settings.load_sources())
            rows = store.list_sources()
    except ConfigError as exc:
        _fail(str(exc))

    header = (
        f"{'id':<15}{'name':<24}{'tier':<13}{'axis':<12}"
        f"{'last ok':>10}{'newest':>8}  state"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for row in rows:
        if not row.enabled:
            state = "disabled"
        elif row.last_fetch_at is None:
            state = "never fetched"
        elif row.consecutive_failures:
            state = f"failing x{row.consecutive_failures}"
        elif row.stale():
            state = "STALE"
        else:
            state = "ok"

        colour = typer.colors.RED if state in ("STALE",) or "failing" in state else None
        line = (
            f"{row.id:<15}{_clip(row.name, 23):<24}{row.tier:<13}{row.axis:<12}"
            f"{_since(row.last_success_at, now):>10}{_age(row.last_item_age_hours):>8}  {state}"
        )
        typer.secho(line, fg=colour)
        if row.last_error:
            typer.secho(f"    ↳ {_clip(row.last_error, 100)}", fg=typer.colors.RED)
    typer.echo("")
    typer.echo(f"database: {settings.db_path}")


@app.command()
def search(
    query: str = typer.Argument(..., help="Full-text query (FTS5 syntax, or plain words)."),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """Search stored items."""
    settings = _settings()
    with _open_store(settings) as store:
        rows = store.search(query, limit=limit)
        if not store.fts_enabled:
            typer.secho("note: FTS5 unavailable, used LIKE fallback", fg=typer.colors.YELLOW)
    if not rows:
        typer.echo("no matches")
        raise typer.Exit(0)
    for row in rows:
        when = (row["published_at"] or row["fetched_at"] or "")[:16].replace("T", " ")
        typer.echo(f"{when}  {_clip(row['source_name'], 16):<16}  {_clip(row['title'], 78)}")
    typer.echo(f"\n{len(rows)} match(es)")


@app.command()
def recent(
    limit: int = typer.Option(20, "--limit", "-n"),
    source: str = typer.Option(None, "--source", "-s", help="Filter to one source id."),
) -> None:
    """List the newest stored items."""
    settings = _settings()
    with _open_store(settings) as store:
        rows = store.recent_items(limit=limit, source_id=source)
    if not rows:
        typer.echo("nothing stored yet — run `telescope sync`")
        raise typer.Exit(0)
    for row in rows:
        when = (row["published_at"] or row["fetched_at"] or "")[:16].replace("T", " ")
        typer.echo(f"{when}  {_clip(row['source_name'], 16):<16}  {_clip(row['title'], 78)}")


@app.command()
def stats() -> None:
    """Show database counts."""
    settings = _settings()
    with _open_store(settings) as store:
        data = store.stats()
        last_sync = store.get_state("last_sync_at")
    for key, value in data.items():
        typer.echo(f"{key:<20} {value}")
    typer.echo(f"{'last_sync_at':<20} {last_sync or 'never'}")


def _coverage_short(story: Story) -> str:
    sources = story.sources
    if not sources:
        return "?"
    others = len(sources) - 1
    return sources[0] + (f" +{others}" if others else "")


def _print_digest_summary(result: DigestResult) -> None:
    typer.secho(f"Digest written — {result.path}", bold=True)
    typer.echo(
        f"window   {result.window_start:%Y-%m-%d %H:%M} UTC → "
        f"{result.window_end:%Y-%m-%d %H:%M} UTC ({result.window_hours:.0f}h)"
    )
    typer.echo(
        f"volume   {result.health.items_considered} items → {len(result.stories)} stories"
    )
    typer.echo(f"sources  {result.health.summary_line}")
    typer.echo(
        f"marked   {'yes' if result.marked else 'no — watermark unchanged'}"
    )
    if result.config.webhook_url:
        if result.webhook_ok is True:
            typer.echo(f"webhook  sent to {result.config.webhook_url}")
        elif result.webhook_ok is False:
            typer.secho(
                f"webhook  FAILED — {result.webhook_error} (the digest was still written)",
                fg=typer.colors.YELLOW,
            )
    if result.top_stories:
        typer.echo("")
        typer.echo("top stories")
        for position, story in enumerate(result.top_stories, start=1):
            typer.echo(
                f"  {position}. {story.score:5.2f}  "
                f"{_clip(story.title, 62):<62} {_clip(_coverage_short(story), 16)}"
            )
    if result.health.everything_failed:
        typer.secho(
            "every source failed — this is a failure, not a quiet news day",
            fg=typer.colors.RED,
        )


@app.command()
def digest(
    since: str = typer.Option(
        None, "--since", help="ISO timestamp to open the window at."
    ),
    hours: float = typer.Option(
        None, "--hours", help="Window length when no watermark exists yet."
    ),
    out: str = typer.Option(
        None, "--out", help="Output directory. Defaults to digest.yaml output_dir."
    ),
    stdout: bool = typer.Option(
        False, "--stdout", help="Print the digest instead of writing it."
    ),
    no_mark: bool = typer.Option(
        False, "--no-mark", help="Do not advance the digest watermark."
    ),
    force: bool = typer.Option(
        False, "--force", help="Ignore the stored watermark and use --hours."
    ),
) -> None:
    """Build the daily digest and write it to the digests folder."""
    settings = _settings()
    parsed_since = parse_iso(since) if since else None
    if since and parsed_since is None:
        _fail(f"--since is not a valid ISO timestamp: {since}")
    if force and parsed_since is None:
        parsed_since = utcnow() - timedelta(
            hours=hours if hours is not None else settings.load_digest_config().window_hours_default
        )

    try:
        with _open_store(settings) as store:
            if stdout:
                result = build_digest(
                    settings, store, since=parsed_since, window_hours=hours
                )
            else:
                result = run_digest(
                    settings,
                    store,
                    out_dir=out,
                    since=parsed_since,
                    window_hours=hours,
                    mark=not no_mark,
                )
    except ConfigError as exc:
        _fail(str(exc))

    if stdout:
        typer.echo(result.markdown)
        raise typer.Exit(1 if result.health.everything_failed else 0)

    _print_digest_summary(result)
    raise typer.Exit(1 if result.health.everything_failed else 0)


@app.command()
def stories(
    hours: float = typer.Option(24.0, "--hours", help="How far back to look."),
    limit: int = typer.Option(20, "--limit", "-n"),
    explain: bool = typer.Option(
        False, "--explain", help="Show every score component and the classification."
    ),
) -> None:
    """Show clustered and ranked stories without writing anything.

    This is the tuning view: when the digest surfaces something silly, this says
    which rule did it.
    """
    settings = _settings()
    config = settings.load_digest_config()
    watchlist = settings.load_watchlist()
    now = utcnow()
    start = now - timedelta(hours=max(0.0, hours))

    with _open_store(settings) as store:
        articles = store.items_between(start, now)
        clustered = cluster_articles(articles, config.clustering)
        ranked = rank_stories(clustered, config=config, watchlist=watchlist, now=now)

    typer.echo(
        f"{len(articles)} items → {len(ranked)} stories over {hours:g}h "
        f"(containment {config.clustering.containment_threshold:g})"
    )
    typer.echo("")
    for position, story in enumerate(ranked[:limit], start=1):
        typer.echo(
            f"{position:>3}. {story.score:6.2f}  x{story.breadth}  "
            f"{_clip(story.title, 74)}"
        )
        if explain:
            components = "  ".join(
                f"{name}={value:g}" for name, value in story.components.items()
            )
            typer.echo(f"      {components}")
            typer.echo(
                f"      {_coverage_short(story)}"
                f" · topics={','.join(story.topics) or '-'}"
                f" · entities={','.join(story.entities) or '-'}"
            )


@app.command()
def reindex() -> None:
    """Recompute entities and topics for stored items, without refetching."""
    settings = _settings()
    changed = 0
    total = 0
    with _open_store(settings) as store:
        for row in store.article_derived_rows():
            total += 1
            entities, topics = derive_fields(row["title"], row["summary"])
            previous_entities = jsonlib.loads(row["entities"] or "[]")
            previous_topics = jsonlib.loads(row["topics"] or "[]")
            if previous_entities != entities or previous_topics != topics:
                store.update_item_derived(row["id"], entities, topics)
                changed += 1
    typer.echo(f"reindexed {total} items, {changed} updated")


schedule_app = typer.Typer(
    help="Manage the launchd agents that keep Telescope current.",
    no_args_is_help=True,
)
app.add_typer(schedule_app, name="schedule")


def _agent_specs(
    sync_hours: str | None = None,
    sync_minute: int = DEFAULT_SYNC_MINUTE,
    digest_hour: int = DEFAULT_DIGEST_HOUR,
    digest_minute: int = DEFAULT_DIGEST_MINUTE,
) -> list:
    hours = None
    if sync_hours:
        try:
            hours = tuple(int(part) for part in sync_hours.split(",") if part.strip())
        except ValueError:
            _fail("--sync-hours must be comma-separated hours, e.g. 0,6,12,18")
        if not hours:
            _fail("--sync-hours listed no hours")
        for hour in hours:
            if not 0 <= hour <= 23:
                _fail(f"--sync-hours out of range: {hour}")
    return default_specs(
        _settings().project_root,
        sync_hours=hours,
        sync_minute=sync_minute,
        digest_hour=digest_hour,
        digest_minute=digest_minute,
    )


@schedule_app.command("show")
def schedule_show(
    sync_hours: str = typer.Option(
        None, "--sync-hours", help="Comma-separated hours, e.g. 0,6,12,18."
    ),
    sync_minute: int = typer.Option(DEFAULT_SYNC_MINUTE, "--sync-minute"),
    digest_hour: int = typer.Option(DEFAULT_DIGEST_HOUR, "--digest-hour"),
    digest_minute: int = typer.Option(DEFAULT_DIGEST_MINUTE, "--digest-minute"),
) -> None:
    """Print what would be installed, without touching anything."""
    typer.secho("Times are system local time (launchd StartCalendarInterval).", dim=True)
    typer.echo("")
    for spec in _agent_specs(sync_hours, sync_minute, digest_hour, digest_minute):
        typer.secho(describe(spec), bold=True)
        typer.echo(f"  plist    {launch_agents_dir() / spec.plist_name}")
        typer.echo(f"  run      {' '.join(spec.program_args)}")
        typer.echo(f"  workdir  {spec.working_dir}")
        typer.echo(f"  stdout   {spec.stdout_path}")
        typer.echo(f"  stderr   {spec.stderr_path}")
        typer.echo("")


@schedule_app.command("install")
def schedule_install(
    sync_hours: str = typer.Option(
        None, "--sync-hours", help="Comma-separated hours, e.g. 0,6,12,18."
    ),
    sync_minute: int = typer.Option(DEFAULT_SYNC_MINUTE, "--sync-minute"),
    digest_hour: int = typer.Option(DEFAULT_DIGEST_HOUR, "--digest-hour"),
    digest_minute: int = typer.Option(DEFAULT_DIGEST_MINUTE, "--digest-minute"),
    no_load: bool = typer.Option(
        False, "--no-load", help="Write the plists but do not load them."
    ),
) -> None:
    """Write the agents and load them. Idempotent: safe to re-run."""
    specs = _agent_specs(sync_hours, sync_minute, digest_hour, digest_minute)
    results = install_agents(specs, load=not no_load)

    failed = False
    for path, ok, detail in results:
        if ok:
            typer.secho(f"ok       {path.name}  ({detail})", fg=typer.colors.GREEN)
        else:
            failed = True
            typer.secho(f"FAILED   {path.name}  ({detail})", fg=typer.colors.RED)

    typer.echo("")
    for spec in specs:
        typer.echo(f"  {describe(spec)}")

    typer.echo("")
    typer.echo("verify with:  telescope schedule status")
    typer.echo("run one now:  telescope schedule kickstart")
    typer.echo("")
    typer.secho("First run only:", bold=True)
    typer.echo(
        "  If sync works but the digest never appears in Drive, the agent needs\n"
        "  Full Disk Access. See SCHEDULING.md. The interpreter to grant it to is:"
    )
    typer.echo(f"  {specs[0].program_args[0]}")

    if failed:
        raise typer.Exit(1)


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """Stop both agents and remove their plists."""
    removed = uninstall_agents(_agent_specs())
    if not removed:
        typer.echo("nothing installed")
        raise typer.Exit(0)
    for path in removed:
        typer.secho(f"removed  {path}", fg=typer.colors.GREEN)
    typer.echo("")
    typer.echo("logs were left in place; delete them with:")
    typer.echo("  rm -rf ~/Library/Logs/Telescope")


@schedule_app.command("status")
def schedule_status() -> None:
    """Show whether each agent is installed, loaded, and how its last run went."""
    specs = _agent_specs()
    now = utcnow()
    all_loaded = True
    for spec in specs:
        status = agent_status(spec)
        if not status.loaded:
            all_loaded = False

        typer.secho(status.label, bold=True)
        typer.echo(
            f"  plist     {'present' if status.installed else 'MISSING'}  {status.plist_path}"
        )
        if status.loaded:
            typer.echo(
                f"  loaded    yes   state={status.state or '?'}"
                f"  last exit={status.last_exit_code if status.last_exit_code is not None else '?'}"
                f"  runs={status.runs if status.runs is not None else '?'}"
            )
            if status.last_exit_code not in (None, 0):
                typer.secho(
                    f"  warning   last run exited {status.last_exit_code} — see the logs",
                    fg=typer.colors.RED,
                )
        else:
            typer.echo(f"  loaded    NO    ({status.detail})")

        for label, path in (("stdout", status.stdout_path), ("stderr", status.stderr_path)):
            if path and path.exists():
                stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime(
                    "%Y-%m-%d %H:%M"
                )
                typer.echo(f"  {label:<9} {path}  (last written {stamp})")
            else:
                typer.echo(f"  {label:<9} {path}  (not written yet)")
        typer.echo("")

    raise typer.Exit(0 if all_loaded else 1)


@schedule_app.command("logs")
def schedule_logs(
    lines: int = typer.Option(40, "--lines", "-n"),
    err: bool = typer.Option(False, "--err", help="Show only stderr."),
) -> None:
    """Show the tail of the agent logs."""
    for spec in _agent_specs():
        if not err:
            typer.secho(f"=== {spec.label} stdout — {spec.stdout_path}", bold=True)
            typer.echo(tail(spec.stdout_path, lines=lines))
            typer.echo("")
        typer.secho(f"=== {spec.label} stderr — {spec.stderr_path}", bold=True)
        typer.echo(tail(spec.stderr_path, lines=lines))
        typer.echo("")
    typer.echo("follow live with:  tail -f ~/Library/Logs/Telescope/*.log")


@schedule_app.command("kickstart")
def schedule_kickstart(
    which: str = typer.Option(
        "sync", "--which", help="Which agent to run now: sync, digest, or both."
    ),
) -> None:
    """Run an agent immediately, without waiting for its schedule."""
    if which not in ("sync", "digest", "both"):
        _fail("--which must be sync, digest, or both")

    targets = ["sync", "digest"] if which == "both" else [which]
    for spec in _agent_specs():
        if not any(spec.label.endswith(target) for target in targets):
            continue
        result = subprocess.run(
            ["launchctl", "kickstart", "-p", f"gui/{os.getuid()}/{spec.label}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            pid = (result.stdout or "").strip()
            typer.secho(f"started  {spec.label}  pid={pid}", fg=typer.colors.GREEN)
        else:
            typer.secho(
                f"FAILED   {spec.label}  {(result.stderr or '').strip()}",
                fg=typer.colors.RED,
            )
            typer.echo("  is it installed?  telescope schedule status")
    typer.echo("")
    typer.echo("then:  telescope schedule logs")


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind."),
    port: int = typer.Option(8765, "--port", "-p", help="Port to serve on."),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open a browser."),
) -> None:
    """Serve the local web UI: digests, search, and browsing by category.

    The viewer is read-only, so it is safe to leave running while `sync` and
    `digest` write to the same database.
    """
    try:
        import uvicorn
    except ImportError:  # pragma: no cover - depends on how it was installed
        _fail("uvicorn is not installed — run `uv sync`")

    from .web.app import create_app

    settings = _settings()
    if not settings.db_path.exists():
        _fail(f"no database at {settings.db_path} — run `telescope sync` first")

    url = f"http://{host}:{port}/"
    typer.secho(f"Telescope web — {url}", bold=True)
    typer.echo(f"database: {settings.db_path}")
    typer.echo("read-only view; stop with ctrl-c")

    if not no_open:
        threading.Timer(0.7, webbrowser.open, args=(url,)).start()

    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(f"telescope {__version__}")


if __name__ == "__main__":
    app()

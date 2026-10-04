import re
import time
from datetime import datetime

import typer
from rich.console import Console
from rich.table import Table

from tempus import catalog, db
from tempus.config import get_settings
from tempus.sources import SOURCE_NAMES, get_source
from tempus.sources.fred import FredError
from tempus.sync import sync_series

app = typer.Typer(help="Tempus: collect time-series data into TimescaleDB.", no_args_is_help=True)
db_app = typer.Typer(help="Database management.", no_args_is_help=True)
catalog_app = typer.Typer(
    help="Manage catalog.toml (the series `sync` tracks).", no_args_is_help=True
)
series_app = typer.Typer(help="Inspect series stored in the database.", no_args_is_help=True)
fred_app = typer.Typer(help="Explore FRED.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(catalog_app, name="catalog")
app.add_typer(series_app, name="series")
app.add_typer(fred_app, name="fred")

console = Console()
SPARK = "▁▂▃▄▅▆▇█"


def _connect():
    try:
        return db.connect(get_settings().database_url)
    except Exception as exc:
        console.print(f"[red]Could not connect to the database:[/red] {exc}")
        console.print("Is it running? Try [bold]mise run db:up[/bold]")
        raise typer.Exit(1) from exc


def _source(name: str, settings):
    try:
        return get_source(name, settings)
    except (FredError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    return "".join(SPARK[int((v - lo) / span * (len(SPARK) - 1))] for v in values)


def _parse_interval(text: str) -> int:
    match = re.fullmatch(r"(\d+)([smhd])", text)
    if not match:
        raise typer.BadParameter("use a number + unit, e.g. 30m, 6h, 1d")
    n, unit = match.groups()
    return int(n) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


# --- db --------------------------------------------------------------------


@db_app.command("init")
def db_init():
    """Create the extension, tables, and hypertable (idempotent)."""
    with _connect() as conn:
        db.init_schema(conn)
    console.print("[green]Schema ready.[/green]")


@db_app.command("status")
def db_status():
    """Show connection info and row counts."""
    with _connect() as conn:
        info = db.status(conn)
    for key, value in info.items():
        console.print(f"[bold]{key:>13}[/bold]  {_fmt(value)}")


# --- catalog ---------------------------------------------------------------


@catalog_app.command("list")
def catalog_list():
    """List series in catalog.toml."""
    entries = catalog.load(get_settings().catalog_path)
    for e in entries:
        console.print(f"{e.source}:{e.id}")
    if not entries:
        console.print("[dim]catalog is empty[/dim]")


@catalog_app.command("add")
def catalog_add(
    source: str = typer.Argument(..., help=f"One of: {', '.join(SOURCE_NAMES)}"),
    series_ids: list[str] = typer.Argument(..., help="One or more series IDs"),
    sync: bool = typer.Option(False, "--sync", help="Sync the new series immediately"),
):
    """Add series to catalog.toml."""
    settings = get_settings()
    src = _source(source, settings)
    for series_id in series_ids:
        series_id = series_id.upper() if source == "fred" else series_id
        meta = src.fetch_meta(series_id)  # validates the ID exists
        if catalog.add(settings.catalog_path, source, series_id):
            console.print(f"[green]added[/green] {source}:{series_id}  [dim]{meta.title}[/dim]")
        else:
            console.print(f"[yellow]already present[/yellow] {source}:{series_id}")
        if sync:
            with _connect() as conn:
                _print_result(sync_series(conn, src, series_id))


@catalog_app.command("remove")
def catalog_remove(source: str, series_id: str):
    """Remove a series from catalog.toml (stored data is kept; see `series delete`)."""
    if catalog.remove(get_settings().catalog_path, source, series_id):
        console.print(f"[green]removed[/green] {source}:{series_id}")
    else:
        console.print(f"[yellow]not in catalog[/yellow] {source}:{series_id}")


# --- sync ------------------------------------------------------------------


def _print_result(r) -> None:
    color = {"updated": "green", "unchanged": "dim", "failed": "red"}[r.status]
    detail = f"+{r.rows_upserted} / -{r.rows_deleted}" if r.status == "updated" else (r.error or "")
    console.print(f"[{color}]{r.status:>9}[/{color}]  {r.source}:{r.series_id}  {detail}")


@app.command()
def sync(
    source: str | None = typer.Option(None, help="Only sync this source"),
    series: list[str] | None = typer.Option(None, "--series", "-s", help="Only these IDs"),
    force: bool = typer.Option(False, help="Re-download even if upstream hasn't changed"),
    every: str | None = typer.Option(
        None, help="Keep running, syncing on this interval (e.g. 6h). Ctrl-C to stop."
    ),
):
    """Sync series from catalog.toml into the database."""
    settings = get_settings()
    interval = _parse_interval(every) if every else None

    while True:
        entries = [
            e
            for e in catalog.load(settings.catalog_path)
            if (source is None or e.source == source) and (not series or e.id in series)
        ]
        if not entries:
            console.print("[yellow]Nothing to sync.[/yellow] Add series with `tempus catalog add`.")
            raise typer.Exit(1)

        failed = 0
        sources = {}
        with _connect() as conn:
            for e in entries:
                if e.source not in sources:
                    sources[e.source] = _source(e.source, settings)
                result = sync_series(conn, sources[e.source], e.id, force=force)
                failed += result.status == "failed"
                _print_result(result)

        if interval is None:
            raise typer.Exit(1 if failed else 0)
        console.print(f"[dim]next sync in {every}…[/dim]")
        time.sleep(interval)


# --- series ----------------------------------------------------------------


@series_app.command("list")
def series_list():
    """Series in the database with coverage and freshness."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.source, s.series_id, s.title, s.frequency, s.last_synced_at,
                   count(o.ts) AS n, min(o.ts) AS first, max(o.ts) AS last
            FROM series s LEFT JOIN observations o USING (source, series_id)
            GROUP BY s.source, s.series_id
            ORDER BY s.source, s.series_id
            """
        ).fetchall()
    table = Table()
    for name in ("series", "freq", "points", "range", "synced"):
        table.add_column(name, no_wrap=True)
    # The title gets whatever width is left (up to 48), truncated rather than wrapped.
    title_width = min(48, max(12, console.width - 80))  # ~80 cols for the fixed columns
    table.add_column("title", no_wrap=True, overflow="ellipsis", max_width=title_width)
    for r in rows:
        span = f"{r['first']:%Y-%m}–{r['last']:%Y-%m}" if r["first"] else "—"
        table.add_row(
            f"{r['source']}:{r['series_id']}",
            r["frequency"] or "",
            str(r["n"]),
            span,
            _fmt(r["last_synced_at"]),
            r["title"] or "",
        )
    console.print(table)


@series_app.command("show")
def series_show(
    series_id: str,
    source: str = typer.Option("fred"),
    last: int = typer.Option(24, help="How many recent points to print"),
):
    """Print recent observations plus a sparkline of the whole series."""
    with _connect() as conn:
        meta = conn.execute(
            "SELECT * FROM series WHERE source = %s AND series_id = %s", (source, series_id)
        ).fetchone()
        if not meta:
            console.print(f"[red]{source}:{series_id} not in database.[/red] Run `tempus sync`.")
            raise typer.Exit(1)
        values = [
            r["value"]
            for r in conn.execute(
                """
                SELECT value FROM observations
                WHERE source = %s AND series_id = %s AND value IS NOT NULL ORDER BY ts
                """,
                (source, series_id),
            )
        ]
        recent = conn.execute(
            """
            SELECT ts, value FROM observations WHERE source = %s AND series_id = %s
            ORDER BY ts DESC LIMIT %s
            """,
            (source, series_id, last),
        ).fetchall()

    console.print(f"[bold]{meta['title']}[/bold]  [dim]{meta['units']} · {meta['frequency']}[/dim]")
    # Downsample to terminal width for the sparkline
    width = max(10, console.width - 2)
    step = max(1, -(-len(values) // width))  # ceil, so the line never wraps
    console.print(_sparkline(values[::step]))
    table = Table("date", "value")
    for r in reversed(recent):
        table.add_row(f"{r['ts']:%Y-%m-%d}", _fmt(r["value"]))
    console.print(table)


@series_app.command("delete")
def series_delete(series_id: str, source: str = typer.Option("fred")):
    """Delete a series and all its observations from the database."""
    with _connect() as conn:
        n = conn.execute(
            "DELETE FROM series WHERE source = %s AND series_id = %s", (source, series_id)
        ).rowcount
    console.print(f"deleted {n} series")


@app.command()
def runs(limit: int = typer.Option(20)):
    """Recent sync runs."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT %s", (limit,)
        ).fetchall()
    table = Table("started", "series", "status", "+", "-", "error")
    for r in rows:
        table.add_row(
            _fmt(r["started_at"]),
            f"{r['source']}:{r['series_id']}",
            r["status"],
            _fmt(r["rows_upserted"]),
            _fmt(r["rows_deleted"]),
            (r["error"] or "")[:60],
        )
    console.print(table)


# --- fred ------------------------------------------------------------------


@fred_app.command("search")
def fred_search(text: str, limit: int = typer.Option(20)):
    """Search FRED by keyword, most popular first."""
    results = _source("fred", get_settings()).search(text, limit=limit)
    table = Table("id", "title", "freq", "units", "pop")
    for m in results:
        table.add_row(
            m.series_id,
            (m.title or "")[:60],
            m.extra.get("frequency_short", ""),
            (m.units or "")[:30],
            str(m.extra.get("popularity", "")),
        )
    console.print(table)
    console.print("[dim]Track one with: tempus catalog add fred <ID> --sync[/dim]")

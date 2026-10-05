import re
import time
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from tempus import catalog, db, ontology
from tempus.config import get_settings
from tempus.filing import file_series
from tempus.jev import JevClient, JevError
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
ontology_app = typer.Typer(
    help="The concept tree (ontology/concepts.yaml) and where series sit in it.",
    no_args_is_help=True,
)
app.add_typer(db_app, name="db")
app.add_typer(catalog_app, name="catalog")
app.add_typer(series_app, name="series")
app.add_typer(fred_app, name="fred")
app.add_typer(ontology_app, name="ontology")
card_app = typer.Typer(help="Cards: saved views over series (cards/*.yaml).", no_args_is_help=True)
app.add_typer(card_app, name="card")

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


def _fmt_p(p: float | None) -> str:
    return "—" if p is None else f"{p:.2f}"


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
        if catalog.add(settings.catalog_path, source, series_id, meta.title):
            console.print(f"[green]added[/green] {source}:{series_id}  [dim]{meta.title}[/dim]")
        else:
            console.print(f"[yellow]already present[/yellow] {source}:{series_id}")
        if sync:
            with _connect() as conn:
                _print_result(sync_series(conn, src, series_id))


@catalog_app.command("import-snapshot")
def catalog_import_snapshot(
    path: Path = typer.Argument(..., help="e.g. data/fred/series-snapshot.json"),
    source: str = typer.Option("fred"),
):
    """Load series metadata from a snapshot file into the database (no observations).

    Browsable in `tempus serve` right away; catalog.toml is unchanged. Run
    `tempus ontology load` next to place them using ontology/placements.draft.yaml.
    """
    from tempus.snapshot import SnapshotError, import_snapshot

    with _connect() as conn:
        try:
            inserted, refreshed = import_snapshot(conn, path, source)
        except (SnapshotError, OSError, ValueError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    console.print(
        f"[green]{inserted} series added[/green], {refreshed} refreshed (synced ones kept)"
    )


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
            _load_ontology(conn, settings, quiet=True)

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


# --- ontology --------------------------------------------------------------


def _load_ontology(conn, settings, *, quiet: bool = False) -> list[ontology.Concept]:
    """Validate the YAML tree and catalog pins, then mirror both into the database."""
    try:
        roots = ontology.load(settings.ontology_path)
    except (ontology.OntologyError, OSError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    known = {c.id for c in ontology.walk(roots)}
    pins = catalog.pins(catalog.load(settings.catalog_path))
    bad = {f"{s}:{i} → {c}" for (s, i), c in pins.items() if c not in known}
    if bad:
        console.print(f"[red]catalog.toml pins to unknown concepts:[/red] {', '.join(sorted(bad))}")
        raise typer.Exit(1)
    try:
        drafts = ontology.load_drafts(settings.drafts_path)
    except (ontology.OntologyError, OSError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    stale = sorted({d.concept for d in drafts.values() if d.concept not in known})
    upserted, removed = ontology.sync_to_db(conn, roots)
    pinned = ontology.apply_pins(conn, pins)
    drafted = ontology.apply_drafts(conn, drafts)
    if not quiet:
        console.print(
            f"[green]{upserted} concepts loaded[/green] ({removed} removed), {pinned} pins, "
            f"{drafted} draft placements"
        )
        if stale:
            console.print(
                f"[yellow]{settings.drafts_path} names unknown concepts (skipped):[/yellow] "
                + ", ".join(stale)
            )
    return roots


@ontology_app.command("load")
def ontology_load():
    """Validate ontology/concepts.yaml, catalog pins and draft placements, and load them.

    Draft placements (ontology/placements.draft.yaml) fill in series that have no pin and
    no Jev decision yet; `tempus ontology file` replaces them with real filing.
    """
    with _connect() as conn:
        _load_ontology(conn, get_settings())


@ontology_app.command("file")
def ontology_file(
    refile: bool = typer.Option(False, help="Re-decide existing auto placements too"),
):
    """Place unfiled and draft-placed series in the tree with Jev. Pins are never changed."""
    settings = get_settings()
    try:
        jev = JevClient(settings.typesafe_api_key)
    except JevError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    with _connect() as conn:
        roots = _load_ontology(conn, settings, quiet=True)
        n = 0
        for source, series_id, p in file_series(conn, roots, jev, refile=refile):
            n += 1
            where = p.concept_id or "[yellow]no fit — needs a new branch?[/yellow]"
            conf = f"[dim]{p.confidence:.2f}[/dim]" if p.confidence is not None else ""
            console.print(f"  {source}:{series_id} → {where} {conf}")
    if not n:
        console.print("[dim]Nothing to file.[/dim]")


@ontology_app.command("tree")
def ontology_tree(
    series: bool = typer.Option(True, help="List the series filed under each concept"),
):
    """Show the concept tree with the series filed in it."""
    from rich.tree import Tree

    with _connect() as conn:
        concepts = conn.execute(
            "SELECT id, parent_id, name FROM concepts ORDER BY position"
        ).fetchall()
        placed = conn.execute(
            """
            SELECT sc.concept_id, sc.method, sc.confidence, s.source, s.series_id, s.title
            FROM series_concepts sc JOIN series s USING (source, series_id)
            ORDER BY s.series_id
            """
        ).fetchall()
        unplaced = conn.execute(
            """
            SELECT s.source, s.series_id, s.title FROM series s
            LEFT JOIN series_concepts sc USING (source, series_id)
            WHERE sc.series_id IS NULL ORDER BY s.series_id
            """
        ).fetchall()
    if not concepts:
        console.print("[yellow]No concepts loaded.[/yellow] Run `tempus ontology load`.")
        raise typer.Exit(1)

    by_concept: dict[str | None, list] = {}
    for r in placed:
        by_concept.setdefault(r["concept_id"], []).append(r)
    children: dict[str | None, list] = {}
    for c in concepts:
        children.setdefault(c["parent_id"], []).append(c)

    def count(concept_id: str) -> int:
        return len(by_concept.get(concept_id, [])) + sum(
            count(c["id"]) for c in children.get(concept_id, [])
        )

    def label(r) -> str:
        how = {"manual": "📌", "draft": "draft"}.get(r["method"]) or _fmt_p(r["confidence"])
        return f"[cyan]{r['series_id']}[/cyan] [dim]{(r['title'] or '')[:60]} · {how}[/dim]"

    def add(node: Tree, parent_id: str | None) -> None:
        for c in children.get(parent_id, []):
            n = count(c["id"])
            style = "bold" if n else "dim"
            branch = node.add(f"[{style}]{c['name']}[/{style}] [dim]{c['id']} ({n})[/dim]")
            if series:
                for r in by_concept.get(c["id"], []):
                    branch.add(label(r))
            add(branch, c["id"])

    root = Tree("[bold]tempus ontology[/bold]")
    add(root, None)
    if by_concept.get(None):
        misfit = root.add("[yellow]No fit (consider a new branch)[/yellow]")
        for r in by_concept[None]:
            misfit.add(label(r))
    if unplaced:
        pending = root.add("[yellow]Not filed yet[/yellow] [dim](tempus ontology file)[/dim]")
        for r in unplaced:
            pending.add(f"[cyan]{r['series_id']}[/cyan] [dim]{(r['title'] or '')[:60]}[/dim]")
    console.print(root)


@app.command()
def resolve(
    phrase: str = typer.Argument(..., help='e.g. "crude oil price"'),
    limit: int = typer.Option(10, help="How many series to list"),
    lexical: bool = typer.Option(False, help="Use the lexical fallback even with a key"),
):
    """Find the concepts and series a phrase means, by walking the tree (Jevbox-style)."""
    from tempus.explore import catalog_from_db
    from tempus.navigate import LexicalChooser, navigate

    settings = get_settings()
    with _connect() as conn:
        catalog = catalog_from_db(conn)
    if not catalog.roots:
        console.print("[yellow]No concepts loaded.[/yellow] Run `tempus ontology load`.")
        raise typer.Exit(1)
    if settings.typesafe_api_key and not lexical:
        chooser, label = JevClient(settings.typesafe_api_key), "jev"
    else:
        chooser, label = LexicalChooser(catalog), "lexical fallback"
    try:
        result = navigate(phrase, catalog, chooser, label=label)
    except JevError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[dim]chooser: {result.chooser}[/dim]")
    if not result.paths:
        console.print("[yellow]Nothing in the tree fits that.[/yellow]")
        raise typer.Exit(1)
    for path in result.paths:
        trail = " › ".join(
            f"{name} [dim]{step['p']:.2f}[/dim]"
            for name, step in zip(path.to_dict(catalog)["names"], path.steps, strict=True)
        )
        console.print(f"[bold]{path.score:.2f}[/bold]  {trail}")
    if result.ambiguous:
        console.print("[yellow]Ambiguous:[/yellow] the top routes are close; check both.")
    table = Table("series", "p", "title")
    for s in result.series[:limit]:
        table.add_row(f"{s['source']}:{s['series_id']}", f"{s['p']:.2f}", s["title"] or "")
    console.print(table)


CARD_TEMPLATE = """\
version: 1
title: {title}
question: What are you trying to find out?
# notes: |
#   Findings so far.
tags: []
concepts: []                      # e.g. [prices.consumer.energy]
window: {{start: 2000-01-01}}

inputs:
  x: fred:UNRATE                  # source:series_id, or {{concept: labor}}

formulas:
  x_yoy: diff(x, 12)              # see `tempus card functions`

panels:                           # see `tempus card panels`
  - type: timeseries
    y: [x, x_yoy]
"""


def _cards():
    from tempus.cards.repository import FileCardRepository

    return FileCardRepository(get_settings().cards_path)


def _print_card_errors(errors) -> None:
    from rich.markup import escape

    for e in errors:
        color = "red" if e.level == "error" else "yellow"
        console.print(f"[{color}]{escape(e.format())}[/{color}]")  # paths contain [0]


@card_app.command("list")
def card_list():
    """List cards with their title, panel count, concepts and tags."""
    repo = _cards()
    table = Table("id", "title", "panels", "concepts", "tags")
    for card_id in repo.ids():
        card, errors = repo.get(card_id)
        if card is None:
            table.add_row(card_id, "[red]invalid[/red]", "", "", "")
            continue
        table.add_row(
            card_id,
            card.title,
            str(len(card.panels)),
            ", ".join(card.concepts),
            ", ".join(card.tags),
        )
    console.print(table)


@card_app.command("new")
def card_new(card_id: str):
    """Write a commented template to cards/<id>.yaml."""
    from tempus.cards.spec import CARD_ID

    if not CARD_ID.fullmatch(card_id):
        console.print("[red]Card ids are kebab-case, e.g. crude-to-gasoline[/red]")
        raise typer.Exit(1)
    path = _cards().path(card_id)
    if path.exists():
        console.print(f"[red]{path} already exists[/red]")
        raise typer.Exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CARD_TEMPLATE.format(title=card_id.replace("-", " ").capitalize()))
    console.print(f"Wrote [bold]{path}[/bold]")


@card_app.command("check")
def card_check(
    card_id: str | None = typer.Argument(None, help="One card; all cards if omitted"),
    offline: bool = typer.Option(False, help="Static checks only, no database"),
):
    """Validate cards. Exits 1 if any card has errors."""
    from tempus.cards.check import check_card
    from tempus.cards.errors import has_errors
    from tempus.cards.resolve import resolve

    repo = _cards()
    ids = [card_id] if card_id else repo.ids()
    conn = None if offline else _connect()
    failed = False
    for cid in ids:
        card, errors = repo.get(cid)
        if card is not None:
            errors += check_card(cid, card)[1]
            if conn is not None and not has_errors(errors):
                errors += resolve(conn, cid, card)[1]
        _print_card_errors(errors)
        if has_errors(errors):
            failed = True
        else:
            console.print(f"[green]✓[/green] {cid}")
    if conn is not None:
        conn.close()
    if failed:
        raise typer.Exit(1)


@card_app.command("show")
def card_show(
    card_id: str,
    start: datetime | None = typer.Option(None, formats=["%Y-%m-%d"]),
    end: datetime | None = typer.Option(None, formats=["%Y-%m-%d"]),
    as_json: bool = typer.Option(False, "--json", help="Print the panel payloads"),
):
    """Evaluate a card. Prints a summary, or the panel payloads with --json."""
    import json

    from tempus.cards.errors import has_errors
    from tempus.cards.run import run_card

    with _connect() as conn:
        result = run_card(conn, _cards(), card_id, start and start.date(), end and end.date())
    if as_json:
        print(json.dumps(result.to_dict(), default=str, indent=2))
        raise typer.Exit(1 if has_errors(result.errors) else 0)
    _print_card_errors(result.errors)
    for p in result.payloads:
        console.print(f"\n[bold]{p['title'] or p['type']}[/bold]")
        if p["type"] == "lagcorr" and p["annotations"]:
            peak = p["annotations"]["peak"]
            console.print(f"  {p['annotations']['lead']} (r = {peak['r']:.3f})")
        elif p["type"] == "stat" and p["stat"]:
            console.print(f"  {p['stat']['value']:.4g} on {p['stat']['date']}")
        for s in p["series"]:
            vals = [v for _, v in s["points"] if v is not None]
            if vals:
                console.print(
                    f"  {s['label']:<40} {_sparkline(vals[-40:])} "
                    f"{vals[-1]:.4g} ({s['points'][-1][0]})"
                )
    if has_errors(result.errors):
        raise typer.Exit(1)


@card_app.command("load")
def card_load():
    """Sync cards/*.yaml into the cards index table."""
    from tempus.cards.repository import sync_index

    with _connect() as conn:
        loaded, errors = sync_index(conn, _cards())
    _print_card_errors(errors)
    console.print(f"Loaded {loaded} card(s)")


@card_app.command("functions")
def card_functions(name: str | None = typer.Argument(None)):
    """The function catalog, or one entry with its caveat."""
    from tempus.cards.functions import CATALOG, signature

    if name:
        fn = CATALOG.get(name)
        if fn is None:
            console.print(f"[red]Unknown function '{name}'[/red]")
            raise typer.Exit(1)
        console.print(
            f"[bold]{signature(fn)}[/bold] → {fn.returns}\n{fn.doc}\n"
            f"[yellow]Caveat:[/yellow] {fn.caveat}"
        )
        return
    table = Table("function", "returns", "what it does")
    for fn in CATALOG.values():
        table.add_row(signature(fn), fn.returns, fn.doc)
    console.print(table)


@card_app.command("panels")
def card_panels(name: str | None = typer.Argument(None)):
    """The panel catalog."""
    from tempus.cards.panels import PANELS, accepts_text

    for p in PANELS.values():
        if name and p.name != name:
            continue
        console.print(
            f"[bold]{p.name}[/bold] ({p.field}: {accepts_text(p)})\n  {p.doc}\n"
            f"  [yellow]Not for:[/yellow] {p.when_not}"
        )


@app.command()
def serve(
    port: int = typer.Option(8765),
    host: str = typer.Option("127.0.0.1"),
    share: bool = typer.Option(
        False, help="Guest app for Tailscale Funnel: read-only, behind TEMPUS_SHARE_PASSWORD"
    ),
    until: float | None = typer.Option(None, help="With --share: stop serving at this epoch"),
):
    """Run the tempus app: browse concepts and cards, with live reload on save."""
    import uvicorn

    from tempus.app import create_app

    if not share:
        console.print(f"tempus → [bold]http://{host}:{port}[/bold]")
        uvicorn.run(create_app(), host=host, port=port, log_level="warning")
        return

    from tempus.share import ShareGate

    settings = get_settings()
    if host != "127.0.0.1":
        raise typer.BadParameter("--share binds 127.0.0.1 only; Funnel does the publishing")
    if not settings.share_password:
        console.print("[red]Set TEMPUS_SHARE_PASSWORD in .env (or run `mise run share:on`).[/red]")
        raise typer.Exit(1)
    try:
        app_ = ShareGate(create_app(settings, guest=True), settings.share_password, until)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"tempus (shared, password-gated) → [bold]http://{host}:{port}[/bold]")
    uvicorn.run(app_, host=host, port=port, log_level="warning", proxy_headers=False)

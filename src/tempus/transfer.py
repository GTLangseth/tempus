"""Copy data between the dev database and prod (the home base): `tempus db pull` / `push`.

Spec: docs/specs/2026-10-11-dev-prod-db-sync-design.md. Every function takes two open
connections, so tests run them between two local databases and the CLI supplies the tunnel.
Only `auto` placements (Jev filings) move: pins and drafts come from git on each side.
When SPA-16 adds `jev_decisions`, it joins pull and push as an insert-only table.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import psycopg
from psycopg import sql

Key = tuple[str, str]  # (source, series_id)
RANK = {"draft": 0, "auto": 1, "manual": 2}
TRUE = sql.SQL("TRUE")

# Columns each table must have on both sides for a copy to mean anything.
REQUIRED = {
    "series": ("source", "series_id", "upstream_updated_at", "last_synced_at"),
    "observations": ("source", "series_id", "ts", "value"),
    "series_concepts": ("source", "series_id", "concept_id", "method", "filed_at"),
}


class TransferError(RuntimeError):
    """A pull or push that can't go ahead. The message is one line, ready for the CLI."""


class SchemaMismatch(TransferError):
    pass


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position
        """,
        (table,),
    )
    return [r["column_name"] for r in rows]


def shared_columns(dev: psycopg.Connection, prod: psycopg.Connection, table: str) -> list[str]:
    """`table`'s columns present on both sides, in dev's order."""
    dev_cols, prod_cols = _columns(dev, table), _columns(prod, table)
    for side, cols in (("dev", dev_cols), ("prod", prod_cols)):
        missing = [c for c in REQUIRED[table] if c not in cols]
        if missing:
            raise SchemaMismatch(
                f"{table} on {side} lacks {', '.join(missing)}: run `tempus db init` on {side}"
            )
    return [c for c in dev_cols if c in prod_cols]


def _where(keys: list[Key] | None) -> tuple[sql.Composable, tuple]:
    if keys is None:
        return TRUE, ()
    return sql.SQL("(source || ':' || series_id) = ANY(%s)"), ([f"{s}:{i}" for s, i in keys],)


def copy_rows(
    src: psycopg.Connection,
    dst: psycopg.Connection,
    table: str,
    cols: list[str],
    where: sql.Composable = TRUE,
    params: tuple = (),
    *,
    into: str | None = None,
) -> int:
    """Stream `table`'s rows matching `where` from src into dst (or its table `into`).

    The SELECT form matters: plain `COPY observations TO` on a hypertable returns no rows.
    Binary when both servers share a major version, else text.
    """
    same_major = src.info.server_version // 10000 == dst.info.server_version // 10000
    fmt = sql.SQL("BINARY" if same_major else "TEXT")
    col_list = sql.SQL(", ").join(map(sql.Identifier, cols))
    out = sql.SQL("COPY (SELECT {c} FROM {t} WHERE {w}) TO STDOUT (FORMAT {f})").format(
        c=col_list, t=sql.Identifier(table), w=where, f=fmt
    )
    inn = sql.SQL("COPY {t} ({c}) FROM STDIN (FORMAT {f})").format(
        t=sql.Identifier(into or table), c=col_list, f=fmt
    )
    with src.cursor() as sc, dst.cursor() as dc:
        with sc.copy(out, params) as reader, dc.copy(inn) as writer:
            for block in reader:
                writer.write(block)
        return dc.rowcount


def select_keys(
    conn: psycopg.Connection, series: list[Key] | None = None, concept: str | None = None
) -> list[Key] | None:
    """The series a pull or push covers: `series` plus everything placed at or under
    `concept` (by `conn`'s placements). None means all."""
    if series is None and concept is None:
        return None
    keys = set(series or [])
    if concept:
        rows = conn.execute(
            "SELECT source, series_id FROM series_concepts"
            " WHERE concept_id = %(c)s OR starts_with(concept_id, %(under)s)",
            {"c": concept, "under": concept + "."},
        )
        keys |= {(r["source"], r["series_id"]) for r in rows}
    return sorted(keys)


@dataclass(frozen=True)
class PushEntry:
    key: Key
    data: Literal["new", "newer"] | None  # None: only the placement changes
    placement: bool
    observations: int = 0


@dataclass
class PushPlan:
    entries: list[PushEntry] = field(default_factory=list)
    prod_ahead: list[Key] = field(default_factory=list)  # prod has a later revision: pull
    skipped_placements: list[Key] = field(default_factory=list)  # concept not in prod's tree

    def __bool__(self) -> bool:
        return bool(self.entries)


def _series_state(conn: psycopg.Connection, keys: list[Key] | None, *, synced_only: bool):
    w, p = _where(keys)
    q = sql.SQL(
        "SELECT source, series_id, upstream_updated_at, last_synced_at FROM series WHERE {w}"
    ).format(w=w)
    if synced_only:
        q += sql.SQL(" AND last_synced_at IS NOT NULL")
    return {(r["source"], r["series_id"]): r for r in conn.execute(q, p)}


def _placements(conn: psycopg.Connection, keys: list[Key] | None, methods: tuple[str, ...]):
    w, p = _where(keys)
    q = sql.SQL(
        "SELECT source, series_id, concept_id, method, filed_at FROM series_concepts"
        " WHERE method = ANY(%s) AND {w}"
    ).format(w=w)
    return {(r["source"], r["series_id"]): r for r in conn.execute(q, (list(methods), *p))}


def _observation_counts(conn: psycopg.Connection, keys: list[Key]) -> dict[Key, int]:
    if not keys:
        return {}
    w, p = _where(keys)
    q = sql.SQL(
        "SELECT source, series_id, count(*) AS n FROM observations WHERE {w} GROUP BY 1, 2"
    ).format(w=w)
    return {(r["source"], r["series_id"]): r["n"] for r in conn.execute(q, p)}


def _data_reason(dev_row: dict, prod_row: dict | None) -> str:
    """new | newer (push) or same | ahead (don't). The provider's revision time decides;
    sources that don't report one fall back to when each side synced."""
    if prod_row is None or prod_row["last_synced_at"] is None:
        return "new"
    d, p = dev_row["upstream_updated_at"], prod_row["upstream_updated_at"]
    if d is None or p is None:
        d, p = dev_row["last_synced_at"], prod_row["last_synced_at"]
    if d > p:
        return "newer"
    return "same" if d == p else "ahead"


def _wins(mine: dict, theirs: dict | None) -> bool:
    """manual > auto > draft; between equal methods, the later filing."""
    if theirs is None:
        return True
    if RANK[mine["method"]] != RANK[theirs["method"]]:
        return RANK[mine["method"]] > RANK[theirs["method"]]
    return mine["filed_at"] > theirs["filed_at"]


def plan_push(
    dev: psycopg.Connection, prod: psycopg.Connection, keys: list[Key] | None = None
) -> PushPlan:
    """What `tempus db push` would send: whole series dev synced more recently, and Jev
    filings that outrank prod's placements. Reads only."""
    plan = PushPlan()
    dev_series = _series_state(dev, keys, synced_only=True)
    dev_placed = _placements(dev, keys, ("auto",))
    wanted = sorted(set(dev_series) | set(dev_placed))
    prod_series = _series_state(prod, wanted, synced_only=False)

    data: dict[Key, str] = {}
    for key, row in dev_series.items():
        reason = _data_reason(row, prod_series.get(key))
        if reason in ("new", "newer"):
            data[key] = reason
        elif reason == "ahead":
            plan.prod_ahead.append(key)

    prod_placed = _placements(prod, sorted(dev_placed), tuple(RANK))
    prod_concepts = {r["id"] for r in prod.execute("SELECT id FROM concepts")}
    placed: set[Key] = set()
    for key, row in dev_placed.items():
        if key not in prod_series and key not in data:
            continue  # the series isn't on prod and isn't being pushed
        if not _wins(row, prod_placed.get(key)):
            continue
        if row["concept_id"] is not None and row["concept_id"] not in prod_concepts:
            plan.skipped_placements.append(key)
            continue
        placed.add(key)

    counts = _observation_counts(dev, sorted(data))
    for key in sorted(set(data) | placed):
        plan.entries.append(PushEntry(key, data.get(key), key in placed, counts.get(key, 0)))
    plan.prod_ahead.sort()
    plan.skipped_placements.sort()
    return plan


class UnpushedWork(TransferError):
    def __init__(self, plan: PushPlan):
        self.plan = plan
        super().__init__(
            f"dev holds {len(plan.entries)} series or filings prod doesn't: "
            "push them first (tempus db push --yes), or pull with --discard-local"
        )


@dataclass(frozen=True)
class PullResult:
    rows: dict[str, int]
    skipped_placements: int  # prod filings into concepts this branch's tree lacks
    missing: list[Key]  # asked for, but not on prod: dev's copy kept


def _nothing(conn: psycopg.Connection) -> None:
    pass


def _set_clause(cols: list[str]) -> sql.Composable:
    return sql.SQL(", ").join(
        sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
        for c in cols
        if c not in ("source", "series_id")
    )


def pull(
    dev: psycopg.Connection,
    prod: psycopg.Connection,
    keys: list[Key] | None = None,
    *,
    metadata_only: bool = False,
    discard_local: bool = False,
    prepare: Callable[[psycopg.Connection], None] = _nothing,
) -> PullResult:
    """Replace dev's copy of the selected series with prod's, in one dev transaction.

    `prepare(dev)` rebuilds the git-derived tables (concepts, pins, drafts, cards) once the
    series are in and before prod's filings merge in, so filings land only under concepts
    this branch's tree has.
    """
    missing: list[Key] = []
    if keys is not None:
        on_prod = set(_series_state(prod, keys, synced_only=False))
        missing = [k for k in keys if k not in on_prod]
        keys = [k for k in keys if k in on_prod]
    if not discard_local:
        plan = plan_push(dev, prod, keys)
        if plan:
            raise UnpushedWork(plan)
    series_cols = shared_columns(dev, prod, "series")
    obs_cols = shared_columns(dev, prod, "observations")
    placement_cols = shared_columns(dev, prod, "series_concepts")
    w, p = _where(keys)
    rows: dict[str, int] = {}
    with dev.transaction():
        if keys is None:
            dev.execute("TRUNCATE observations, series_concepts, series")
        else:
            dev.execute(sql.SQL("DELETE FROM series WHERE {w}").format(w=w), p)  # cascades
        rows["series"] = copy_rows(prod, dev, "series", series_cols, w, p)
        if not metadata_only:
            rows["observations"] = copy_rows(prod, dev, "observations", obs_cols, w, p)
        prepare(dev)
        dev.execute(
            "CREATE TEMP TABLE _pull_placements (LIKE series_concepts INCLUDING DEFAULTS)"
            " ON COMMIT DROP"
        )
        auto = sql.SQL("method = 'auto' AND {w}").format(w=w)
        copy_rows(prod, dev, "series_concepts", placement_cols, auto, p, into="_pull_placements")
        cols = sql.SQL(", ").join(map(sql.Identifier, placement_cols))
        rows["series_concepts"] = dev.execute(
            sql.SQL(
                """
                INSERT INTO series_concepts ({cols}) SELECT {cols} FROM _pull_placements t
                WHERE (t.concept_id IS NULL
                       OR EXISTS (SELECT 1 FROM concepts c WHERE c.id = t.concept_id))
                  AND EXISTS (SELECT 1 FROM series s
                              WHERE s.source = t.source AND s.series_id = t.series_id)
                ON CONFLICT (source, series_id) DO UPDATE SET {sets}
                WHERE series_concepts.method <> 'manual'
                """
            ).format(cols=cols, sets=_set_clause(placement_cols))
        ).rowcount
        skipped = dev.execute(
            """
            SELECT count(*) AS n FROM _pull_placements t
            WHERE t.concept_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM concepts c WHERE c.id = t.concept_id)
            """
        ).fetchone()["n"]
    return PullResult(rows, skipped, missing)


@dataclass(frozen=True)
class PushResult:
    series: int
    observations: int
    placements: int


def apply_push(dev: psycopg.Connection, prod: psycopg.Connection, plan: PushPlan) -> PushResult:
    """Apply `plan` to prod in one transaction. Each series in it is replaced whole (its row
    and all its observations); nothing else in prod is deleted. Pushed series log a
    `sync_runs` row with status `pushed`."""
    data_keys = [e.key for e in plan.entries if e.data]
    placement_keys = [e.key for e in plan.entries if e.placement]
    series_cols = shared_columns(dev, prod, "series")
    obs_cols = shared_columns(dev, prod, "observations")
    placement_cols = shared_columns(dev, prod, "series_concepts")
    observations = 0
    with prod.transaction():
        w, p = _where([e.key for e in plan.entries])
        # A scheduled sync on the mini touching these series waits for this transaction.
        prod.execute(sql.SQL("SELECT 1 FROM series WHERE {w} FOR UPDATE").format(w=w), p)
        if data_keys:
            w, p = _where(data_keys)
            cols = sql.SQL(", ").join(map(sql.Identifier, series_cols))
            prod.execute(
                "CREATE TEMP TABLE _push_series (LIKE series INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            copy_rows(dev, prod, "series", series_cols, w, p, into="_push_series")
            prod.execute(
                sql.SQL(
                    "INSERT INTO series ({cols}) SELECT {cols} FROM _push_series"
                    " ON CONFLICT (source, series_id) DO UPDATE SET {sets}"
                ).format(cols=cols, sets=_set_clause(series_cols))
            )
            prod.execute(sql.SQL("DELETE FROM observations WHERE {w}").format(w=w), p)
            observations = copy_rows(dev, prod, "observations", obs_cols, w, p)
            counts = _observation_counts(prod, data_keys)
            with prod.cursor() as cur:
                cur.executemany(
                    "INSERT INTO sync_runs (source, series_id, finished_at, status, rows_upserted)"
                    " VALUES (%s, %s, now(), 'pushed', %s)",
                    [(s, i, counts.get((s, i), 0)) for s, i in data_keys],
                )
        if placement_keys:
            w, p = _where(placement_keys)
            cols = sql.SQL(", ").join(map(sql.Identifier, placement_cols))
            prod.execute(
                "CREATE TEMP TABLE _push_placements"
                " (LIKE series_concepts INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            copy_rows(dev, prod, "series_concepts", placement_cols, w, p, into="_push_placements")
            prod.execute(
                sql.SQL(
                    "INSERT INTO series_concepts ({cols}) SELECT {cols} FROM _push_placements"
                    " ON CONFLICT (source, series_id) DO UPDATE SET {sets}"
                ).format(cols=cols, sets=_set_clause(placement_cols))
            )
    return PushResult(len(data_keys), observations, len(placement_keys))

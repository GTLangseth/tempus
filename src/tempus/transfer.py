"""Copy data between the dev database and prod (the home base): `tempus db pull` / `push`.

Spec: docs/specs/2026-10-11-dev-prod-db-sync-design.md. Every function takes two open
connections, so tests run them between two local databases and the CLI supplies the tunnel.
Only `auto` placements (Jev filings) move: pins and drafts come from git on each side.
When SPA-16 adds `jev_decisions`, it joins pull and push as an insert-only table.
"""

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

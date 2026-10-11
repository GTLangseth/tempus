# tests/test_transfer.py
from datetime import UTC, datetime, timedelta

import pytest

from tempus import transfer
from tempus.transfer import SchemaMismatch, copy_rows, plan_push, select_keys, shared_columns

T0 = datetime(2026, 10, 1, tzinfo=UTC)
LATER = T0 + timedelta(days=1)


def add_series(conn, sid, *, upstream=T0, synced=T0, points=3, values_from=0.0):
    """A series with `points` observations, one per 6 years so they span several chunks."""
    conn.execute(
        """
        INSERT INTO series (source, series_id, title, upstream_updated_at, last_synced_at)
        VALUES ('fred', %s, %s, %s, %s)
        ON CONFLICT (source, series_id) DO UPDATE SET
            upstream_updated_at = EXCLUDED.upstream_updated_at,
            last_synced_at = EXCLUDED.last_synced_at
        """,
        (sid, sid, upstream, synced),
    )
    conn.execute("DELETE FROM observations WHERE series_id = %s", (sid,))
    for k in range(points):
        conn.execute(
            "INSERT INTO observations VALUES ('fred', %s, %s, %s)",
            (sid, datetime(2000 + 6 * k, 1, 1, tzinfo=UTC), values_from + k),
        )


def add_concept(conn, cid):
    parent = cid.rsplit(".", 1)[0] if "." in cid else None
    conn.execute(
        "INSERT INTO concepts (id, parent_id, name, description, position)"
        " VALUES (%s, %s, %s, '', 0) ON CONFLICT DO NOTHING",
        (cid, parent, cid),
    )


def place(conn, sid, concept, method="auto", filed=T0):
    conn.execute(
        """
        INSERT INTO series_concepts (source, series_id, concept_id, method, filed_at)
        VALUES ('fred', %s, %s, %s, %s)
        ON CONFLICT (source, series_id) DO UPDATE SET
            concept_id = EXCLUDED.concept_id, method = EXCLUDED.method, filed_at = EXCLUDED.filed_at
        """,
        (sid, concept, method, filed),
    )


def obs(conn, sid):
    rows = conn.execute(
        "SELECT ts, value FROM observations WHERE series_id = %s ORDER BY ts", (sid,)
    )
    return [(r["ts"], r["value"]) for r in rows]


def test_shared_columns_lists_the_common_columns(dev_conn, prod_conn):
    cols = shared_columns(dev_conn, prod_conn, "series")
    assert cols[:2] == ["source", "series_id"] and "last_synced_at" in cols
    prod_conn.execute("ALTER TABLE series ADD COLUMN newer_feature text")
    assert "newer_feature" not in shared_columns(dev_conn, prod_conn, "series")


def test_schema_mismatch_names_the_side(dev_conn, prod_conn):
    prod_conn.execute("ALTER TABLE series DROP COLUMN upstream_updated_at")
    with pytest.raises(SchemaMismatch, match="run `tempus db init` on prod"):
        shared_columns(dev_conn, prod_conn, "series")


def test_copy_rows_reads_every_hypertable_chunk(dev_conn, prod_conn):
    add_series(prod_conn, "A", points=4)
    copy_rows(prod_conn, dev_conn, "series", shared_columns(dev_conn, prod_conn, "series"))
    n = copy_rows(
        prod_conn, dev_conn, "observations", shared_columns(dev_conn, prod_conn, "observations")
    )
    assert n == 4 and obs(dev_conn, "A") == obs(prod_conn, "A")


def test_select_keys(prod_conn):
    for cid in ("labor", "labor.unemployment", "laboratory"):
        add_concept(prod_conn, cid)
    for sid, cid in (("U", "labor.unemployment"), ("L", "labor"), ("X", "laboratory")):
        add_series(prod_conn, sid, points=0)
        place(prod_conn, sid, cid)
    assert select_keys(prod_conn) is None
    assert select_keys(prod_conn, concept="labor") == [("fred", "L"), ("fred", "U")]
    assert select_keys(prod_conn, series=[("fred", "X")], concept="labor.unemployment") == [
        ("fred", "U"),
        ("fred", "X"),
    ]


@pytest.mark.parametrize(
    "prod_state, expected",
    [
        (None, "new"),  # prod has no row
        ({"upstream": None, "synced": None}, "new"),  # snapshot-only on prod
        ({"upstream": T0 - timedelta(days=1), "synced": T0}, "newer"),
        ({"upstream": T0, "synced": T0}, "same"),
        ({"upstream": LATER, "synced": LATER}, "ahead"),
    ],
)
def test_push_plan_for_series_data(dev_conn, prod_conn, prod_state, expected):
    add_series(dev_conn, "A", upstream=T0, synced=T0)
    if prod_state:
        add_series(prod_conn, "A", upstream=prod_state["upstream"], synced=prod_state["synced"])
    plan = plan_push(dev_conn, prod_conn)
    data = {e.key: e.data for e in plan.entries}
    if expected in ("new", "newer"):
        assert data == {("fred", "A"): expected} and plan.entries[0].observations == 3
    elif expected == "same":
        assert not plan and plan.prod_ahead == []
    else:
        assert not plan and plan.prod_ahead == [("fred", "A")]


def test_push_plan_ignores_series_dev_never_synced(dev_conn, prod_conn):
    add_series(dev_conn, "A", upstream=None, synced=None, points=0)
    assert not plan_push(dev_conn, prod_conn)


def test_push_plan_falls_back_to_sync_times_without_revision_times(dev_conn, prod_conn):
    add_series(dev_conn, "A", upstream=None, synced=LATER)
    add_series(prod_conn, "A", upstream=None, synced=T0)
    assert [e.data for e in plan_push(dev_conn, prod_conn).entries] == ["newer"]


@pytest.mark.parametrize(
    "prod_placement, pushed",
    [
        (None, True),
        (("draft", T0), True),  # a Jev filing beats a draft
        (("auto", T0 - timedelta(days=1)), True),  # a newer filing wins
        (("auto", LATER), False),  # prod filed it more recently
        (("manual", T0 - timedelta(days=1)), False),  # never over a pin
    ],
)
def test_push_plan_for_placements(dev_conn, prod_conn, prod_placement, pushed):
    for conn in (dev_conn, prod_conn):
        add_concept(conn, "labor")
        add_series(conn, "A", upstream=None, synced=None, points=0)  # same metadata both sides
    place(dev_conn, "A", "labor", "auto", T0)
    if prod_placement:
        place(prod_conn, "A", "labor", *prod_placement)
    entries = plan_push(dev_conn, prod_conn).entries
    assert [(e.key, e.data, e.placement) for e in entries] == (
        [(("fred", "A"), None, True)] if pushed else []
    )


def test_push_plan_skips_placements_into_concepts_prod_lacks(dev_conn, prod_conn):
    for conn in (dev_conn, prod_conn):
        add_concept(conn, "labor")
        add_series(conn, "A", upstream=None, synced=None, points=0)
    add_concept(dev_conn, "labor.new_branch_concept")
    place(dev_conn, "A", "labor.new_branch_concept")
    plan = plan_push(dev_conn, prod_conn)
    assert not plan and plan.skipped_placements == [("fred", "A")]


def test_push_plan_never_pushes_drafts_or_pins(dev_conn, prod_conn):
    for conn in (dev_conn, prod_conn):
        add_concept(conn, "labor")
        add_series(conn, "A", upstream=None, synced=None, points=0)
        add_series(conn, "B", upstream=None, synced=None, points=0)
    place(dev_conn, "A", "labor", "draft", LATER)
    place(dev_conn, "B", "labor", "manual", LATER)
    assert not plan_push(dev_conn, prod_conn)


def test_push_plan_narrowed_to_keys(dev_conn, prod_conn):
    add_series(dev_conn, "A")
    add_series(dev_conn, "B")
    plan = plan_push(dev_conn, prod_conn, [("fred", "B")])
    assert [e.key for e in plan.entries] == [("fred", "B")]


def test_transfer_has_no_side_effects_when_planning(dev_conn, prod_conn):
    add_series(dev_conn, "A")
    plan_push(dev_conn, prod_conn)
    assert prod_conn.execute("SELECT count(*) AS n FROM series").fetchone()["n"] == 0
    assert transfer.TransferError  # exported for the CLI

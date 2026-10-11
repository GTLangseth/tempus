# tests/test_transfer.py
from datetime import UTC, datetime, timedelta

import pytest

from tempus import transfer
from tempus.transfer import (
    SchemaMismatch,
    UnpushedWork,
    copy_rows,
    plan_push,
    pull,
    select_keys,
    shared_columns,
)

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


def series_ids(conn):
    return [r["series_id"] for r in conn.execute("SELECT series_id FROM series ORDER BY 1")]


def test_full_pull_replaces_dev(dev_conn, prod_conn):
    add_series(dev_conn, "OLD", upstream=None, synced=None)  # snapshot-only: nothing to push
    add_series(prod_conn, "A", points=4)
    add_series(prod_conn, "B", points=2)
    result = pull(dev_conn, prod_conn)
    assert series_ids(dev_conn) == ["A", "B"]
    assert obs(dev_conn, "A") == obs(prod_conn, "A")
    assert result.rows == {"series": 2, "observations": 6, "series_concepts": 0}


def test_pull_refuses_while_dev_holds_unpushed_syncs(dev_conn, prod_conn):
    add_series(prod_conn, "A", upstream=T0)
    add_series(dev_conn, "A", upstream=LATER, values_from=10)  # a backfill not pushed yet
    with pytest.raises(UnpushedWork, match="push them first") as exc:
        pull(dev_conn, prod_conn)
    assert [e.key for e in exc.value.plan.entries] == [("fred", "A")]
    assert obs(dev_conn, "A")[0][1] == 10  # dev untouched
    pull(dev_conn, prod_conn, discard_local=True)
    assert obs(dev_conn, "A")[0][1] == 0


def test_narrowed_pull_leaves_other_dev_series_alone(dev_conn, prod_conn):
    add_series(dev_conn, "KEEP", upstream=None, synced=None)
    add_series(prod_conn, "A")
    add_series(prod_conn, "B")
    pull(dev_conn, prod_conn, [("fred", "A")])
    assert series_ids(dev_conn) == ["A", "KEEP"]


def test_pull_keeps_dev_series_prod_lacks(dev_conn, prod_conn):
    add_series(dev_conn, "DEVONLY", upstream=None, synced=None, points=2)
    add_series(prod_conn, "A")
    result = pull(dev_conn, prod_conn, [("fred", "A"), ("fred", "DEVONLY")])
    assert result.missing == [("fred", "DEVONLY")]
    assert len(obs(dev_conn, "DEVONLY")) == 2


def test_metadata_only_pull_copies_no_observations(dev_conn, prod_conn):
    add_series(prod_conn, "A")
    result = pull(dev_conn, prod_conn, metadata_only=True)
    assert series_ids(dev_conn) == ["A"] and obs(dev_conn, "A") == []
    assert "observations" not in result.rows


def test_pull_merges_prod_filings_under_the_branch_tree(dev_conn, prod_conn):
    for cid in ("labor", "gone"):
        add_concept(prod_conn, cid)
    for sid, cid in (("A", "labor"), ("B", "gone"), ("C", "labor")):
        add_series(prod_conn, sid, points=0)
        place(prod_conn, sid, cid, "auto")

    def prepare(conn):  # stands in for `ontology load`: this branch's tree and a pin
        add_concept(conn, "labor")
        add_concept(conn, "pinned")
        place(conn, "C", "pinned", "manual")

    result = pull(dev_conn, prod_conn, prepare=prepare)
    rows = dev_conn.execute(
        "SELECT series_id, concept_id, method FROM series_concepts ORDER BY 1"
    ).fetchall()
    assert [(r["series_id"], r["concept_id"], r["method"]) for r in rows] == [
        ("A", "labor", "auto"),
        ("C", "pinned", "manual"),  # a pin from git beats prod's filing
    ]
    assert result.skipped_placements == 1  # B's concept isn't in this branch's tree


def test_prepare_runs_after_series_arrive(dev_conn, prod_conn):
    add_series(prod_conn, "A")
    seen = []
    pull(dev_conn, prod_conn, prepare=lambda c: seen.append(series_ids(c)))
    assert seen == [["A"]]


def test_failed_pull_leaves_dev_untouched(dev_conn, prod_conn):
    add_series(dev_conn, "OLD", upstream=None, synced=None)
    add_series(prod_conn, "A")

    def boom(conn):
        raise RuntimeError("ontology failed to load")

    with pytest.raises(RuntimeError):
        pull(dev_conn, prod_conn, prepare=boom)
    assert series_ids(dev_conn) == ["OLD"]

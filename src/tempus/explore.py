"""Reads for the explore view: the whole tree with its series, and one series' page.

A series page is an implicit single-series card (a stat panel and a timeseries panel) run
through the card engine, so it draws with the same payloads and adapters as saved cards and
never writes to cards/.
"""

from collections.abc import Callable

import psycopg

from tempus.cards.run import run_spec
from tempus.cards.spec import Card
from tempus.cards.values import freq_from_fred
from tempus.navigate import Catalog
from tempus.ontology import from_rows
from tempus.sources import Source
from tempus.sync import sync_series

SourceFactory = Callable[[str], Source | None]
NEIGHBORS = 12


def explore_payload(conn: psycopg.Connection) -> dict:
    """Concepts with direct and subtree series counts, plus every series, flat and compact."""
    concepts = conn.execute(
        """
        SELECT c.id, c.parent_id, c.name, c.description, c.position,
               count(sc.series_id) AS series
        FROM concepts c LEFT JOIN series_concepts sc ON sc.concept_id = c.id
        GROUP BY c.id ORDER BY c.position, c.id
        """
    ).fetchall()
    rows = conn.execute(
        """
        SELECT s.source, s.series_id, s.title, s.frequency, s.last_synced_at,
               (s.extra->>'popularity')::int AS popularity, sc.concept_id, sc.method
        FROM series s LEFT JOIN series_concepts sc USING (source, series_id)
        ORDER BY popularity DESC NULLS LAST, s.series_id
        """
    ).fetchall()
    total = {c["id"]: c["series"] for c in concepts}
    parent = {c["id"]: c["parent_id"] for c in concepts}
    for c in concepts:  # add each concept's direct count to all of its ancestors
        p = parent[c["id"]]
        while p:
            total[p] += c["series"]
            p = parent[p]
    methods: dict[str, int] = {}
    for r in rows:
        if r["method"]:
            methods[r["method"]] = methods.get(r["method"], 0) + 1
    return {
        "concepts": [c | {"total": total[c["id"]]} for c in concepts],
        "series": [
            {
                "source": r["source"],
                "id": r["series_id"],
                "title": r["title"],
                "freq": freq_from_fred(r["frequency"]),
                "concept": r["concept_id"],
                "method": r["method"],
                "popularity": r["popularity"],
                "synced": r["last_synced_at"] is not None,
            }
            for r in rows
        ],
        "methods": methods,
    }


def catalog_from_db(conn: psycopg.Connection) -> Catalog:
    roots = from_rows(
        conn.execute("SELECT id, parent_id, name, description, position FROM concepts").fetchall()
    )
    series: dict[str, list[dict]] = {}
    for r in conn.execute(
        """
        SELECT s.source, s.series_id, s.title, s.frequency, s.units, sc.concept_id,
               (s.extra->>'popularity')::int AS popularity
        FROM series_concepts sc JOIN series s USING (source, series_id)
        WHERE sc.concept_id IS NOT NULL
        """
    ):
        concept = r.pop("concept_id")
        series.setdefault(concept, []).append(r)
    return Catalog(roots, series)


def _implicit_card(source: str, series_id: str, title: str | None) -> Card:
    return Card.model_validate(
        {
            "version": 1,
            "title": title or series_id,
            "question": f"What is {title or series_id} doing?",
            "inputs": {"x": {"series": f"{source}:{series_id}", "label": title or series_id}},
            "panels": [
                {"type": "stat", "y": "x", "change": "yoy"},
                {"type": "timeseries", "title": "History", "y": ["x"]},
            ],
        }
    )


def _meta(conn: psycopg.Connection, source: str, series_id: str) -> dict | None:
    return conn.execute(
        """
        SELECT s.*, (SELECT count(*) FROM observations o
                     WHERE o.source = s.source AND o.series_id = s.series_id) AS observations
        FROM series s WHERE source = %s AND series_id = %s
        """,
        (source, series_id),
    ).fetchone()


def series_page(
    conn: psycopg.Connection,
    source: str,
    series_id: str,
    sync_source: SourceFactory | None = None,
) -> dict | None:
    """Metadata, placement and panel payloads for one series; None if it isn't known.

    A series with no observations yet (e.g. from the snapshot) is synced on first open when
    `sync_source` can provide its Source (i.e. a FRED_API_KEY is configured). Otherwise the
    page says how to sync it.
    """
    meta = _meta(conn, source, series_id)
    if meta is None:
        return None
    sync = None
    if not meta["observations"] and sync_source is not None:
        src = sync_source(source)
        if src is not None:
            result = sync_series(conn, src, series_id)
            sync = {"status": result.status, "error": result.error}
            if result.status != "failed":
                sync.pop("error")
                meta = _meta(conn, source, series_id)
    assert meta is not None
    synced = bool(meta["observations"])

    payloads, errors = [], []
    if synced:
        card_id = f"series-{source}-{series_id}".lower().replace("_", "-")
        result = run_spec(conn, card_id, _implicit_card(source, series_id, meta["title"]))
        payloads, errors = result.payloads, [e.to_dict() for e in result.errors]

    extra = meta["extra"] or {}
    return {
        "series": {
            "source": source,
            "id": series_id,
            "title": meta["title"],
            "units": meta["units"],
            "frequency": meta["frequency"],
            "freq": freq_from_fred(meta["frequency"]),
            "seasonal_adjustment": meta["seasonal_adjustment"],
            "notes": meta["notes"],
            "popularity": extra.get("popularity"),
            "observation_start": extra.get("observation_start"),
            "observation_end": extra.get("observation_end"),
            "upstream_updated_at": meta["upstream_updated_at"],
            "last_synced_at": meta["last_synced_at"],
            "observations": meta["observations"],
        },
        "synced": synced,
        "sync": sync,
        "hint": None if synced else f"tempus catalog add {source} {series_id} --sync",
        "placement": _placement(conn, source, series_id),
        "neighbors": _neighbors(conn, source, series_id),
        "links": {"source": f"https://fred.stlouisfed.org/series/{series_id}"}
        if source == "fred"
        else {},
        "payloads": payloads,
        "errors": errors,
    }


def _placement(conn: psycopg.Connection, source: str, series_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT concept_id, method, confidence, trail, filed_at FROM series_concepts
        WHERE source = %s AND series_id = %s
        """,
        (source, series_id),
    ).fetchone()
    if row is None:
        return None
    path = []
    concept_id = row["concept_id"]
    while concept_id:
        c = conn.execute(
            "SELECT id, parent_id, name FROM concepts WHERE id = %s", (concept_id,)
        ).fetchone()
        if c is None:
            break
        path.insert(0, {"id": c["id"], "name": c["name"]})
        concept_id = c["parent_id"]
    return row | {"path": path}


def _neighbors(conn: psycopg.Connection, source: str, series_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.source, s.series_id, s.title, s.frequency
        FROM series_concepts me
        JOIN series_concepts sc ON sc.concept_id = me.concept_id
        JOIN series s ON s.source = sc.source AND s.series_id = sc.series_id
        WHERE me.source = %s AND me.series_id = %s
          AND NOT (sc.source = me.source AND sc.series_id = me.series_id)
        ORDER BY (s.extra->>'popularity')::int DESC NULLS LAST, s.series_id
        LIMIT %s
        """,
        (source, series_id, NEIGHBORS),
    ).fetchall()
    return [
        {
            "source": r["source"],
            "id": r["series_id"],
            "title": r["title"],
            "freq": freq_from_fred(r["frequency"]),
        }
        for r in rows
    ]

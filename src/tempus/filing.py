"""Place series in the concept tree by walking it top-down with Jev (Jevbox-style filing).

At each level Jev picks among the children plus a "none of these" option. The walk goes
deeper only on a clear decision: the top choice has probability >= MIN_PROBABILITY and
leads the runner-up by >= MIN_LEAD. Otherwise the series stays at the deepest concept it
reached. A series that fits nothing at the top level is recorded with no concept, so a
person can decide whether the tree needs a new branch.
"""

from dataclasses import dataclass
from typing import Protocol

import psycopg
from psycopg.types.json import Jsonb

from tempus.jev import ChoiceAnswer
from tempus.ontology import Concept

MIN_PROBABILITY = 0.65
MIN_LEAD = 0.2
NONE = "none_of_these"
NOTES_LIMIT = 1500


class Chooser(Protocol):
    def choice(self, state, instructions: str, criteria: dict[str, str]) -> ChoiceAnswer: ...


@dataclass(frozen=True)
class Placement:
    concept_id: str | None
    confidence: float | None  # lowest probability along the path taken
    trail: list[dict]


def place(state: dict, roots: list[Concept], jev: Chooser) -> Placement:
    parent: Concept | None = None
    level = roots
    confidence: float | None = None
    trail: list[dict] = []

    while level:
        by_slug = {c.id.rsplit(".", 1)[-1]: c for c in level}
        criteria = {slug: f"{c.name}: {c.description}" for slug, c in by_slug.items()}
        if parent is None:
            criteria[NONE] = "None of these areas describes what the series measures."
            question = "Which area of the economy does this data series measure?"
        else:
            criteria[NONE] = f"None of these; it belongs under {parent.name} generally."
            question = f"Which part of {parent.name} does this data series measure?"

        ranked = jev.choice(state, question, criteria).ranked()
        top, p1 = ranked[0]
        p2 = ranked[1][1] if len(ranked) > 1 else 0.0
        trail.append(
            {
                "at": parent.id if parent else None,
                "top": [[k, round(p, 3)] for k, p in ranked[:3]],
            }
        )
        if top == NONE or p1 < MIN_PROBABILITY or p1 - p2 < MIN_LEAD:
            break
        parent = by_slug[top]
        confidence = p1 if confidence is None else min(confidence, p1)
        level = parent.children

    return Placement(parent.id if parent else None, confidence, trail)


def series_state(row: dict) -> dict:
    """What Jev sees about a series."""
    state = {
        k: row[k] for k in ("title", "units", "frequency", "seasonal_adjustment") if row.get(k)
    }
    if row.get("notes"):
        state["notes"] = row["notes"][:NOTES_LIMIT]
    return state


def file_series(
    conn: psycopg.Connection, roots: list[Concept], jev: Chooser, *, refile: bool = False
):
    """File every synced series that has no placement (or every auto placement if `refile`).

    Yields (source, series_id, Placement) as each one is decided. Manual pins are never touched.
    """
    rows = conn.execute(
        """
        SELECT s.* FROM series s
        LEFT JOIN series_concepts sc USING (source, series_id)
        WHERE sc.series_id IS NULL OR (%s AND sc.method = 'auto')
        ORDER BY s.source, s.series_id
        """,
        (refile,),
    ).fetchall()
    for row in rows:
        placement = place(series_state(row), roots, jev)
        conn.execute(
            """
            INSERT INTO series_concepts (source, series_id, concept_id, method, confidence, trail)
            VALUES (%s, %s, %s, 'auto', %s, %s)
            ON CONFLICT (source, series_id) DO UPDATE SET
                concept_id = EXCLUDED.concept_id, method = 'auto',
                confidence = EXCLUDED.confidence, trail = EXCLUDED.trail, filed_at = now()
            """,
            (
                row["source"],
                row["series_id"],
                placement.concept_id,
                placement.confidence,
                Jsonb(placement.trail),
            ),
        )
        yield row["source"], row["series_id"], placement

"""The concept tree (ontology/concepts.yaml): what each series measures."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import yaml

SLUG = re.compile(r"[a-z][a-z0-9_]*")


class OntologyError(ValueError):
    pass


@dataclass
class Concept:
    id: str  # dotted path
    name: str
    description: str
    parent_id: str | None = None
    children: list["Concept"] = field(default_factory=list)


def load(path: Path) -> list[Concept]:
    """Parse and validate the YAML tree. Returns the top-level concepts."""
    raw = yaml.safe_load(path.read_text()) or []
    return _parse(raw, parent_id=None, where=path.name)


def _parse(nodes, parent_id: str | None, where: str) -> list[Concept]:
    if not isinstance(nodes, list):
        raise OntologyError(f"{where}: expected a list of concepts")
    seen: set[str] = set()
    concepts = []
    for node in nodes:
        slug = node.get("id", "")
        here = f"{where} › {slug or '?'}"
        if not SLUG.fullmatch(slug):
            raise OntologyError(f"{here}: id must be lowercase snake_case")
        if slug in seen:
            raise OntologyError(f"{here}: duplicate id among siblings")
        seen.add(slug)
        for key in ("name", "description"):
            if not str(node.get(key, "")).strip():
                raise OntologyError(f"{here}: missing {key}")
        concept_id = f"{parent_id}.{slug}" if parent_id else slug
        concept = Concept(concept_id, node["name"], node["description"].strip(), parent_id)
        concept.children = _parse(node.get("children", []), concept_id, here)
        concepts.append(concept)
    return concepts


def walk(concepts: list[Concept]):
    """Depth-first, parents before children."""
    for c in concepts:
        yield c
        yield from walk(c.children)


def sync_to_db(conn: psycopg.Connection, roots: list[Concept]) -> tuple[int, int]:
    """Mirror the tree into `concepts`. Returns (upserted, removed)."""
    concepts = list(walk(roots))
    positions: dict[str | None, int] = {}
    with conn.transaction():
        for c in concepts:
            positions[c.parent_id] = positions.get(c.parent_id, -1) + 1
            conn.execute(
                """
                INSERT INTO concepts (id, parent_id, name, description, position)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    parent_id = EXCLUDED.parent_id, name = EXCLUDED.name,
                    description = EXCLUDED.description, position = EXCLUDED.position
                """,
                (c.id, c.parent_id, c.name, c.description, positions[c.parent_id]),
            )
        # Removed concepts cascade to their placements, so those series get filed again.
        removed = conn.execute(
            "DELETE FROM concepts WHERE NOT (id = ANY(%s))", ([c.id for c in concepts],)
        ).rowcount
        # The tree may now have a home for series that previously fit nowhere.
        conn.execute("DELETE FROM series_concepts WHERE method = 'auto' AND concept_id IS NULL")
    return len(concepts), removed


def apply_pins(conn: psycopg.Connection, pins: dict[tuple[str, str], str]) -> int:
    """Record manual placements from catalog.toml. Returns how many were applied."""
    applied = 0
    with conn.transaction():
        # Pins removed from the catalog become eligible for auto filing again.
        conn.execute(
            """
            DELETE FROM series_concepts
            WHERE method = 'manual' AND NOT ((source || ':' || series_id) = ANY(%s))
            """,
            ([f"{s}:{i}" for s, i in pins],),
        )
        for (source, series_id), concept_id in pins.items():
            exists = conn.execute(
                "SELECT 1 FROM series WHERE source = %s AND series_id = %s", (source, series_id)
            ).fetchone()
            if not exists:
                continue  # not synced yet; pinned on a later run
            conn.execute(
                """
                INSERT INTO series_concepts (source, series_id, concept_id, method)
                VALUES (%s, %s, %s, 'manual')
                ON CONFLICT (source, series_id) DO UPDATE SET
                    concept_id = EXCLUDED.concept_id, method = 'manual',
                    confidence = NULL, trail = '[]'::jsonb, filed_at = now()
                """,
                (source, series_id, concept_id),
            )
            applied += 1
    return applied

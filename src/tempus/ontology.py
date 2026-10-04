"""The concept tree (ontology/concepts.yaml): what each series measures."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import yaml
from psycopg.types.json import Jsonb

SLUG = re.compile(r"[a-z][a-z0-9_]*")
REF = re.compile(r"([a-z][a-z0-9_]*):(\S+)")
DRAFTS_VERSION = 1


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


@dataclass(frozen=True)
class Draft:
    concept: str
    low: str | None = None  # why it was a judgment call, if it was one


def load_drafts(path: Path) -> dict[tuple[str, str], Draft]:
    """Parse ontology/placements.draft.yaml: concept id -> series refs. Missing file = none."""
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict) or raw.get("version") != DRAFTS_VERSION:
        raise OntologyError(f"{path.name}: expected `version: {DRAFTS_VERSION}`")
    drafts: dict[tuple[str, str], Draft] = {}
    for concept, refs in (raw.get("placements") or {}).items():
        for entry in refs or []:
            ref, low = (
                (entry.get("ref", ""), entry.get("low"))
                if isinstance(entry, dict)
                else (
                    str(entry),
                    None,
                )
            )
            match = REF.fullmatch(ref)
            if not match:
                raise OntologyError(f"{path.name} › {concept}: {ref!r} is not source:series_id")
            key = (match[1], match[2])
            if key in drafts:
                raise OntologyError(f"{path.name}: {ref} is placed more than once")
            drafts[key] = Draft(str(concept), low)
    return drafts


def apply_drafts(conn: psycopg.Connection, drafts: dict[tuple[str, str], Draft]) -> int:
    """Record draft placements where nothing better exists. Returns how many were applied.

    Precedence is manual > auto > draft: a draft never replaces a pin or a Jev decision, and
    `filing.file_series` treats drafts as unfiled. Drafts for series that aren't in the
    database yet, or for concepts the tree no longer has, are skipped.
    """
    applied = 0
    with conn.transaction():
        # Drafts removed from (or moved in) the file follow it.
        conn.execute(
            """
            DELETE FROM series_concepts
            WHERE method = 'draft' AND NOT ((source || ':' || series_id) = ANY(%s))
            """,
            ([f"{s}:{i}" for s, i in drafts],),
        )
        for (source, series_id), draft in drafts.items():
            row = conn.execute(
                """
                INSERT INTO series_concepts (source, series_id, concept_id, method, trail)
                SELECT %(source)s, %(series_id)s, %(concept)s, 'draft', %(trail)s
                WHERE EXISTS (SELECT 1 FROM series
                              WHERE source = %(source)s AND series_id = %(series_id)s)
                  AND EXISTS (SELECT 1 FROM concepts WHERE id = %(concept)s)
                ON CONFLICT (source, series_id) DO UPDATE SET
                    concept_id = EXCLUDED.concept_id, trail = EXCLUDED.trail,
                    confidence = NULL, filed_at = now()
                WHERE series_concepts.method = 'draft'
                RETURNING 1
                """,
                {
                    "source": source,
                    "series_id": series_id,
                    "concept": draft.concept,
                    "trail": Jsonb([{"draft": True, "low": draft.low}]),
                },
            ).fetchone()
            applied += row is not None
    return applied

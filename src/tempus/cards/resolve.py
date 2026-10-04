"""Turn a card's references into series metadata. Frequency comes from here."""

from dataclasses import dataclass

import psycopg

from tempus.cards.errors import CardError, did_you_mean
from tempus.cards.spec import Card, ConceptInput
from tempus.cards.values import freq_from_fred


@dataclass(frozen=True)
class SeriesInfo:
    source: str
    series_id: str
    title: str | None
    units: str | None
    freq: str


def _info(row) -> SeriesInfo:
    return SeriesInfo(
        row["source"],
        row["series_id"],
        row["title"],
        row["units"],
        freq_from_fred(row["frequency"]),
    )


def resolve(
    conn: psycopg.Connection, card_id: str, card: Card
) -> tuple[dict[str, list[SeriesInfo]], list[CardError]]:
    errors: list[CardError] = []
    resolved: dict[str, list[SeriesInfo]] = {}
    rows = conn.execute("SELECT source, series_id, title, units, frequency FROM series").fetchall()
    series = {(r["source"], r["series_id"]): r for r in rows}
    concept_ids = {r["id"] for r in conn.execute("SELECT id FROM concepts").fetchall()}

    for i, concept in enumerate(card.concepts):
        if concept not in concept_ids:
            errors.append(
                CardError(
                    card_id,
                    f"concepts[{i}]",
                    f"unknown concept '{concept}'.",
                    hint=did_you_mean(concept, concept_ids),
                )
            )

    for name, inp in card.inputs.items():
        path = f"inputs.{name}"
        if isinstance(inp, ConceptInput):
            if inp.concept not in concept_ids:
                errors.append(
                    CardError(
                        card_id,
                        path,
                        f"unknown concept '{inp.concept}'.",
                        hint=did_you_mean(inp.concept, concept_ids),
                    )
                )
                continue
            members = conn.execute(
                """
                SELECT s.source, s.series_id, s.title, s.units, s.frequency
                FROM series_concepts sc JOIN series s USING (source, series_id)
                WHERE sc.concept_id = %(c)s OR sc.concept_id LIKE %(c)s || '.%%'
                ORDER BY s.source, s.series_id
                """,
                {"c": inp.concept},
            ).fetchall()
            if not members:
                errors.append(
                    CardError(
                        card_id, path, f"concept '{inp.concept}' has no series filed under it"
                    )
                )
                continue
            resolved[name] = [_info(r) for r in members]
        else:
            row = series.get((inp.source, inp.series_id))
            if row is None:
                refs = [f"{s}:{i}" for s, i in series if s == inp.source]
                hint = did_you_mean(inp.series, refs) or (
                    f"Add it with `tempus catalog add {inp.source} {inp.series_id} --sync`."
                )
                errors.append(
                    CardError(
                        card_id, path, f"series '{inp.series}' isn't in the database.", hint=hint
                    )
                )
                continue
            resolved[name] = [_info(row)]
    return resolved, errors

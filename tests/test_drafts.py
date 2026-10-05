from pathlib import Path

import psycopg
import pytest

from tempus import db, ontology
from tempus.filing import file_series
from tempus.jev import ChoiceAnswer
from tempus.ontology import OntologyError

TREE = """
- id: labor
  name: Labor Market
  description: Jobs and workers.
  children:
    - id: unemployment
      name: Unemployment
      description: Unemployment rates.
- id: prices
  name: Prices
  description: Inflation.
"""

DRAFTS = """
version: 1
placements:
  labor.unemployment:
    - fred:UNRATE
    - {ref: fred:U6RATE, low: "could be its own branch"}
  prices:
    - fred:CPIAUCSL
    - fred:NOTSYNCED
"""


@pytest.fixture
def paths(tmp_path):
    (tmp_path / "concepts.yaml").write_text(TREE)
    (tmp_path / "drafts.yaml").write_text(DRAFTS)
    return tmp_path


def test_load_drafts_parses_refs_and_low_flags(paths):
    drafts = ontology.load_drafts(paths / "drafts.yaml")
    assert drafts[("fred", "UNRATE")] == ontology.Draft("labor.unemployment", None)
    assert drafts[("fred", "U6RATE")].low == "could be its own branch"
    assert len(drafts) == 4


def test_missing_drafts_file_is_empty(tmp_path):
    assert ontology.load_drafts(tmp_path / "nope.yaml") == {}


@pytest.mark.parametrize(
    "text, message",
    [
        ("version: 2\nplacements: {}", "version"),
        ("version: 1\nplacements:\n  a: [UNRATE]", "source:series_id"),
        ("version: 1\nplacements:\n  a: [fred:X]\n  b: [fred:X]", "more than once"),
    ],
)
def test_load_drafts_rejects_bad_files(tmp_path, text, message):
    path = tmp_path / "d.yaml"
    path.write_text(text)
    with pytest.raises(OntologyError, match=message):
        ontology.load_drafts(path)


def test_repo_drafts_cover_the_snapshot_with_known_concepts():
    import json

    root = Path(__file__).parents[1]
    drafts = ontology.load_drafts(root / "ontology" / "placements.draft.yaml")
    known = {c.id for c in ontology.walk(ontology.load(root / "ontology" / "concepts.yaml"))}
    snapshot = json.loads((root / "data" / "fred" / "series-snapshot.json").read_text())
    assert {("fred", s["id"]) for s in snapshot["series"]} <= set(drafts)
    assert {d.concept for d in drafts.values()} <= known


def _series(conn, *ids):
    for sid in ids:
        conn.execute("INSERT INTO series (source, series_id) VALUES ('fred', %s)", (sid,))


def _methods(conn) -> dict[str, tuple[str, str | None]]:
    rows = conn.execute("SELECT series_id, concept_id, method FROM series_concepts").fetchall()
    return {r["series_id"]: (r["method"], r["concept_id"]) for r in rows}


def test_apply_drafts_fills_only_unplaced_series(conn, paths):
    roots = ontology.load(paths / "concepts.yaml")
    ontology.sync_to_db(conn, roots)
    _series(conn, "UNRATE", "U6RATE", "CPIAUCSL")
    ontology.apply_pins(conn, {("fred", "CPIAUCSL"): "labor"})
    applied = ontology.apply_drafts(conn, ontology.load_drafts(paths / "drafts.yaml"))
    assert applied == 2  # NOTSYNCED isn't in `series`; the pin outranks the draft
    assert _methods(conn) == {
        "UNRATE": ("draft", "labor.unemployment"),
        "U6RATE": ("draft", "labor.unemployment"),
        "CPIAUCSL": ("manual", "labor"),
    }
    low = conn.execute("SELECT trail FROM series_concepts WHERE series_id = 'U6RATE'").fetchone()
    assert low["trail"] == [{"draft": True, "low": "could be its own branch"}]


def test_drafts_are_idempotent_and_follow_the_file(conn, paths):
    ontology.sync_to_db(conn, ontology.load(paths / "concepts.yaml"))
    _series(conn, "UNRATE", "CPIAUCSL")
    drafts = ontology.load_drafts(paths / "drafts.yaml")
    ontology.apply_drafts(conn, drafts)
    ontology.apply_drafts(conn, drafts)
    assert len(_methods(conn)) == 2
    # Moving a draft in the file moves it in the DB; dropping it removes the placement.
    ontology.apply_drafts(conn, {("fred", "UNRATE"): ontology.Draft("prices", None)})
    assert _methods(conn) == {"UNRATE": ("draft", "prices")}


def test_removing_a_pin_lets_the_draft_back_in(conn, paths):
    ontology.sync_to_db(conn, ontology.load(paths / "concepts.yaml"))
    _series(conn, "UNRATE")
    drafts = ontology.load_drafts(paths / "drafts.yaml")
    ontology.apply_pins(conn, {("fred", "UNRATE"): "prices"})
    ontology.apply_drafts(conn, drafts)
    assert _methods(conn)["UNRATE"] == ("manual", "prices")
    ontology.apply_pins(conn, {})
    ontology.apply_drafts(conn, drafts)
    assert _methods(conn)["UNRATE"] == ("draft", "labor.unemployment")


class _AlwaysPrices:
    def choice(self, state, instructions, criteria):
        probs = {k: (0.95 if k == "prices" else 0.01) for k in criteria}
        return ChoiceAnswer(max(probs, key=probs.get), probs)


def test_filing_redecides_drafts_but_never_pins(conn, paths):
    roots = ontology.load(paths / "concepts.yaml")
    ontology.sync_to_db(conn, roots)
    _series(conn, "UNRATE", "CPIAUCSL")
    ontology.apply_pins(conn, {("fred", "CPIAUCSL"): "labor"})
    ontology.apply_drafts(conn, ontology.load_drafts(paths / "drafts.yaml"))
    filed = [sid for _, sid, _ in file_series(conn, roots, _AlwaysPrices())]
    assert filed == ["UNRATE"]
    assert _methods(conn) == {"UNRATE": ("auto", "prices"), "CPIAUCSL": ("manual", "labor")}
    # A later draft load never overwrites Jev's decision.
    ontology.apply_drafts(conn, ontology.load_drafts(paths / "drafts.yaml"))
    assert _methods(conn)["UNRATE"] == ("auto", "prices")


def test_schema_migration_upgrades_an_existing_database(conn):
    # Recreate the pre-draft constraint, as an older `tempus db init` left it.
    conn.execute("ALTER TABLE series_concepts DROP CONSTRAINT series_concepts_method_check")
    conn.execute(
        "ALTER TABLE series_concepts ADD CONSTRAINT series_concepts_method_check"
        " CHECK (method IN ('manual', 'auto'))"
    )
    _series(conn, "X")
    insert = "INSERT INTO series_concepts (source, series_id, method) VALUES ('fred', 'X', 'draft')"
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(insert)
    db.init_schema(conn)
    db.init_schema(conn)  # idempotent
    conn.execute(insert)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE series_concepts SET method = 'guess'")

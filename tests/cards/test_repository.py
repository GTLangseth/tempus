from pathlib import Path

from tempus.cards.repository import FileCardRepository, sync_index

CARD = """\
version: 1
title: Crude
question: Does crude lead gasoline?
concepts: [prices.consumer.energy]
tags: [energy]
inputs: {crude: "fred:DCOILWTICO"}
panels: [{type: timeseries, y: [crude]}]
"""


def test_ids_get_and_missing(tmp_path: Path):
    (tmp_path / "crude.yaml").write_text(CARD)
    (tmp_path / "notes.txt").write_text("ignored")
    repo = FileCardRepository(tmp_path)
    assert repo.ids() == ["crude"]
    card, errors = repo.get("crude")
    assert errors == [] and card.title == "Crude"
    card, errors = repo.get("crud")
    assert card is None and errors[0].hint == "Did you mean 'crude'?"


def test_missing_root_has_no_cards(tmp_path: Path):
    assert FileCardRepository(tmp_path / "nope").ids() == []


def test_save_refuses_invalid_cards_and_writes_valid_ones(tmp_path: Path):
    repo = FileCardRepository(tmp_path)
    errors = repo.save("broken", "version: 1\ntitle: x\n")
    assert errors and not (tmp_path / "broken.yaml").exists()
    assert repo.save("crude", CARD) == []
    assert repo.text("crude") == CARD


def test_sync_index_loads_valid_and_drops_deleted(conn, tmp_path: Path):
    (tmp_path / "crude.yaml").write_text(CARD)
    (tmp_path / "bad.yaml").write_text("version: 1\n")
    repo = FileCardRepository(tmp_path)
    loaded, errors = sync_index(conn, repo)
    assert loaded == 1 and any(e.card == "bad" for e in errors)
    row = conn.execute(
        "SELECT title, concepts, tags, spec FROM cards WHERE id = 'crude'"
    ).fetchone()
    assert row["title"] == "Crude" and row["concepts"] == ["prices.consumer.energy"]
    assert row["spec"]["inputs"]["crude"]["series"] == "fred:DCOILWTICO"
    (tmp_path / "crude.yaml").unlink()
    sync_index(conn, repo)
    assert conn.execute("SELECT count(*) AS n FROM cards").fetchone()["n"] == 0

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner

from tempus.app import create_app, reading_event
from tempus.cards.repository import FileCardRepository
from tempus.cli import app as cli_app
from tempus.config import Settings, get_settings
from tempus.readings import (
    CardEmbed,
    ClaimBlock,
    DividerBlock,
    FileReadingRepository,
    Footnote,
    HeadingBlock,
    ListBlock,
    NoteBlock,
    ParagraphBlock,
    QuoteBlock,
    check_reading,
    has_errors,
    parse_reading,
)

from .cards.seed import seed
from .conftest import TEST_DB

LEAD = Path(__file__).parent / "cards" / "fixtures" / "lead.yaml"

SAMPLE = """\
---
version: 1
title: Lead and lag note
authors: ["Ada Lovelace"]
source: Lab Note
published: 2026-08
summary: Short note testing live cards and claims.
concepts: [prices.energy]
tags: [macro]
---

## Overview

First paragraph with **bold** and `fred:XLEAD`.[^1]

> A blockquote from the paper.

- Item one
- Item two

---

```card
lead
```

```card
card: lead
caption: Clipped window view.
start: 2020-01-01
end: 2023-12-31
```

```claim
quote: "X leads Y by one month."
status: confirmed
verdict: Confirmed in the synthetic store.
series: [fred:XLEAD, fred:YFOLLOW]
card: lead
```

```note
title: Next checks
- Watch the 1-month lag stability.
```

[^1]: First footnote explaining the sample series.
  Continuation on next line.
"""


@pytest.fixture
def env_paths(tmp_path: Path):
    cards_dir = tmp_path / "cards"
    readings_dir = tmp_path / "readings"
    cards_dir.mkdir()
    readings_dir.mkdir()
    (cards_dir / "lead.yaml").write_text(LEAD.read_text())
    (cards_dir / "broken.yaml").write_text("title: [oops\n")
    (readings_dir / "sample.md").write_text(SAMPLE)
    url = make_conninfo(get_settings().database_url, dbname=TEST_DB)
    return Settings(database_url=url, cards_path=cards_dir, readings_path=readings_dir)


def test_parse_reading_valid_blocks():
    reading, errors = parse_reading("sample", SAMPLE)
    assert errors == [] and reading is not None
    assert reading.meta.published == "2026-08"
    assert reading.card_ids() == ["lead"]
    assert reading.claim_counts() == {
        "confirmed": 1,
        "extended": 0,
        "nuanced": 0,
        "unverified": 0,
    }
    types = [type(b) for b in reading.blocks]
    assert types == [
        HeadingBlock,
        ParagraphBlock,
        QuoteBlock,
        ListBlock,
        DividerBlock,
        CardEmbed,
        CardEmbed,
        ClaimBlock,
        NoteBlock,
    ]
    assert reading.blocks[5] == CardEmbed("lead", path="body.line:23")
    assert reading.blocks[8] == NoteBlock(
        title="Next checks",
        paragraphs=("- Watch the 1-month lag stability.",),
        path="body.line:42",
    )
    assert reading.footnotes == (
        Footnote("1", "First footnote explaining the sample series. Continuation on next line."),
    )
    assert reading.word_count() > 15 and reading.reading_minutes() == 1


@pytest.mark.parametrize(
    ("name", "body", "needle"),
    [
        ("BadName", SAMPLE, "kebab-case"),
        ("no-fm", "# Just markdown\n", "YAML front matter"),
        (
            "bad-fence",
            "---\nversion: 1\ntitle: T\nauthors: [A]\npublished: '2026'\nsummary: S\n"
            "---\n```cards\nlead\n```\n",
            "Did you mean 'card'?",
        ),
        (
            "bad-claim",
            "---\nversion: 1\ntitle: T\nauthors: [A]\npublished: '2026'\nsummary: S\n"
            "---\n```claim\nquote: q\nstatus: bogus\nverdict: v\nseries: [BADREF]\n```\n",
            "Input should be",
        ),
    ],
)
def test_parse_reading_rejects_invalid_inputs(name, body, needle):
    reading, errors = parse_reading(name, body)
    assert reading is None and has_errors(errors)
    assert any(needle in (e.message + (e.hint or "")) for e in errors)


def test_check_reading_offline_catches_bad_refs_and_concepts(env_paths):
    cards = FileCardRepository(env_paths.cards_path)
    readings = FileReadingRepository(env_paths.readings_path)
    reading, _ = readings.get("sample")
    assert reading is not None

    assert check_reading("sample", reading, cards, known_concepts={"prices.energy"}) == []
    concept_errs = check_reading("sample", reading, cards, known_concepts={"prices.enrgy"})
    assert len(concept_errs) == 1 and "Did you mean" in (concept_errs[0].hint or "")

    missing_card_md = SAMPLE.replace("card: lead", "card: leed")
    r_missing, _ = parse_reading("missing", missing_card_md)
    assert r_missing is not None
    errs = check_reading("missing", r_missing, cards)
    assert any(
        "unknown card 'leed'" in e.message and "Did you mean 'lead'?" in (e.hint or "")
        for e in errs
    )

    broken_md = SAMPLE.replace("card: lead", "card: broken")
    r_broken, _ = parse_reading("broken-ref", broken_md)
    assert r_broken is not None
    errs = check_reading("broken-ref", r_broken, cards)
    assert any("card 'broken' has errors" in e.message for e in errs)

    missing_fn_md = SAMPLE.replace(
        "[^1]: First footnote explaining the sample series.\n  Continuation on next line.\n", ""
    )
    r_missing_fn, _ = parse_reading("missing-fn", missing_fn_md)
    assert r_missing_fn is not None
    fn_errs = check_reading("missing-fn", r_missing_fn, cards)
    assert any("missing footnote definition '[^1]'" in e.message for e in fn_errs)

    unused_fn_md = SAMPLE + "\n[^2]: Unused note.\n"
    r_unused_fn, _ = parse_reading("unused-fn", unused_fn_md)
    assert r_unused_fn is not None
    unused_errs = check_reading("unused-fn", r_unused_fn, cards)
    assert not has_errors(unused_errs)
    assert any(e.level == "warning" and "unused footnote '[^2]'" in e.message for e in unused_errs)


def test_check_reading_db_series_and_unverified(conn, env_paths):
    seed(conn)
    conn.execute(
        "INSERT INTO series (source, series_id, title) VALUES ('fred', 'METAONLY', 'Meta')"
    )
    cards = FileCardRepository(env_paths.cards_path)

    # Unsynced series in a confirmed claim emits a warning; missing series emits an error.
    meta_only_md = SAMPLE.replace("fred:YFOLLOW", "fred:METAONLY")
    r_warn, _ = parse_reading("warn", meta_only_md)
    assert r_warn is not None
    warn_errs = check_reading("warn", r_warn, cards, conn=conn)
    assert not has_errors(warn_errs)
    assert any(e.level == "warning" and "metadata only" in e.message for e in warn_errs)

    # An unverified claim referencing an unsynced series does not warn.
    unverified_md = meta_only_md.replace("status: confirmed", "status: unverified")
    r_unv, _ = parse_reading("unv", unverified_md)
    assert r_unv is not None
    assert check_reading("unv", r_unv, cards, conn=conn) == []

    missing_series_md = SAMPLE.replace("fred:YFOLLOW", "fred:NOSUCHSERIES")
    r_err, _ = parse_reading("err", missing_series_md)
    assert r_err is not None
    errs = check_reading("err", r_err, cards, conn=conn)
    assert has_errors(errs) and any("unknown series 'fred:NOSUCHSERIES'" in e.message for e in errs)


def test_readings_api_and_sse_events(conn, env_paths):
    seed(conn)
    client = TestClient(create_app(env_paths))

    listing = client.get("/api/readings").json()
    assert len(listing) == 1
    assert listing[0]["id"] == "sample" and listing[0]["cards"] == 1 and listing[0]["errors"] == 0
    assert listing[0]["word_count"] > 15 and listing[0]["reading_minutes"] == 1

    detail = client.get("/api/readings/sample").json()
    assert detail["errors"] == []
    assert len(detail["footnotes"]) == 1 and detail["footnotes"][0]["id"] == "1"
    note = next(b for b in detail["blocks"] if b["type"] == "note")
    assert note["title"] == "Next checks"
    claim = next(b for b in detail["blocks"] if b["type"] == "claim")
    assert len(claim["series_info"]) == 2
    assert claim["series_info"][0]["latest_value"] is not None
    embed = next(b for b in detail["blocks"] if b["type"] == "card")
    assert embed["result"]["payloads"][0]["type"] == "timeseries"

    assert client.get("/api/readings/nope").status_code == 404

    cards_repo = FileCardRepository(env_paths.cards_path)
    readings_repo = FileReadingRepository(env_paths.readings_path)
    assert reading_event(readings_repo, cards_repo, env_paths.readings_path / "sample.md") == {
        "event": "reading-changed",
        "reading": "sample",
    }
    assert reading_event(readings_repo, cards_repo, env_paths.readings_path / "gone.md") == {
        "event": "reading-removed",
        "reading": "gone",
    }
    for temp in (".#sample.md", "sample.tmp.md", "~sample.md", "README.md"):
        p = env_paths.readings_path / temp
        p.write_text("bad\n")
        assert reading_event(readings_repo, cards_repo, p) is None


def test_reading_cli_offline_commands(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    seed_card = (Path("cards") / "crude-to-gasoline.yaml").read_text()
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "crude-to-gasoline.yaml").write_text(seed_card)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEMPUS_STORAGE", "files")

    res_empty = runner.invoke(cli_app, ["reading", "check", "--offline"])
    assert res_empty.exit_code == 0, res_empty.output
    assert "no readings found" in res_empty.output

    res_new = runner.invoke(cli_app, ["reading", "new", "sample-essay"])
    assert res_new.exit_code == 0, res_new.output

    res_check = runner.invoke(cli_app, ["reading", "check", "--offline"])
    assert res_check.exit_code == 0, res_check.output
    assert "sample-essay" in res_check.output

    res_list = runner.invoke(cli_app, ["reading", "list"])
    assert res_list.exit_code == 0, res_list.output
    assert "sample-essay" in res_list.output

    reading, errs = FileReadingRepository(tmp_path / "readings").get("sample-essay")
    assert reading is not None and errs == []
    assert reading.card_ids() == ["crude-to-gasoline"]

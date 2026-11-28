"""Tests for Postgres-backed cards/readings storage, tracked sync, and article ingestion."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from tempus import transfer
from tempus.app import create_app
from tempus.cards.repository import DbCardRepository, FileCardRepository, sync_index
from tempus.config import Settings, get_settings
from tempus.ingest import create_job, get_job, run_ingest_job
from tempus.llm import (
    AnthropicIngestLLM,
    ComposedEnrichment,
    EnrichmentQuery,
    ExtractedArticle,
    GeminiIngestLLM,
    LLMError,
    ProposedCard,
    ProposedClaim,
    configured_provider,
    fetch_url_article,
    select_ingest_llm,
)
from tempus.readings import DbReadingRepository, check_reading
from tempus.sync import mark_tracked, tracked_series

from .conftest import TEST_DB

VALID_CARD_YAML = """\
version: 1
title: Fed funds trend
question: How has the policy rate evolved?
concepts: [rates.policy]
tags: [rates]
inputs:
  ff: fred:FEDFUNDS
panels:
  - type: timeseries
    y: [ff]
"""

VALID_READING_MD = """\
---
version: 1
title: Policy Rate Watch
authors: ["Test Analyst"]
source: Test Desk
published: "2026-11"
summary: Short note tracking policy rate levels.
concepts: [rates.policy]
tags: [rates]
---

## Policy Stance

Short prose paragraph linking to a live chart.

```card
card: dyn-rate-card
caption: Policy rate history from stored FRED observations.
```
"""


def _db_url(dbname: str = TEST_DB) -> str:
    return make_conninfo(get_settings().database_url, dbname=dbname)


def _seed_series(conn, source: str = "fred", series_id: str = "FEDFUNDS") -> None:
    conn.execute(
        """
        INSERT INTO series (source, series_id, title, units, frequency, last_synced_at)
        VALUES (%s, %s, 'Federal Funds Effective Rate', 'Percent', 'Monthly', now())
        ON CONFLICT (source, series_id) DO NOTHING
        """,
        (source, series_id),
    )
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO observations (source, series_id, ts, value) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT DO NOTHING",
            [
                (source, series_id, datetime(2024, m, 1, tzinfo=UTC), 4.5 + m * 0.05)
                for m in range(1, 13)
            ],
        )


def _seed_concepts(conn) -> None:
    conn.execute(
        "INSERT INTO concepts (id, parent_id, name, description, position) VALUES"
        " ('rates', NULL, 'Interest Rates', 'Rates and yields', 1),"
        " ('rates.policy', 'rates', 'Policy Rates', 'Central bank policy rates', 1)"
        " ON CONFLICT (id) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO series_concepts (source, series_id, concept_id, method, confidence)"
        " VALUES ('fred', 'FEDFUNDS', 'rates.policy', 'manual', 1.0)"
        " ON CONFLICT DO NOTHING"
    )


class _FakeLLM:
    provider_name = "gemini"

    def __init__(self, *, valid_card: bool = True):
        self.valid_card = valid_card

    def extract_article(self, **kwargs) -> ExtractedArticle:
        return ExtractedArticle(
            slug="macro-rate-digest",
            title="Macro Rate Digest",
            authors=["Jane Economist"],
            source="Test Wire",
            published="2026-11",
            summary="Synthetic essay testing dynamic extraction and FRED enrichment.",
            concepts=["rates.policy"],
            tags=["rates"],
            body_markdown=(
                "## Rate Regime Shift\n\n"
                "Central bank policy rates moved noticeably across the cycle.[^1]\n\n"
                "Higher short rates tightened financial conditions."
            ),
            footnotes=[{"id": "1", "text": "Observed monthly effective funds rate."}],
            queries=[
                EnrichmentQuery(
                    topic="Federal funds policy rate",
                    claim_quote="Policy rates moved noticeably across the cycle.",
                    search_query="federal funds effective rate",
                    anchor_heading="Rate Regime Shift",
                )
            ],
        )

    def compose_enrichment(
        self, *, article: ExtractedArticle, series_context: list[dict], known_concepts: list[str]
    ) -> ComposedEnrichment:
        if not self.valid_card:
            return ComposedEnrichment(
                cards=[
                    ProposedCard(
                        id="broken-card",
                        anchor_heading="Rate Regime Shift",
                        spec={
                            "version": 1,
                            "title": "Broken",
                            "question": "Bad formula?",
                            "inputs": {"x": "fred:FEDFUNDS"},
                            "formulas": {"y": "nonexistent_fn(x)"},
                            "panels": [{"type": "timeseries", "y": ["y"]}],
                        },
                    )
                ]
            )
        return ComposedEnrichment(
            cards=[
                ProposedCard(
                    id="fedfunds-cycle",
                    anchor_heading="Rate Regime Shift",
                    caption="Monthly policy rate history.",
                    note_title="Tempus Policy Check",
                    note_body="Stored FRED data confirms elevated policy rates.",
                    claim=ProposedClaim(
                        quote="Policy rates moved noticeably across the cycle.",
                        status="confirmed",
                        verdict="FEDFUNDS stays above 4.5% across all stored months.",
                        series=["fred:FEDFUNDS"],
                    ),
                    spec={
                        "version": 1,
                        "title": "Effective Fed Funds Rate",
                        "question": "What level is the effective policy rate?",
                        "concepts": ["rates.policy"],
                        "tags": ["rates"],
                        "inputs": {"ff": "fred:FEDFUNDS"},
                        "panels": [{"type": "timeseries", "y": ["ff"]}],
                    },
                )
            ]
        )


def test_db_card_and_reading_repositories(conn, tmp_path: Path):
    _seed_series(conn)
    _seed_concepts(conn)
    conn.commit()
    url = _db_url()

    fallback_cards = tmp_path / "cards"
    fallback_cards.mkdir()
    (fallback_cards / "seed-card.yaml").write_text(VALID_CARD_YAML)

    cards_repo = DbCardRepository(url, fallback_root=fallback_cards)
    assert cards_repo.ids() == ["seed-card"]
    assert cards_repo.save("dyn-rate-card", "title: missing fields") != []
    assert cards_repo.save("dyn-rate-card", VALID_CARD_YAML) == []
    assert cards_repo.ids() == ["dyn-rate-card", "seed-card"]

    card, errs = cards_repo.get("dyn-rate-card")
    assert card is not None and errs == []
    assert "FREDFUNDS" in cards_repo.text("dyn-rate-card") or "FEDFUNDS" in cards_repo.text(
        "dyn-rate-card"
    )

    readings_repo = DbReadingRepository(url, fallback_root=tmp_path / "readings")
    assert readings_repo.save("policy-watch", VALID_READING_MD, origin="ingested") == []
    assert readings_repo.ids() == ["policy-watch"]
    reading, rerrs = readings_repo.get("policy-watch")
    assert reading is not None and rerrs == []
    assert check_reading("policy-watch", reading, cards_repo, conn=conn) == []

    info = readings_repo.status_info("policy-watch")
    assert info is not None and info["origin"] == "ingested" and info["status"] == "published"

    assert readings_repo.delete("policy-watch") is True
    assert readings_repo.delete("policy-watch") is False
    assert cards_repo.delete("dyn-rate-card") is True
    assert cards_repo.delete("dyn-rate-card") is False


def test_sync_mark_tracked_and_list(conn):
    _seed_series(conn, "fred", "AAA")
    _seed_series(conn, "fred", "BBB")
    mark_tracked(conn, "fred", "BBB", True)
    conn.commit()

    assert tracked_series(conn) == [("fred", "BBB")]
    mark_tracked(conn, "fred", "BBB", False)
    assert tracked_series(conn) == []


def test_transfer_syncs_dynamic_cards_and_readings(dev_conn, prod_conn, tmp_path: Path):
    dev_url = _db_url(dev_conn.info.dbname)
    prod_url = _db_url(prod_conn.info.dbname)
    _seed_series(dev_conn)
    _seed_series(prod_conn)
    _seed_concepts(dev_conn)
    _seed_concepts(prod_conn)
    dev_conn.commit()
    prod_conn.commit()

    git_cards = tmp_path / "cards"
    git_cards.mkdir()
    (git_cards / "git-seed.yaml").write_text(VALID_CARD_YAML)
    sync_index(dev_conn, FileCardRepository(git_cards))
    dev_conn.commit()

    dev_cards = DbCardRepository(dev_url)
    assert dev_cards.save("dyn-rate-card", VALID_CARD_YAML) == []
    dev_readings = DbReadingRepository(dev_url)
    assert dev_readings.save("policy-watch", VALID_READING_MD, origin="ingested") == []

    plan = transfer.plan_push(dev_conn, prod_conn)
    assert plan.cards == [("dyn-rate-card", "new")]
    assert plan.readings == [("policy-watch", "new")]

    pushed = transfer.apply_push(dev_conn, prod_conn, plan)
    assert pushed.cards == 1 and pushed.readings == 1
    assert "dyn-rate-card" in DbCardRepository(prod_url).ids()
    assert "policy-watch" in DbReadingRepository(prod_url).ids()

    # Delete dev dynamic items and confirm pull restores from prod without failing FKs.
    assert dev_readings.delete("policy-watch")
    assert dev_cards.delete("dyn-rate-card")
    pulled = transfer.pull(dev_conn, prod_conn, discard_local=True)
    assert pulled.rows["cards"] == 1
    assert pulled.rows["readings"] == 1
    assert "dyn-rate-card" in dev_cards.ids()
    assert "policy-watch" in dev_readings.ids()


def test_llm_selection_and_url_extraction():
    try:
        select_ingest_llm(Settings(_env_file=None, gemini_api_key=None, anthropic_api_key=None))
    except LLMError as exc:
        assert "GEMINI_API_KEY" in str(exc)
    else:
        raise AssertionError("expected LLMError when neither key is set")

    s_gem = Settings(_env_file=None, gemini_api_key="g-key", anthropic_api_key="a-key")
    assert configured_provider(s_gem) == "gemini"
    assert isinstance(select_ingest_llm(s_gem), GeminiIngestLLM)

    s_ant = Settings(
        _env_file=None,
        ingest_provider="anthropic",
        gemini_api_key="g-key",
        anthropic_api_key="a-key",
    )
    assert configured_provider(s_ant) == "anthropic"
    assert isinstance(select_ingest_llm(s_ant), AnthropicIngestLLM)

    html = """
    <html><head>
      <title>Ignored Site Title</title>
      <meta property="og:title" content="Rate Cycles Explained">
      <meta property="og:site_name" content="Macro Journal">
      <meta name="author" content="Alex Researcher">
      <meta property="article:published_time" content="2026-10-15T12:00:00Z">
    </head><body>
      <nav>Home | About</nav>
      <h2>Yield Dynamics</h2>
      <p>Long-term yields moved alongside demographics across advanced economies.</p>
      <blockquote>Real rates anchor capital allocation over multi-decade waves.</blockquote>
      <script>console.log("ignore me")</script>
      <footer>Copyright Notice</footer>
    </body></html>
    """
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text=html, headers={"content-type": "text/html"})
    )
    with httpx.Client(transport=transport) as client:
        extracted = fetch_url_article("https://example.com/rates", client=client)

    assert extracted["title"] == "Rate Cycles Explained"
    assert extracted["source"] == "Macro Journal"
    assert extracted["author"] == "Alex Researcher"
    assert extracted["published"] == "2026-10-15"
    assert "## Yield Dynamics" in extracted["text"]
    assert "ignore me" not in extracted["text"]
    assert "Home | About" not in extracted["text"]


def test_run_ingest_job_and_fallback(conn, tmp_path: Path):
    _seed_series(conn)
    _seed_concepts(conn)
    conn.commit()
    url = _db_url()

    settings = Settings(
        _env_file=None,
        database_url=url,
        storage="db",
        cards_path=tmp_path / "cards",
        readings_path=tmp_path / "readings",
        fred_api_key=None,
    )
    events: list[dict] = []
    job = create_job(conn, kind="text", title="Test Article")
    conn.commit()

    done = run_ingest_job(
        job["id"],
        settings=settings,
        kind="text",
        text="Policy rates tightened considerably across this macro cycle.",
        llm=_FakeLLM(valid_card=True),
        on_event=events.append,
    )
    assert done["status"] == "done"
    assert done["reading_id"] == "macro-rate-digest"
    assert done["card_ids"] == ["fedfunds-cycle"]
    assert any(e["event"] == "reading-changed" for e in events)

    # Also confirm JevDrafter fallback produces a valid card when LLM cards fail validation.
    job2 = create_job(conn, kind="text", title="Fallback Article")
    conn.commit()
    done2 = run_ingest_job(
        job2["id"],
        settings=settings,
        kind="text",
        text="Federal funds policy rates shifted sharply.",
        llm=_FakeLLM(valid_card=False),
    )
    assert done2["status"] == "done"
    assert len(done2["card_ids"]) >= 1


def test_api_ingest_and_delete_routes(conn, tmp_path: Path):
    _seed_series(conn)
    _seed_concepts(conn)
    conn.commit()
    url = _db_url()

    no_key_settings = Settings(
        _env_file=None,
        database_url=url,
        storage="db",
        cards_path=tmp_path / "cards",
        readings_path=tmp_path / "readings",
        gemini_api_key=None,
        anthropic_api_key=None,
    )
    client_no_key = TestClient(create_app(no_key_settings))
    assert client_no_key.get("/api/ingest/config").json() == {
        "storage": "db",
        "ingest_provider": None,
    }
    bad = client_no_key.post(
        "/api/ingest",
        json={"kind": "text", "text": "Enough prose to reach length requirement easily here."},
    )
    assert bad.status_code == 400 and "GEMINI_API_KEY" in bad.json()["detail"]

    client = TestClient(create_app(no_key_settings, ingest_llm=_FakeLLM(valid_card=True)))
    # Markdown import creates card + reading synchronously cleanly.
    r_md = client.post(
        "/api/ingest",
        json={
            "kind": "markdown",
            "slug": "direct-essay",
            "text": VALID_READING_MD,
            "cards_yaml": {"dyn-rate-card": VALID_CARD_YAML},
        },
    )
    assert r_md.status_code == 201, r_md.text
    assert r_md.json()["reading_id"] == "direct-essay"

    fetched = client.get("/api/readings/direct-essay")
    assert fetched.status_code == 200 and fetched.json()["origin"] == "imported"

    # Async text ingest finishes cleanly via get_job polling.
    r_async = client.post(
        "/api/ingest",
        json={"kind": "text", "text": "Policy rates moved noticeably across the entire cycle."},
    )
    assert r_async.status_code == 202, r_async.text
    job_id = r_async.json()["id"]
    for _ in range(40):
        status_payload = client.get(f"/api/ingest/{job_id}").json()
        if status_payload["status"] in ("done", "failed"):
            break
    assert status_payload["status"] == "done", status_payload

    assert get_job(conn, job_id)["status"] == "done"

    deleted = client.delete("/api/readings/direct-essay")
    assert deleted.status_code == 200
    assert client.get("/api/readings/direct-essay").status_code == 404

    guest_client = TestClient(create_app(no_key_settings, guest=True))
    assert (
        guest_client.post("/api/ingest", json={"kind": "text", "text": "x" * 60}).status_code == 403
    )
    assert guest_client.delete("/api/readings/macro-rate-digest").status_code == 403

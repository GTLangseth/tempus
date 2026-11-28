"""Async article digestion pipeline backed by Postgres (`ingest_jobs`, `readings`, `cards`)."""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import httpx
import psycopg
import yaml

from tempus import db, ontology
from tempus.cards.check import check_card
from tempus.cards.errors import has_errors as has_card_errors
from tempus.cards.repository import CardRepository, DbCardRepository
from tempus.cards.resolve import resolve as resolve_card
from tempus.cards.spec import CARD_ID, Card, parse_card
from tempus.config import Settings
from tempus.drafting import JevDrafter, card_yaml
from tempus.drafting import card_id as slug_for
from tempus.explore import SourceFactory, catalog_from_db
from tempus.filing import Chooser, file_series
from tempus.jev import JevClient, JevError
from tempus.llm import (
    EnrichmentQuery,
    ExtractedArticle,
    IngestLLM,
    ProposedClaim,
    fetch_url_article,
    select_ingest_llm,
)
from tempus.navigate import LexicalChooser, navigate
from tempus.readings import (
    READING_ID,
    DbReadingRepository,
    _series_snapshot,
    check_reading,
    parse_reading,
)
from tempus.readings import has_errors as has_reading_errors
from tempus.sources import SOURCE_NAMES, get_source
from tempus.sources.fred import FredError
from tempus.sync import mark_tracked, sync_series


def default_sync_source(settings: Settings) -> SourceFactory | None:
    """Sync-on-open / auto-ingest needs a provider key; None disables live FRED sync."""
    if not settings.fred_api_key:
        return None
    return lambda name: get_source(name, settings) if name in SOURCE_NAMES else None


_FENCE_STRIP = re.compile(r"```.*?```", re.DOTALL)
_FOOTNOTE_REF = re.compile(r"\[\^([A-Za-z0-9_-]+)\]")


def _serialize_job(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "title": row["title"],
        "source_url": row["source_url"],
        "status": row["status"],
        "step": row["step"],
        "error": row["error"],
        "reading_id": row["reading_id"],
        "card_ids": list(row["card_ids"] or []),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def create_job(
    conn: psycopg.Connection,
    *,
    kind: str,
    title: str | None = None,
    source_url: str | None = None,
    status: str = "running",
    step: str = "queued",
) -> dict:
    """Insert a new row in `ingest_jobs` and return its serialized payload."""
    job_id = uuid.uuid4()
    row = conn.execute(
        """
        INSERT INTO ingest_jobs (id, kind, title, source_url, status, step)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (job_id, kind, title, source_url, status, step),
    ).fetchone()
    return _serialize_job(row)


def update_job(conn: psycopg.Connection, job_id: str | uuid.UUID, **fields: object) -> dict:
    """Update allowed fields on `ingest_jobs` and return the updated job dict."""
    allowed = {"title", "source_url", "status", "step", "error", "reading_id", "card_ids"}
    updates = [(k, v) for k, v in fields.items() if k in allowed]
    sets = ", ".join(f"{k} = %s" for k, _ in updates)
    sql_str = (
        f"UPDATE ingest_jobs SET {sets}, updated_at = now() WHERE id = %s RETURNING *"
        if sets
        else "UPDATE ingest_jobs SET updated_at = now() WHERE id = %s RETURNING *"
    )
    params = [v for _, v in updates] + [uuid.UUID(str(job_id))]
    row = conn.execute(sql_str, params).fetchone()
    if row is None:
        raise KeyError(f"no ingest job '{job_id}'")
    return _serialize_job(row)


def get_job(conn: psycopg.Connection, job_id: str | uuid.UUID) -> dict | None:
    try:
        parsed = uuid.UUID(str(job_id))
    except ValueError:
        return None
    row = conn.execute("SELECT * FROM ingest_jobs WHERE id = %s", (parsed,)).fetchone()
    return _serialize_job(row) if row is not None else None


def list_jobs(conn: psycopg.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM ingest_jobs ORDER BY created_at DESC LIMIT %s", (limit,)
    ).fetchall()
    return [_serialize_job(r) for r in rows]


def _emit(on_event: Callable[[dict], None] | None, payload: dict) -> None:
    if on_event is None:
        return
    with contextlib.suppress(Exception):
        on_event(payload)


def _unique_reading_id(candidate: str, existing: set[str]) -> str:
    base = candidate.strip().lower()
    if not READING_ID.fullmatch(base):
        base = slug_for(candidate, set())
    if not READING_ID.fullmatch(base):
        base = "article"
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def ingest_markdown_direct(
    *,
    settings: Settings,
    markdown_text: str,
    slug: str | None = None,
    cards_yaml: dict[str, str] | None = None,
    status: str = "published",
    origin: str = "imported",
    on_event: Callable[[dict], None] | None = None,
) -> dict:
    """Validate and save a complete Markdown reading (+ optional cards YAML) into Postgres."""
    cards_repo = DbCardRepository(settings.database_url, fallback_root=settings.cards_path)
    readings_repo = DbReadingRepository(settings.database_url, fallback_root=settings.readings_path)
    saved_cards: list[str] = []

    with db.connect(settings.database_url) as conn:
        job = create_job(conn, kind="markdown", title=slug, step="saving")
        conn.commit()
    _emit(on_event, {"event": "ingest-progress", "job": job})

    try:
        for cid, yaml_text in (cards_yaml or {}).items():
            if not CARD_ID.fullmatch(cid):
                raise ValueError(f"card id '{cid}' must be kebab-case")
            errs = cards_repo.save(cid, yaml_text)
            if has_card_errors(errs):
                first = next(e for e in errs if e.level == "error")
                raise ValueError(f"card '{cid}': {first.path} — {first.message}")
            saved_cards.append(cid)
            _emit(on_event, {"event": "card-changed", "card": cid})

        probe, parse_errs = parse_reading(slug or "incoming-reading", markdown_text)
        if probe is None or has_reading_errors(parse_errs):
            msg = "; ".join(e.format() for e in parse_errs if e.level == "error")
            raise ValueError(msg or "invalid reading Markdown")

        if slug:
            target_id = slug.strip()
        else:
            target_id = _unique_reading_id(probe.meta.title, set(readings_repo.ids()))
        if not READING_ID.fullmatch(target_id):
            raise ValueError(f"reading id '{target_id}' must be kebab-case")

        with db.connect(settings.database_url) as conn:
            known = {r["id"] for r in conn.execute("SELECT id FROM concepts")} or None
            reading, _ = parse_reading(target_id, markdown_text)
            assert reading is not None
            check_errs = check_reading(
                target_id, reading, cards_repo, conn=conn, known_concepts=known
            )
            if has_reading_errors(check_errs):
                msg = "; ".join(e.format() for e in check_errs if e.level == "error")
                raise ValueError(msg)

        save_errs = readings_repo.save(target_id, markdown_text, status=status, origin=origin)
        if has_reading_errors(save_errs):
            msg = "; ".join(e.format() for e in save_errs if e.level == "error")
            raise ValueError(msg)

        all_card_ids = sorted(set(saved_cards) | set(reading.card_ids()))
        with db.connect(settings.database_url) as conn:
            job = update_job(
                conn,
                job["id"],
                title=reading.meta.title,
                source_url=reading.meta.url,
                status="done",
                step="complete",
                reading_id=target_id,
                card_ids=all_card_ids,
            )
            conn.commit()
        _emit(on_event, {"event": "reading-changed", "reading": target_id})
        _emit(on_event, {"event": "ingest-progress", "job": job})
        return job
    except Exception as exc:
        with db.connect(settings.database_url) as conn:
            job = update_job(
                conn,
                job["id"],
                status="failed",
                step="failed",
                error=str(exc),
                card_ids=saved_cards,
            )
            conn.commit()
        _emit(on_event, {"event": "ingest-progress", "job": job})
        raise


@dataclass
class _AcceptedCard:
    id: str
    card: Card
    anchor_heading: str | None
    caption: str | None
    note_title: str | None
    note_body: str | None
    claim: ProposedClaim | None


def _resolve_candidates(
    conn: psycopg.Connection,
    settings: Settings,
    queries: list[EnrichmentQuery],
    *,
    chooser: Chooser | None,
    sync_factory: SourceFactory | None,
) -> tuple[list[dict], list[tuple[str, str]]]:
    """Find stored series matching essay queries, auto-syncing FRED when local coverage is thin."""
    catalog = catalog_from_db(conn)
    base_chooser = chooser
    if base_chooser is None and settings.typesafe_api_key:
        base_chooser = JevClient(settings.typesafe_api_key)

    refs: list[str] = []
    seen: set[str] = set()
    synced_new: list[tuple[str, str]] = []

    def add_ref(ref: str) -> None:
        if ref not in seen and len(refs) < 12:
            seen.add(ref)
            refs.append(ref)

    fred = None
    if sync_factory is not None:
        fred = sync_factory("fred")

    for q in queries[:5]:
        local_hits: list[dict] = []
        if base_chooser is not None:
            try:
                local_hits = navigate(q.search_query, catalog, base_chooser).series
            except JevError:
                local_hits = navigate(q.search_query, catalog, LexicalChooser(catalog)).series
        else:
            local_hits = navigate(q.search_query, catalog, LexicalChooser(catalog)).series

        strong = [s for s in local_hits if (s.get("score") or 0) >= 0.08][:2]
        for s in strong:
            add_ref(f"{s['source']}:{s['series_id']}")

        if len(strong) < 2 and fred is not None:
            try:
                found = fred.search(q.search_query, limit=4)
            except (FredError, httpx.HTTPError, RuntimeError, ValueError):
                found = []
            added_here = 0
            for meta in found:
                ref = f"fred:{meta.series_id}"
                if ref in seen:
                    continue
                row = conn.execute(
                    "SELECT last_synced_at FROM series WHERE source = 'fred' AND series_id = %s",
                    (meta.series_id,),
                ).fetchone()
                if row is None or row["last_synced_at"] is None:
                    res = sync_series(conn, fred, meta.series_id)
                    if res.status == "failed":
                        continue
                    mark_tracked(conn, "fred", meta.series_id, True)
                    synced_new.append(("fred", meta.series_id))
                add_ref(ref)
                added_here += 1
                if added_here >= 2:
                    break

    if synced_new and settings.ontology_path.exists():
        try:
            roots = ontology.load(settings.ontology_path)
            filer = base_chooser or LexicalChooser(catalog_from_db(conn))
            list(file_series(conn, roots, filer))
        except Exception:
            pass

    conn.commit()
    return _series_ground_truth(conn, refs), synced_new


def _series_ground_truth(conn: psycopg.Connection, refs: list[str]) -> list[dict]:
    if not refs:
        return []
    snap = _series_snapshot(conn, refs)
    extra_rows = conn.execute(
        """
        SELECT s.source || ':' || s.series_id AS ref, s.frequency,
               min(o.ts) AS first_ts, count(o.ts) AS obs_count
        FROM series s
        LEFT JOIN observations o USING (source, series_id)
        WHERE s.source || ':' || s.series_id = ANY(%s)
        GROUP BY s.source, s.series_id, s.frequency
        """,
        (refs,),
    ).fetchall()
    by_ref = {r["ref"]: r for r in extra_rows}
    out = []
    for ref in refs:
        if ref not in snap:
            continue
        item = dict(snap[ref])
        extra = by_ref.get(ref, {})
        item["frequency"] = extra.get("frequency")
        item["start_date"] = extra["first_ts"].date().isoformat() if extra.get("first_ts") else None
        item["observations"] = int(extra.get("obs_count") or 0)
        if item["observations"] > 0:
            out.append(item)
    return out


def _validate_and_accept_cards(
    conn: psycopg.Connection,
    cards_repo: CardRepository,
    composed: ExtractedArticle | None,
    enrichment: list,
    queries: list[EnrichmentQuery],
    known_concepts: set[str],
    *,
    chooser: Chooser | None,
    sync_factory: SourceFactory | None,
) -> list[_AcceptedCard]:
    existing_ids = set(cards_repo.ids())
    accepted: list[_AcceptedCard] = []
    stored_refs = {
        r["ref"]
        for r in conn.execute(
            "SELECT source || ':' || series_id AS ref FROM series WHERE last_synced_at IS NOT NULL"
        )
    }

    for item in enrichment:
        raw = dict(item.spec)
        raw.setdefault("version", 1)
        if isinstance(raw.get("concepts"), list):
            raw["concepts"] = [c for c in raw["concepts"] if c in known_concepts]
        cid = slug_for(item.id, existing_ids)
        card, errs = parse_card(cid, raw)
        if card is not None:
            errs = [*errs, *check_card(cid, card)[1]]
        if card is not None and not has_card_errors(errs):
            errs = [*errs, *resolve_card(conn, cid, card)[1]]
        if card is None or has_card_errors(errs):
            continue

        claim = item.claim
        if claim is not None:
            valid_series = [s for s in claim.series if s in stored_refs]
            if not valid_series and claim.status != "unverified":
                claim = None
            else:
                claim = ProposedClaim(
                    quote=claim.quote,
                    status=claim.status,
                    verdict=claim.verdict,
                    series=valid_series,
                )

        existing_ids.add(cid)
        accepted.append(
            _AcceptedCard(
                id=cid,
                card=card,
                anchor_heading=item.anchor_heading,
                caption=item.caption,
                note_title=item.note_title,
                note_body=item.note_body,
                claim=claim,
            )
        )

    if not accepted:
        # Deterministic JevDrafter fallback when LLM card composition failed validation.
        catalog = catalog_from_db(conn)
        draft_chooser = chooser
        if draft_chooser is None:
            draft_chooser = LexicalChooser(catalog)
        drafter = JevDrafter(conn, draft_chooser, sync_source=sync_factory)
        fallback_queries = queries[:2] or (
            [
                EnrichmentQuery(
                    topic=composed.title,
                    claim_quote=composed.summary,
                    search_query=composed.title,
                )
            ]
            if composed
            else []
        )
        for q in fallback_queries:
            try:
                draft = drafter.draft(q.search_query)
            except JevError:
                draft = JevDrafter(conn, LexicalChooser(catalog), sync_source=sync_factory).draft(
                    q.search_query
                )
            if draft.card is None:
                continue
            cid = slug_for(draft.card.title, existing_ids)
            errs = resolve_card(conn, cid, draft.card)[1]
            if has_card_errors(errs):
                continue
            for ref in draft.synced:
                src, _, sid = ref.partition(":")
                mark_tracked(conn, src, sid, True)
            existing_ids.add(cid)
            accepted.append(
                _AcceptedCard(
                    id=cid,
                    card=draft.card,
                    anchor_heading=q.anchor_heading,
                    caption=f"Empirical context for: {q.topic}.",
                    note_title=None,
                    note_body=None,
                    claim=None,
                )
            )
        conn.commit()
    return accepted


def _block_markdown(card: _AcceptedCard) -> str:
    parts: list[str] = []
    if card.claim is not None:
        claim_doc: dict[str, object] = {
            "quote": card.claim.quote,
            "status": card.claim.status,
            "verdict": card.claim.verdict,
        }
        if card.claim.series:
            claim_doc["series"] = card.claim.series
        claim_doc["card"] = card.id
        dumped = yaml.safe_dump(claim_doc, sort_keys=False, allow_unicode=True).strip()
        parts.append(f"```claim\n{dumped}\n```")

    embed_doc: dict[str, str] = {"card": card.id}
    if card.caption:
        embed_doc["caption"] = card.caption
    embed_yaml = yaml.safe_dump(embed_doc, sort_keys=False, allow_unicode=True).strip()
    parts.append(f"```card\n{embed_yaml}\n```")

    if card.note_body:
        title_line = f"title: {card.note_title}\n\n" if card.note_title else ""
        parts.append(f"```note\n{title_line}{card.note_body.strip()}\n```")
    return "\n\n".join(parts)


def _assemble_reading_markdown(
    article: ExtractedArticle,
    cards: list[_AcceptedCard],
    known_concepts: set[str],
    source_url: str | None,
    extra_footnotes: list[dict[str, str]],
) -> str:
    clean_body = _FENCE_STRIP.sub("", article.body_markdown).strip()
    # Strip any existing leading YAML frontmatter if the LLM emitted one anyway.
    if clean_body.startswith("---\n"):
        parts = clean_body.split("---\n", 2)
        if len(parts) == 3:
            clean_body = parts[2].strip()

    # Split body into sections headed by `## ...` so cards interleave near their target sections.
    lines = clean_body.splitlines()
    sections: list[tuple[str | None, list[str]]] = []
    cur_heading: str | None = None
    cur_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            sections.append((cur_heading, cur_lines))
            cur_heading = line[3:].strip()
            cur_lines = [line]
        else:
            cur_lines.append(line)
    sections.append((cur_heading, cur_lines))

    by_section: dict[int, list[_AcceptedCard]] = {}
    unanchored: list[_AcceptedCard] = []
    for card in cards:
        placed = False
        if card.anchor_heading:
            needle = card.anchor_heading.lower().strip()
            for idx, (h, _) in enumerate(sections):
                if h and (needle in h.lower() or h.lower() in needle):
                    by_section.setdefault(idx, []).append(card)
                    placed = True
                    break
        if not placed:
            unanchored.append(card)

    rendered_sections: list[str] = []
    for idx, (_h, sec_lines) in enumerate(sections):
        sec_text = "\n".join(sec_lines).strip()
        chunks = [sec_text] if sec_text else []
        for card in by_section.get(idx, []):
            chunks.append(_block_markdown(card))
        if chunks:
            rendered_sections.append("\n\n".join(chunks))

    if unanchored:
        appendix = ["## Empirical Context & Series"]
        appendix.extend(_block_markdown(card) for card in unanchored)
        rendered_sections.append("\n\n".join(appendix))

    body_text = "\n\n".join(rendered_sections).strip()

    fn_map: dict[str, str] = {}
    for entry in [*article.footnotes, *extra_footnotes]:
        fid = str(entry.get("id", "")).strip()
        ftext = str(entry.get("text", "")).strip()
        if fid and ftext and re.fullmatch(r"[A-Za-z0-9_-]+", fid):
            fn_map.setdefault(fid, ftext)

    # Remove undefined footnote markers to guarantee `check_reading` never sees missing definitions.
    body_text = _FOOTNOTE_REF.sub(lambda m: m.group(0) if m.group(1) in fn_map else "", body_text)
    used_fn_ids: list[str] = []
    for m in _FOOTNOTE_REF.finditer(body_text):
        fid = m.group(1)
        if fid in fn_map and fid not in used_fn_ids:
            used_fn_ids.append(fid)

    if used_fn_ids:
        fn_block = "\n".join(f"[^{fid}]: {fn_map[fid]}" for fid in used_fn_ids)
        body_text = f"{body_text}\n\n{fn_block}"

    all_concepts = sorted(
        {
            *(c for c in article.concepts if c in known_concepts),
            *(c for card in cards for c in card.card.concepts if c in known_concepts),
        }
    )
    meta: dict[str, object] = {
        "version": 1,
        "title": article.title.strip(),
        "authors": [a.strip() for a in article.authors if a.strip()] or ["Unknown Author"],
        "published": article.published.strip() or "2026",
        "summary": article.summary.strip(),
        "concepts": all_concepts,
        "tags": list(dict.fromkeys(t.strip().lower() for t in article.tags if t.strip()))[:8],
    }
    if article.source:
        meta["source"] = article.source.strip()
    if source_url:
        meta["url"] = source_url

    frontmatter = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{frontmatter}\n---\n\n{body_text}\n"


def run_ingest_job(
    job_id: str | uuid.UUID,
    *,
    settings: Settings,
    kind: Literal["pdf", "url", "text"],
    pdf_bytes: bytes | None = None,
    url: str | None = None,
    text: str | None = None,
    title_hint: str | None = None,
    llm: IngestLLM | None = None,
    sync_source: SourceFactory | None = None,
    chooser: Chooser | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> dict:
    """Run extraction, FRED resolution/sync, SQL-grounded chart composition, and DB save."""
    cards_repo = DbCardRepository(settings.database_url, fallback_root=settings.cards_path)
    readings_repo = DbReadingRepository(settings.database_url, fallback_root=settings.readings_path)
    sync_factory = sync_source or default_sync_source(settings)

    def advance(step: str, *, status: str = "running", **fields: object) -> dict:
        with db.connect(settings.database_url) as conn:
            job = update_job(conn, job_id, step=step, status=status, **fields)
            conn.commit()
        _emit(on_event, {"event": "ingest-progress", "job": job})
        return job

    try:
        active_llm = llm or select_ingest_llm(settings)
        advance("extracting")

        with db.connect(settings.database_url) as conn:
            known_concepts = {r["id"] for r in conn.execute("SELECT id FROM concepts")}
        known_list = sorted(known_concepts)

        source_url = url
        extracted_text = text
        inferred_title = title_hint
        if kind == "url":
            if not url:
                raise ValueError("URL ingestion requires `url`")
            fetched = fetch_url_article(url)
            source_url = fetched["url"]
            extracted_text = fetched["text"]
            inferred_title = title_hint or fetched.get("title")
        elif kind == "pdf":
            if not pdf_bytes:
                raise ValueError("PDF ingestion requires `pdf_bytes`")
        elif kind == "text":
            if not text or not text.strip():
                raise ValueError("Text ingestion requires non-empty `text`")

        article = active_llm.extract_article(
            pdf_bytes=pdf_bytes if kind == "pdf" else None,
            text=None if kind == "pdf" else extracted_text,
            source_url=source_url,
            title_hint=inferred_title,
            known_concepts=known_list,
        )
        advance("resolving", title=article.title, source_url=source_url)

        queries = article.queries or [
            EnrichmentQuery(
                topic=article.title,
                claim_quote=article.summary,
                search_query=article.title,
            )
        ]

        with db.connect(settings.database_url) as conn:
            series_context, _synced = _resolve_candidates(
                conn,
                settings,
                queries,
                chooser=chooser,
                sync_factory=sync_factory,
            )

        advance("composing")
        with db.connect(settings.database_url) as conn:
            composed = active_llm.compose_enrichment(
                article=article,
                series_context=series_context,
                known_concepts=known_list,
            )
            accepted = _validate_and_accept_cards(
                conn,
                cards_repo,
                article,
                composed.cards,
                queries,
                known_concepts,
                chooser=chooser,
                sync_factory=sync_factory,
            )

        advance("saving")
        saved_card_ids: list[str] = []
        with db.connect(settings.database_url) as conn:
            for item in accepted:
                errs = cards_repo.save(item.id, card_yaml(item.card))
                if not has_card_errors(errs):
                    saved_card_ids.append(item.id)
                    _emit(on_event, {"event": "card-changed", "card": item.id})
                    for series_ref in item.card.inputs.values():
                        if ":" in str(series_ref):
                            src, _, sid = str(series_ref).partition(":")
                            mark_tracked(conn, src, sid, True)
            conn.commit()

        reading_id = _unique_reading_id(article.slug or article.title, set(readings_repo.ids()))
        final_md = _assemble_reading_markdown(
            article,
            [c for c in accepted if c.id in saved_card_ids],
            known_concepts,
            source_url,
            composed.extra_footnotes,
        )
        save_errs = readings_repo.save(reading_id, final_md, status="published", origin="ingested")
        if has_reading_errors(save_errs):
            msg = "; ".join(e.format() for e in save_errs if e.level == "error")
            raise ValueError(f"Generated reading failed validation: {msg}")

        _emit(on_event, {"event": "reading-changed", "reading": reading_id})
        return advance(
            "complete",
            status="done",
            reading_id=reading_id,
            card_ids=saved_card_ids,
        )
    except Exception as exc:
        with db.connect(settings.database_url) as conn:
            job = update_job(conn, job_id, status="failed", step="failed", error=str(exc))
            conn.commit()
        _emit(on_event, {"event": "ingest-progress", "job": job})
        return job

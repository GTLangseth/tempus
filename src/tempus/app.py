"""`tempus serve`: the engine API plus the static front end, with hot reload.

Every read goes through the card engine; the browser never touches formulas or SQL.
"""

import asyncio
import base64
import binascii
import contextlib
import json
import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Literal

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from watchfiles import awatch

from tempus import db
from tempus.cards.check import check_card
from tempus.cards.errors import has_errors
from tempus.cards.repository import DbCardRepository, FileCardRepository
from tempus.cards.run import run_card, run_spec
from tempus.cards.spec import CARD_ID, parse_card
from tempus.compare import MODES, compare_card, correlation, default_mode, modes
from tempus.config import Settings, get_settings
from tempus.drafting import SHAPES, Draft, JevDrafter, Pick, card_yaml, series_meta
from tempus.drafting import card_id as card_id_for
from tempus.explore import SourceFactory, catalog_from_db, explore_payload, series_page
from tempus.filing import Chooser
from tempus.ingest import (
    create_job,
    default_sync_source,
    get_job,
    ingest_markdown_direct,
    list_jobs,
    run_ingest_job,
)
from tempus.jev import ChoiceAnswer, JevClient, JevError
from tempus.llm import IngestLLM, LLMError, configured_provider, select_ingest_llm
from tempus.navigate import LexicalChooser, navigate
from tempus.readings import (
    READING_ID,
    DbReadingRepository,
    FileReadingRepository,
    check_reading,
    run_reading,
)
from tempus.readings import has_errors as has_reading_errors

MAX_PDF_BYTES = 25 * 1024 * 1024

WEB_DIR = Path(__file__).with_name("web")


def card_event(repo: FileCardRepository, path: Path) -> dict | None:
    # Editors' temp and backup files (.#card.yaml, card.tmp.yaml) aren't cards.
    if path.suffix != ".yaml" or not CARD_ID.fullmatch(path.stem):
        return None
    card_id = path.stem
    if not path.exists():
        return {"event": "card-removed", "card": card_id}
    card, errors = repo.get(card_id)
    if card is not None:
        errors += check_card(card_id, card)[1]
    if has_errors(errors):
        return {"event": "card-invalid", "card": card_id, "errors": [e.to_dict() for e in errors]}
    return {"event": "card-changed", "card": card_id}


def reading_event(
    readings_repo: FileReadingRepository, cards_repo: FileCardRepository, path: Path
) -> dict | None:
    if path.suffix != ".md" or not READING_ID.fullmatch(path.stem):
        return None
    reading_id = path.stem
    if not path.exists():
        return {"event": "reading-removed", "reading": reading_id}
    reading, errors = readings_repo.get(reading_id)
    if reading is not None:
        errors = [*errors, *check_reading(reading_id, reading, cards_repo)]
    if has_reading_errors(errors):
        return {
            "event": "reading-invalid",
            "reading": reading_id,
            "errors": [e.to_dict() for e in errors],
        }
    return {"event": "reading-changed", "reading": reading_id}


LEXICAL = "lexical fallback (no TYPESAFE_API_KEY)"


class DraftRequest(BaseModel):
    question: str = Field(max_length=300)


class RunnerUp(BaseModel):
    series: str
    title: str | None = None
    probability: float


class PickIn(BaseModel):
    role: str
    series: str
    probability: float
    runners_up: list[RunnerUp] = []


class AssembleRequest(DraftRequest):
    shape: str
    shape_p: float
    picks: list[PickIn]


class CompareRequest(BaseModel):
    series: list[str]
    mode: str | None = None


class KeepRequest(BaseModel):
    card: dict


class IngestRequest(BaseModel):
    kind: Literal["pdf", "url", "text", "markdown"]
    title: str | None = Field(default=None, max_length=300)
    url: str | None = Field(default=None, max_length=2000)
    text: str | None = None
    pdf_base64: str | None = None
    filename: str | None = Field(default=None, max_length=300)
    slug: str | None = Field(default=None, max_length=120)
    cards_yaml: dict[str, str] | None = None


class _EventHub:
    """Thread-safe fanout of SSE payloads to connected /api/events listeners."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict]]] = []

    def subscribe(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue()
        sub = (asyncio.get_running_loop(), q)
        with self._lock:
            self._subs.append(sub)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict]) -> None:
        with self._lock:
            self._subs = [(loop, sub_q) for loop, sub_q in self._subs if sub_q is not q]

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(q.put_nowait, event)


def _question(text: str) -> str:
    if not text.strip():
        raise HTTPException(422, "ask a question")
    return text.strip()


def draft_body(conn: psycopg.Connection, draft: Draft, chooser: str) -> dict:
    """A draft as /api/draft and /api/draft/assemble answer it, with the card run."""
    refs = [ref for p in draft.picks for ref in (p.series, *(r for r, _ in p.runners_up))]
    rows = conn.execute(
        "SELECT source || ':' || series_id AS ref, title FROM series"
        " WHERE source || ':' || series_id = ANY(%s)",
        (refs,),
    )
    titles = {r["ref"]: r["title"] for r in rows}
    card = draft.card
    result = run_spec(conn, "draft", card) if card is not None else None
    return {
        "question": draft.question,
        "shape": {
            "name": draft.shape.choice,
            "p": draft.shape.probabilities[draft.shape.choice],
            "ranked": [list(kv) for kv in draft.shape.ranked()[:3]],
        },
        "picks": [
            {
                "role": p.role,
                "series": p.series,
                "title": titles.get(p.series),
                "probability": p.probability,
                "runners_up": [
                    {"series": r, "title": titles.get(r), "probability": rp}
                    for r, rp in p.runners_up
                ],
            }
            for p in draft.picks
        ],
        "card": card.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        if card is not None
        else None,
        "yaml": card_yaml(card) if card is not None else None,
        "payloads": result.payloads if result else [],
        "errors": [e.to_dict() for e in result.errors] if result else [],
        "error": draft.error,
        "synced": draft.synced,
        "chooser": chooser,
    }


def create_app(
    settings: Settings | None = None,
    sync_source: SourceFactory | None = None,
    guest: bool = False,
    read_only: bool = False,
    chooser: Chooser | None = None,
    ingest_llm: IngestLLM | None = None,
) -> FastAPI:
    """`read_only=True` never writes to the database: series pages and drafts don't sync.
    Reading prod through the tunnel (TEMPUS_DB=prod) is always read-only. Keep still writes
    cards/ locally there. `guest=True` is the shared (Funnel) app: it never writes, drafts,
    ingests or keeps, and has no API docs."""
    settings = settings or get_settings()
    is_remote = settings.db == "prod"
    read_only = guest or read_only or is_remote
    sync_source = None if read_only else sync_source or default_sync_source(settings)
    if settings.uses_db_storage:
        repo = DbCardRepository(settings.database_url, fallback_root=settings.cards_path)
        readings_repo = DbReadingRepository(
            settings.database_url, fallback_root=settings.readings_path
        )
    else:
        repo = FileCardRepository(settings.cards_path)
        readings_repo = FileReadingRepository(settings.readings_path)
    file_cards_repo = FileCardRepository(settings.cards_path)
    file_readings_repo = FileReadingRepository(settings.readings_path)
    hub = _EventHub()
    docs = {"docs_url": None, "redoc_url": None, "openapi_url": None} if guest else {}
    app = FastAPI(title="tempus", **docs)

    def reading_summaries() -> list[dict]:
        out = []
        for reading_id in readings_repo.ids():
            reading, errors = readings_repo.get(reading_id)
            if reading is not None:
                errors = [*errors, *check_reading(reading_id, reading, repo)]
            info = readings_repo.status_info(reading_id)
            out.append(
                {
                    "id": reading_id,
                    "title": reading.meta.title if reading else reading_id,
                    "authors": reading.meta.authors if reading else [],
                    "source": reading.meta.source if reading else None,
                    "published": reading.meta.published if reading else None,
                    "summary": reading.meta.summary if reading else None,
                    "concepts": reading.meta.concepts if reading else [],
                    "tags": reading.meta.tags if reading else [],
                    "cards": len(reading.card_ids()) if reading else 0,
                    "word_count": reading.word_count() if reading else 0,
                    "reading_minutes": reading.reading_minutes() if reading else 1,
                    "claim_counts": reading.claim_counts() if reading else {},
                    "origin": info["origin"] if info else "file",
                    "status": info["status"] if info else "published",
                    "errors": sum(e.level == "error" for e in errors),
                }
            )
        return out

    def summaries() -> list[dict]:
        out = []
        for card_id in repo.ids():
            card, errors = repo.get(card_id)
            if card is not None:
                errors += check_card(card_id, card)[1]
            out.append(
                {
                    "id": card_id,
                    "title": card.title if card else card_id,
                    "question": card.question if card else None,
                    "concepts": card.concepts if card else [],
                    "tags": card.tags if card else [],
                    "errors": sum(e.level == "error" for e in errors),
                }
            )
        return out

    @app.get("/api/meta")
    def meta() -> dict:
        return {"read_only": read_only, "remote": is_remote, "guest": guest}

    @app.get("/api/cards")
    def list_cards() -> list[dict]:
        return summaries()

    @app.get("/api/cards/{card_id}")
    def get_card(card_id: str, start: date | None = None, end: date | None = None) -> dict:
        if card_id not in repo.ids():
            raise HTTPException(404, f"no card '{card_id}'")
        with db.connect(settings.database_url) as conn:
            return run_card(conn, repo, card_id, start, end).to_dict()

    @app.get("/api/readings")
    def list_readings() -> list[dict]:
        return reading_summaries()

    @app.get("/api/readings/{reading_id}")
    def get_reading(reading_id: str) -> dict:
        if reading_id not in readings_repo.ids():
            raise HTTPException(404, f"no reading '{reading_id}'")
        with db.connect(settings.database_url) as conn:
            rows = conn.execute("SELECT id FROM concepts").fetchall()
            known = {r["id"] for r in rows} or None
            payload = run_reading(conn, readings_repo, repo, reading_id, known_concepts=known)
        info = readings_repo.status_info(reading_id)
        payload["origin"] = info["origin"] if info else "file"
        payload["status"] = info["status"] if info else "published"
        return payload

    @app.get("/api/concepts")
    def concepts() -> dict:
        with db.connect(settings.database_url) as conn:
            rows = conn.execute(
                """
                SELECT c.id, c.parent_id, c.name, c.position,
                       (SELECT count(*) FROM series_concepts sc WHERE sc.concept_id = c.id)
                           AS series
                FROM concepts c ORDER BY c.position, c.id
                """
            ).fetchall()
        cards = summaries()
        nodes = {
            r["id"]: {
                "id": r["id"],
                "name": r["name"],
                "series": r["series"],
                "cards": [c["id"] for c in cards if r["id"] in c["concepts"]],
                "children": [],
            }
            for r in rows
        }
        tree = []
        for r in rows:
            (nodes[r["parent_id"]]["children"] if r["parent_id"] else tree).append(nodes[r["id"]])
        unfiled = [c["id"] for c in cards if not any(k in nodes for k in c["concepts"])]
        return {"tree": tree, "unfiled_cards": unfiled}

    @app.get("/api/explore")
    def explore() -> dict:
        with db.connect(settings.database_url) as conn:
            return explore_payload(conn)

    @app.get("/api/navigate")
    def navigate_route(q: str = Query(..., max_length=300)) -> dict:
        query = q.strip()
        if not query:
            raise HTTPException(422, "say what you're looking for")
        with db.connect(settings.database_url) as conn:
            catalog = catalog_from_db(conn)
        if settings.typesafe_api_key:
            try:
                jev = JevClient(settings.typesafe_api_key)
                return navigate(query, catalog, jev, label="jev").to_dict(catalog)
            except JevError as exc:
                label = f"lexical fallback (Jev failed: {str(exc)[:120]})"
                return navigate(query, catalog, LexicalChooser(catalog), label=label).to_dict(
                    catalog
                )
        return navigate(query, catalog, LexicalChooser(catalog), label=LEXICAL).to_dict(catalog)

    def drafting(conn: psycopg.Connection, run: Callable[[JevDrafter], Draft]) -> dict:
        """`run(drafter)` with Jev, or the lexical chooser without a key or when Jev fails,
        as /api/navigate does."""
        jev = chooser
        if jev is None and settings.typesafe_api_key:
            jev = JevClient(settings.typesafe_api_key)
        if jev is not None:
            try:
                draft = run(JevDrafter(conn, jev, sync_source=sync_source))
                return draft_body(conn, draft, "jev")
            except JevError:
                pass
        lexical = LexicalChooser(catalog_from_db(conn))
        draft = run(JevDrafter(conn, lexical, sync_source=sync_source))
        return draft_body(conn, draft, "lexical fallback")

    def no_guest() -> None:
        if guest:
            raise HTTPException(403, "the shared app can't draft or keep cards")

    def no_write() -> None:
        no_guest()
        if read_only:
            raise HTTPException(403, "read-only mode: cannot modify readings or cards")

    @app.post("/api/draft", dependencies=[Depends(no_guest)])
    def draft_route(req: DraftRequest) -> dict:
        question = _question(req.question)
        with db.connect(settings.database_url) as conn:
            return drafting(conn, lambda d: d.draft(question))

    @app.post("/api/draft/assemble", dependencies=[Depends(no_guest)])
    def assemble_route(req: AssembleRequest) -> dict:
        """Rebuild the card from picks the client chose. Runners-up the client sends back
        are returned unchanged (empty when it sends none), and `shape.ranked` holds only the
        given shape; the client keeps its own lists from /api/draft."""
        question = _question(req.question)
        shape = SHAPES.get(req.shape)
        if shape is None:
            raise HTTPException(422, f"no shape '{req.shape}'")
        by_role = {p.role: p for p in req.picks}
        if len(req.picks) != len(shape.roles) or set(by_role) != set(shape.roles):
            raise HTTPException(422, f"{shape.name} needs one pick per role: {shape.roles}")
        ordered = [by_role[role] for role in shape.roles]
        picks = [
            Pick(p.role, p.series, p.probability, [(r.series, r.probability) for r in p.runners_up])
            for p in ordered
        ]
        answer = ChoiceAnswer(shape.name, {shape.name: req.shape_p})
        with db.connect(settings.database_url) as conn:
            return drafting(conn, lambda d: d.assemble(question, answer, picks))

    @app.post("/api/compare")
    def compare_route(req: CompareRequest) -> dict:
        """Read only, so guest and read-only apps may compare."""
        refs = req.series
        if not 2 <= len(refs) <= 6:
            raise HTTPException(422, "compare 2 to 6 series")
        if len(set(refs)) != len(refs):
            raise HTTPException(422, "each series once")
        if req.mode is not None and req.mode not in MODES:
            raise HTTPException(422, f"no mode '{req.mode}': one of {list(MODES)}")
        with db.connect(settings.database_url) as conn:
            meta = {ref: series_meta(conn, ref) for ref in refs}
            if missing := [ref for ref, row in meta.items() if row is None]:
                raise HTTPException(422, f"no series {missing[0]}")
            card = compare_card(refs, meta, req.mode)
            result = run_spec(conn, "compare", card)
            matrix = correlation(conn, refs)
        return {
            "series": [
                {"series": ref, **{k: meta[ref][k] for k in ("title", "units", "frequency")}}
                for ref in refs
            ],
            "mode": req.mode or default_mode(meta),
            "modes": modes(meta),
            "card": card.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
            "yaml": card_yaml(card),
            "payloads": result.payloads,
            "errors": [e.to_dict() for e in result.errors],
            "correlation": matrix,
        }

    @app.post("/api/cards", status_code=201, dependencies=[Depends(no_guest)])
    def keep_route(req: KeepRequest):
        card, errors = parse_card("draft", req.card)
        if card is not None:
            errors += check_card("draft", card)[1]
        if card is None or has_errors(errors):
            return JSONResponse({"errors": [e.to_dict() for e in errors]}, status_code=400)
        new_id = card_id_for(card.title, set(repo.ids()))
        rendered = card_yaml(card)
        if settings.uses_db_storage and not is_remote:
            save_errors = repo.save(new_id, rendered)
            if has_errors(save_errors):
                return JSONResponse({"errors": [e.to_dict() for e in save_errors]}, status_code=400)
            hub.publish({"event": "card-changed", "card": new_id})
        else:
            file_cards_repo.root.mkdir(parents=True, exist_ok=True)
            file_cards_repo.path(new_id).write_text(rendered)
        return {"id": new_id}

    @app.get("/api/ingest", dependencies=[Depends(no_guest)])
    def list_ingest_jobs() -> list[dict]:
        with db.connect(settings.database_url) as conn:
            return list_jobs(conn)

    @app.get("/api/ingest/config", dependencies=[Depends(no_guest)])
    def ingest_config() -> dict:
        return {
            "storage": "db" if settings.uses_db_storage else "files",
            "ingest_provider": (
                ingest_llm.provider_name if ingest_llm else configured_provider(settings)
            ),
        }

    @app.get("/api/ingest/{job_id}", dependencies=[Depends(no_guest)])
    def get_ingest_job(job_id: str) -> dict:
        with db.connect(settings.database_url) as conn:
            job = get_job(conn, job_id)
        if job is None:
            raise HTTPException(404, f"no ingest job '{job_id}'")
        return job

    @app.post("/api/ingest", dependencies=[Depends(no_write)])
    def start_ingest(req: IngestRequest):
        if req.kind == "markdown":
            if not req.text or not req.text.strip():
                raise HTTPException(422, "paste or upload Markdown reading content")
            try:
                job = ingest_markdown_direct(
                    settings=settings,
                    markdown_text=req.text,
                    slug=req.slug,
                    cards_yaml=req.cards_yaml,
                    on_event=hub.publish,
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return JSONResponse(job, status_code=201)

        pdf_bytes: bytes | None = None
        if req.kind == "pdf":
            if not req.pdf_base64:
                raise HTTPException(422, "upload a PDF file")
            try:
                pdf_bytes = base64.b64decode(req.pdf_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(422, "invalid base64 PDF payload") from exc
            if not pdf_bytes:
                raise HTTPException(422, "uploaded PDF is empty")
            if len(pdf_bytes) > MAX_PDF_BYTES:
                raise HTTPException(422, "PDF exceeds 25 MB limit")
        elif req.kind == "url":
            if not req.url or not req.url.strip():
                raise HTTPException(422, "enter an http:// or https:// article URL")
        elif req.kind == "text":
            if not req.text or len(req.text.strip()) < 40:
                raise HTTPException(422, "paste at least a short paragraph of article text")

        try:
            active_llm = ingest_llm or select_ingest_llm(settings)
        except LLMError as exc:
            raise HTTPException(400, str(exc)) from exc

        title_hint = req.title or (
            Path(req.filename).stem.replace("-", " ") if req.filename else None
        )
        with db.connect(settings.database_url) as conn:
            job = create_job(conn, kind=req.kind, title=title_hint, source_url=req.url)
            conn.commit()
        hub.publish({"event": "ingest-progress", "job": job})

        thread = threading.Thread(
            target=run_ingest_job,
            kwargs={
                "job_id": job["id"],
                "settings": settings,
                "kind": req.kind,
                "pdf_bytes": pdf_bytes,
                "url": req.url,
                "text": req.text,
                "title_hint": title_hint,
                "llm": active_llm,
                "sync_source": sync_source,
                "chooser": chooser,
                "on_event": hub.publish,
            },
            daemon=True,
        )
        thread.start()
        return JSONResponse(job, status_code=202)

    @app.delete("/api/readings/{reading_id}", dependencies=[Depends(no_write)])
    def delete_reading(reading_id: str) -> dict:
        if readings_repo.delete(reading_id):
            hub.publish({"event": "reading-removed", "reading": reading_id})
            return {"deleted": reading_id}
        if reading_id in readings_repo.ids():
            raise HTTPException(400, "git-backed reading cannot be deleted from Postgres")
        raise HTTPException(404, f"no reading '{reading_id}'")

    @app.get("/api/series/{source}/{series_id}")
    def series_route(source: str, series_id: str) -> dict:
        with db.connect(settings.database_url) as conn:
            page = series_page(conn, source, series_id, sync_source)
        if page is None:
            raise HTTPException(404, f"no series {source}:{series_id}")
        if read_only and page["hint"]:
            page["hint"] = f"{page['hint']}  # on the home base"
        page["read_only"] = read_only
        return page

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        async def stream():
            queue = hub.subscribe()
            watch_task: asyncio.Task | None = None
            watch_dirs: list[Path] = []
            if not settings.uses_db_storage:
                file_cards_repo.root.mkdir(parents=True, exist_ok=True)
                file_readings_repo.root.mkdir(parents=True, exist_ok=True)
            for d in (file_cards_repo.root, file_readings_repo.root):
                if d.is_dir():
                    watch_dirs.append(d)

            if watch_dirs:
                readings_root = file_readings_repo.root.resolve()

                async def watch_files() -> None:
                    async for changes in awatch(*watch_dirs):
                        for _, changed in changes:
                            path = Path(changed)
                            if file_readings_repo.root.is_dir() and path.resolve().is_relative_to(
                                readings_root
                            ):
                                event = reading_event(file_readings_repo, file_cards_repo, path)
                            else:
                                event = card_event(file_cards_repo, path)
                            if event:
                                await queue.put(event)

                watch_task = asyncio.create_task(watch_files())

            try:
                while True:
                    event = await queue.get()
                    yield f"event: {event['event']}\ndata: {json.dumps(event)}\n\n"
            finally:
                hub.unsubscribe(queue)
                if watch_task is not None:
                    watch_task.cancel()

        return StreamingResponse(stream(), media_type="text/event-stream")

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app

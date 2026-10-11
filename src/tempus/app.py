"""`tempus serve`: the engine API plus the static front end, with hot reload.

Every read goes through the card engine; the browser never touches formulas or SQL.
"""

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from watchfiles import awatch

from tempus import db
from tempus.cards.check import check_card
from tempus.cards.errors import has_errors
from tempus.cards.repository import FileCardRepository
from tempus.cards.run import run_card, run_spec
from tempus.cards.spec import CARD_ID, parse_card
from tempus.config import Settings, get_settings
from tempus.drafting import SHAPES, Draft, JevDrafter, Pick, card_yaml
from tempus.drafting import card_id as card_id_for
from tempus.explore import SourceFactory, catalog_from_db, explore_payload, series_page
from tempus.filing import Chooser
from tempus.jev import ChoiceAnswer, JevClient, JevError
from tempus.navigate import LexicalChooser, navigate
from tempus.sources import SOURCE_NAMES, get_source

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


LEXICAL = "lexical fallback (no TYPESAFE_API_KEY)"


def default_sync_source(settings: Settings) -> SourceFactory | None:
    """Sync-on-open needs a provider key; without one, series pages explain how to sync."""
    if not settings.fred_api_key:
        return None
    return lambda name: get_source(name, settings) if name in SOURCE_NAMES else None


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


class KeepRequest(BaseModel):
    card: dict


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
) -> FastAPI:
    """`read_only=True` never writes to the database: series pages and drafts don't sync.
    Reading prod through the tunnel (TEMPUS_DB=prod) is always read-only. Keep still writes
    cards/ there, since the file is local. `guest=True` is the shared (Funnel) app: it never
    writes, drafts or keeps, and has no API docs. `chooser` stands in for Jev in drafting
    (tests); by default drafting uses Jev with a key and the lexical fallback without."""
    settings = settings or get_settings()
    is_remote = settings.db == "prod"
    read_only = guest or read_only or is_remote
    sync_source = None if read_only else sync_source or default_sync_source(settings)
    repo = FileCardRepository(settings.cards_path)
    docs = {"docs_url": None, "redoc_url": None, "openapi_url": None} if guest else {}
    app = FastAPI(title="tempus", **docs)

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

    @app.post("/api/cards", status_code=201, dependencies=[Depends(no_guest)])
    def keep_route(req: KeepRequest):
        card, errors = parse_card("draft", req.card)
        if card is not None:
            errors += check_card("draft", card)[1]
        if card is None or has_errors(errors):
            return JSONResponse({"errors": [e.to_dict() for e in errors]}, status_code=400)
        new_id = card_id_for(card.title, set(repo.ids()))
        repo.root.mkdir(parents=True, exist_ok=True)
        repo.path(new_id).write_text(card_yaml(card))
        return {"id": new_id}

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
            repo.root.mkdir(parents=True, exist_ok=True)
            async for changes in awatch(repo.root):
                for _, changed in changes:
                    event = card_event(repo, Path(changed))
                    if event:
                        yield f"event: {event['event']}\ndata: {json.dumps(event)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app

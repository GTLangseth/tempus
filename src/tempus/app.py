"""`tempus serve`: the engine API plus the static front end, with hot reload.

Every read goes through the card engine; the browser never touches formulas or SQL.
"""

import json
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from watchfiles import awatch

from tempus import db
from tempus.cards.check import check_card
from tempus.cards.errors import has_errors
from tempus.cards.repository import FileCardRepository
from tempus.cards.run import run_card
from tempus.config import Settings, get_settings

WEB_DIR = Path(__file__).with_name("web")


def card_event(repo: FileCardRepository, path: Path) -> dict | None:
    if path.suffix != ".yaml":
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    repo = FileCardRepository(settings.cards_path)
    app = FastAPI(title="tempus")

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

"""load → check → resolve → evaluate → render, stopping at the first stage with errors."""

from dataclasses import dataclass, field
from datetime import date

import psycopg

from tempus.cards.check import check_card
from tempus.cards.errors import CardError, has_errors
from tempus.cards.evaluate import evaluate, fetch
from tempus.cards.render import PAYLOAD_VERSION, render
from tempus.cards.repository import CardRepository
from tempus.cards.resolve import resolve
from tempus.cards.spec import Card

__all__ = ["PAYLOAD_VERSION", "CardResult", "run_card"]


@dataclass
class CardResult:
    card_id: str
    card: Card | None
    payloads: list[dict] = field(default_factory=list)
    errors: list[CardError] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.card_id,
            "card": self.card.model_dump(mode="json") if self.card else None,
            "payloads": self.payloads,
            "errors": [e.to_dict() for e in self.errors],
        }


def run_card(
    conn: psycopg.Connection,
    repo: CardRepository,
    card_id: str,
    start: date | None = None,
    end: date | None = None,
) -> CardResult:
    card, errors = repo.get(card_id)
    result = CardResult(card_id, card, errors=errors)
    if card is None:
        return result
    result.errors += check_card(card_id, card)[1]
    if has_errors(result.errors):
        return result
    resolved, errs = resolve(conn, card_id, card)
    result.errors += errs
    if has_errors(result.errors):
        return result
    data = fetch(conn, [i for infos in resolved.values() for i in infos])
    window = (start or card.window.start, end or card.window.end)
    env, errs = evaluate(card_id, card, resolved, data, window)
    result.errors += errs
    result.payloads, errs = render(card_id, card, env, window)
    result.errors += errs
    return result

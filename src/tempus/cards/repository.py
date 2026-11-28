"""Where cards live. Files in git are the source of truth; the `cards` table is an index.

Every surface (CLI, API, MCP) goes through a CardRepository, so a database-first deployment
later only swaps the implementation (docs/STACK.md → Open decisions, card storage).
"""

import hashlib
from pathlib import Path
from typing import Protocol

import psycopg
import yaml
from psycopg.types.json import Jsonb

from tempus import db
from tempus.cards.check import check_card
from tempus.cards.errors import CardError, did_you_mean, has_errors
from tempus.cards.spec import Card, load_card, parse_card


class CardRepository(Protocol):
    def ids(self) -> list[str]: ...
    def get(self, card_id: str) -> tuple[Card | None, list[CardError]]: ...
    def text(self, card_id: str) -> str: ...
    def save(self, card_id: str, text: str) -> list[CardError]: ...


class FileCardRepository:
    def __init__(self, root: Path):
        self.root = root

    def path(self, card_id: str) -> Path:
        return self.root / f"{card_id}.yaml"

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.stem for p in self.root.glob("*.yaml"))

    def get(self, card_id: str) -> tuple[Card | None, list[CardError]]:
        path = self.path(card_id)
        if not path.exists():
            return None, [
                CardError(card_id, "file", "no such card", hint=did_you_mean(card_id, self.ids()))
            ]
        return load_card(path)

    def text(self, card_id: str) -> str:
        return self.path(card_id).read_text()

    def save(self, card_id: str, text: str) -> list[CardError]:
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            return [CardError(card_id, "file", f"invalid YAML: {exc}")]
        card, errors = parse_card(card_id, raw)
        if card is not None:
            errors += check_card(card_id, card)[1]
        if has_errors(errors):
            return errors
        self.root.mkdir(parents=True, exist_ok=True)
        self.path(card_id).write_text(text)
        return errors

    def delete(self, card_id: str) -> bool:
        path = self.path(card_id)
        if not path.exists():
            return False
        path.unlink()
        return True


class DbCardRepository:
    """Cards stored in Postgres (`cards` table), with optional read fallback to repo seed files."""

    def __init__(self, database_url: str, fallback_root: Path | None = None):
        self.database_url = database_url
        self.fallback = FileCardRepository(fallback_root) if fallback_root is not None else None

    def ids(self) -> list[str]:
        with db.connect(self.database_url) as conn:
            known = {r["id"] for r in conn.execute("SELECT id FROM cards ORDER BY id")}
        if self.fallback is not None:
            known.update(self.fallback.ids())
        return sorted(known)

    def get(self, card_id: str) -> tuple[Card | None, list[CardError]]:
        with db.connect(self.database_url) as conn:
            row = conn.execute(
                "SELECT spec, yaml_text FROM cards WHERE id = %s", (card_id,)
            ).fetchone()
        if row is not None:
            if row["yaml_text"] is not None:
                try:
                    raw = yaml.safe_load(row["yaml_text"])
                except yaml.YAMLError as exc:
                    return None, [CardError(card_id, "file", f"invalid YAML: {exc}")]
            else:
                raw = row["spec"]
            return parse_card(card_id, raw)
        if self.fallback is not None and self.fallback.path(card_id).exists():
            return self.fallback.get(card_id)
        return None, [
            CardError(card_id, "file", "no such card", hint=did_you_mean(card_id, self.ids()))
        ]

    def text(self, card_id: str) -> str:
        with db.connect(self.database_url) as conn:
            row = conn.execute(
                "SELECT spec, yaml_text FROM cards WHERE id = %s", (card_id,)
            ).fetchone()
        if row is not None:
            if row["yaml_text"] is not None:
                return row["yaml_text"]
            return yaml.safe_dump(row["spec"], sort_keys=False, allow_unicode=True)
        if self.fallback is not None:
            return self.fallback.text(card_id)
        raise FileNotFoundError(f"no such card: {card_id}")

    def save(self, card_id: str, text: str) -> list[CardError]:
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            return [CardError(card_id, "file", f"invalid YAML: {exc}")]
        card, errors = parse_card(card_id, raw)
        if card is not None:
            errors += check_card(card_id, card)[1]
        if has_errors(errors) or card is None:
            return errors
        with db.connect(self.database_url) as conn, conn.transaction():
            conn.execute(
                """
                INSERT INTO cards (id, title, question, tags, concepts, spec, yaml_text, file_hash,
                                   updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, '', now())
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    question = EXCLUDED.question,
                    tags = EXCLUDED.tags,
                    concepts = EXCLUDED.concepts,
                    spec = EXCLUDED.spec,
                    yaml_text = EXCLUDED.yaml_text,
                    updated_at = now()
                """,
                (
                    card_id,
                    card.title,
                    card.question,
                    card.tags,
                    card.concepts,
                    Jsonb(card.model_dump(mode="json")),
                    text,
                ),
            )
        return errors

    def delete(self, card_id: str) -> bool:
        with db.connect(self.database_url) as conn, conn.transaction():
            cur = conn.execute("DELETE FROM cards WHERE id = %s", (card_id,))
        return cur.rowcount > 0


def sync_index(
    conn: psycopg.Connection, repo: CardRepository, *, prune: bool = True
) -> tuple[int, list[CardError]]:
    loaded, problems = 0, []
    ids = repo.ids()
    with conn.transaction():
        for card_id in ids:
            card, errors = repo.get(card_id)
            if card is not None:
                errors += check_card(card_id, card)[1]
            problems += errors
            if card is None or has_errors(errors):
                continue
            digest = hashlib.sha256(repo.text(card_id).encode()).hexdigest()
            conn.execute(
                """
                INSERT INTO cards (id, title, question, tags, concepts, spec, yaml_text, file_hash,
                                   updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title, question = EXCLUDED.question,
                    tags = EXCLUDED.tags, concepts = EXCLUDED.concepts, spec = EXCLUDED.spec,
                    yaml_text = NULL, file_hash = EXCLUDED.file_hash,
                    loaded_at = now(), updated_at = now()
                """,
                (
                    card_id,
                    card.title,
                    card.question,
                    card.tags,
                    card.concepts,
                    Jsonb(card.model_dump(mode="json")),
                    digest,
                ),
            )
            loaded += 1
        if prune:
            conn.execute("DELETE FROM cards WHERE NOT (id = ANY(%s))", (ids,))
        else:
            conn.execute("DELETE FROM cards WHERE yaml_text IS NULL AND NOT (id = ANY(%s))", (ids,))
    return loaded, problems

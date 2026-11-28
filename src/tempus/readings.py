"""Readings: card-backed Markdown digests of research articles (`readings/<id>.md`).

Files in git are the source of truth behind ReadingRepository; calculation and charting
stay inside `cards/*.yaml`.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, Protocol

import psycopg
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from tempus import db
from tempus.cards.check import check_card
from tempus.cards.errors import CardError, did_you_mean
from tempus.cards.errors import has_errors as has_card_errors
from tempus.cards.repository import CardRepository
from tempus.cards.resolve import resolve
from tempus.cards.run import run_card
from tempus.cards.spec import CARD_ID, SERIES_REF, Card

READING_ID = CARD_ID
CLAIM_STATUSES = ("confirmed", "extended", "nuanced", "unverified")
_FOOTNOTE_DEF = re.compile(r"^\[\^([A-Za-z0-9_-]+)\]:\s*(.+)$")
_FOOTNOTE_REF = re.compile(r"\[\^([A-Za-z0-9_-]+)\]")


@dataclass(frozen=True)
class ReadingError:
    reading: str
    path: str
    message: str
    level: Literal["error", "warning"] = "error"
    hint: str | None = None

    def format(self) -> str:
        text = f"readings/{self.reading}.md · {self.path}\n  {self.message}"
        return f"{text} {self.hint}" if self.hint else text

    def to_dict(self) -> dict:
        return {
            "reading": self.reading,
            "path": self.path,
            "message": self.message,
            "level": self.level,
            "hint": self.hint,
        }


def has_errors(errors: list[ReadingError]) -> bool:
    return any(e.level == "error" for e in errors)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReadingMeta(_Strict):
    version: Literal[1]
    title: str = Field(min_length=1)
    authors: list[str] = Field(min_length=1)
    source: str | None = None
    published: str = Field(min_length=4)
    url: str | None = None
    summary: str = Field(min_length=1)
    credit: str | None = None
    concepts: list[str] = []
    tags: list[str] = []

    @field_validator("published", mode="before")
    @classmethod
    def _coerce_published(cls, value: object) -> object:
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, int):
            return str(value)
        return value


class _CardEmbedInput(_Strict):
    card: str
    caption: str | None = None
    start: date | None = None
    end: date | None = None

    @field_validator("card")
    @classmethod
    def _valid_card_id(cls, value: str) -> str:
        if not CARD_ID.fullmatch(value):
            raise ValueError(f"card id must be kebab-case, got {value!r}")
        return value

    @model_validator(mode="after")
    def _valid_window(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError(f"start ({self.start}) must be on or before end ({self.end})")
        return self


class _ClaimInput(_Strict):
    quote: str = Field(min_length=1)
    status: Literal["confirmed", "extended", "nuanced", "unverified"]
    verdict: str = Field(min_length=1)
    series: list[str] = []
    card: str | None = None

    @field_validator("series")
    @classmethod
    def _valid_series(cls, refs: list[str]) -> list[str]:
        for ref in refs:
            if not SERIES_REF.fullmatch(ref):
                raise ValueError(f"series must look like source:series_id, got {ref!r}")
        return refs

    @field_validator("card")
    @classmethod
    def _valid_card(cls, value: str | None) -> str | None:
        if value is not None and not CARD_ID.fullmatch(value):
            raise ValueError(f"card id must be kebab-case, got {value!r}")
        return value


@dataclass(frozen=True)
class HeadingBlock:
    level: Literal[2, 3]
    text: str


@dataclass(frozen=True)
class ParagraphBlock:
    text: str


@dataclass(frozen=True)
class QuoteBlock:
    text: str


@dataclass(frozen=True)
class ListBlock:
    ordered: bool
    items: tuple[str, ...]


@dataclass(frozen=True)
class DividerBlock:
    pass


@dataclass(frozen=True)
class CardEmbed:
    card: str
    caption: str | None = None
    start: date | None = None
    end: date | None = None
    path: str = "body"


@dataclass(frozen=True)
class ClaimBlock:
    quote: str
    status: Literal["confirmed", "extended", "nuanced", "unverified"]
    verdict: str
    series: tuple[str, ...] = ()
    card: str | None = None
    path: str = "body"


@dataclass(frozen=True)
class NoteBlock:
    title: str | None
    paragraphs: tuple[str, ...]
    path: str = "body"


@dataclass(frozen=True)
class Footnote:
    id: str
    text: str


Block = (
    HeadingBlock
    | ParagraphBlock
    | QuoteBlock
    | ListBlock
    | DividerBlock
    | CardEmbed
    | ClaimBlock
    | NoteBlock
)


def _prose_texts(reading: "Reading") -> list[str]:
    texts: list[str] = []
    for block in reading.blocks:
        if isinstance(block, HeadingBlock | ParagraphBlock | QuoteBlock):
            texts.append(block.text)
        elif isinstance(block, ListBlock):
            texts.extend(block.items)
    return texts


@dataclass(frozen=True)
class Reading:
    id: str
    meta: ReadingMeta
    blocks: list[Block] = field(default_factory=list)
    footnotes: tuple[Footnote, ...] = ()

    def card_ids(self) -> list[str]:
        seen: list[str] = []
        for block in self.blocks:
            cid = block.card if isinstance(block, CardEmbed | ClaimBlock) else None
            if cid and cid not in seen:
                seen.append(cid)
        return seen

    def claims(self) -> list[ClaimBlock]:
        return [b for b in self.blocks if isinstance(b, ClaimBlock)]

    def claim_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in CLAIM_STATUSES}
        for claim in self.claims():
            counts[claim.status] += 1
        return counts

    def word_count(self) -> int:
        texts = [*_prose_texts(self), *(f.text for f in self.footnotes)]
        return sum(len(t.split()) for t in texts)

    def reading_minutes(self) -> int:
        words = self.word_count()
        return max(1, round(words / 230)) if words else 1


def _loc(loc: tuple) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "frontmatter"


def _split_frontmatter(reading_id: str, text: str) -> tuple[str | None, str, list[ReadingError]]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return (
            None,
            "",
            [
                ReadingError(
                    reading_id,
                    "frontmatter",
                    "reading must begin with YAML front matter (--- ... ---)",
                )
            ],
        )
    end_idx = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end_idx is None:
        return (
            None,
            "",
            [ReadingError(reading_id, "frontmatter", "unclosed YAML front matter (missing ---)")],
        )
    return "\n".join(lines[1:end_idx]), "\n".join(lines[end_idx + 1 :]), []


def _parse_note(
    reading_id: str, body: str, path: str
) -> tuple[NoteBlock | None, list[ReadingError]]:
    lines = body.splitlines()
    title: str | None = None
    first = next((i for i, raw in enumerate(lines) if raw.strip()), None)
    if first is None:
        return None, [ReadingError(reading_id, path, "```note block cannot be empty")]
    if lines[first].strip().lower().startswith("title:"):
        title = lines[first].split(":", 1)[1].strip() or None
        lines = lines[first + 1 :]
    else:
        lines = lines[first:]

    paragraphs: list[str] = []
    buf: list[str] = []

    def flush():
        if buf:
            paragraphs.append(" ".join(buf))
            buf.clear()

    for raw in lines:
        s = raw.strip()
        if not s:
            flush()
        elif s.startswith(("- ", "* ")) and len(s) > 2:
            flush()
            paragraphs.append(f"- {s[2:].strip()}")
        else:
            if buf and buf[-1].startswith("- "):
                flush()
            buf.append(s)
    flush()

    if not paragraphs:
        return None, [ReadingError(reading_id, path, "```note block needs body text")]
    return NoteBlock(title=title, paragraphs=tuple(paragraphs), path=path), []


def _parse_fence(
    reading_id: str, tag: str, body: str, line_no: int
) -> tuple[Block | None, list[ReadingError]]:
    path = f"body.line:{line_no}"
    if tag == "note":
        return _parse_note(reading_id, body, path)
    if tag not in ("card", "claim"):
        label = tag or "(none)"
        return None, [
            ReadingError(
                reading_id,
                path,
                f"unknown fenced block '{label}'",
                hint=did_you_mean(tag, ("card", "claim", "note"))
                or "Use ```card, ```claim, or ```note.",
            )
        ]
    try:
        raw = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        return None, [ReadingError(reading_id, path, f"invalid YAML in ```{tag} block: {exc}")]

    if tag == "card":
        if isinstance(raw, str):
            raw = {"card": raw.strip()}
        elif isinstance(raw, dict) and "id" in raw and "card" not in raw:
            raw = {"card": raw["id"], **{k: v for k, v in raw.items() if k != "id"}}
        if not isinstance(raw, dict):
            return None, [
                ReadingError(reading_id, path, "```card block must be a card id or YAML mapping")
            ]
        try:
            parsed = _CardEmbedInput.model_validate(raw)
        except ValidationError as exc:
            return None, [
                ReadingError(reading_id, f"{path}.{_loc(e['loc'])}", e["msg"]) for e in exc.errors()
            ]
        return CardEmbed(parsed.card, parsed.caption, parsed.start, parsed.end, path=path), []

    if not isinstance(raw, dict):
        return None, [ReadingError(reading_id, path, "```claim block must be a YAML mapping")]
    try:
        parsed = _ClaimInput.model_validate(raw)
    except ValidationError as exc:
        return None, [
            ReadingError(reading_id, f"{path}.{_loc(e['loc'])}", e["msg"]) for e in exc.errors()
        ]
    return (
        ClaimBlock(
            quote=parsed.quote,
            status=parsed.status,
            verdict=parsed.verdict,
            series=tuple(parsed.series),
            card=parsed.card,
            path=path,
        ),
        [],
    )


def _list_item(line: str) -> tuple[bool, str] | None:
    stripped = line.lstrip()
    if stripped.startswith(("- ", "* ")) and len(stripped) > 2:
        return False, stripped[2:].strip()
    i = 0
    while i < len(stripped) and stripped[i].isdigit():
        i += 1
    if i > 0 and stripped[i : i + 2] == ". ":
        return True, stripped[i + 2 :].strip()
    return None


def _parse_body(
    reading_id: str, body: str, line_offset: int
) -> tuple[list[Block], tuple[Footnote, ...], list[ReadingError]]:
    blocks: list[Block] = []
    footnotes: list[Footnote] = []
    seen_footnotes: set[str] = set()
    errors: list[ReadingError] = []
    lines = body.splitlines()

    para: list[str] = []
    quote: list[str] = []
    items: list[str] = []
    list_ordered: bool | None = None

    def flush_para():
        if para:
            blocks.append(ParagraphBlock(" ".join(para)))
            para.clear()

    def flush_quote():
        if quote:
            blocks.append(QuoteBlock(" ".join(quote)))
            quote.clear()

    def flush_list():
        nonlocal list_ordered
        if items and list_ordered is not None:
            blocks.append(ListBlock(list_ordered, tuple(items)))
            items.clear()
            list_ordered = None

    def flush_all():
        flush_para()
        flush_quote()
        flush_list()

    idx = 0
    while idx < len(lines):
        raw_line = lines[idx]
        line = raw_line.strip()
        line_no = line_offset + idx + 1

        if line.startswith("```"):
            flush_all()
            tag = line[3:].strip().lower()
            start_no = line_no
            idx += 1
            fence_lines: list[str] = []
            while idx < len(lines) and lines[idx].strip() != "```":
                fence_lines.append(lines[idx])
                idx += 1
            if idx >= len(lines):
                errors.append(
                    ReadingError(reading_id, f"body.line:{start_no}", "unclosed fenced block")
                )
                break
            block, fence_errors = _parse_fence(reading_id, tag, "\n".join(fence_lines), start_no)
            errors.extend(fence_errors)
            if block is not None:
                blocks.append(block)
            idx += 1
            continue

        if not line:
            flush_all()
            idx += 1
            continue

        if line in ("---", "***"):
            flush_all()
            blocks.append(DividerBlock())
            idx += 1
            continue

        fn_match = _FOOTNOTE_DEF.match(raw_line)
        if fn_match is not None:
            flush_all()
            fid, ftext = fn_match.group(1), fn_match.group(2).strip()
            idx += 1
            parts = [ftext]
            while (
                idx < len(lines)
                and (lines[idx].startswith("  ") or lines[idx].startswith("\t"))
                and lines[idx].strip()
            ):
                parts.append(lines[idx].strip())
                idx += 1
            if fid in seen_footnotes:
                errors.append(
                    ReadingError(
                        reading_id, f"body.line:{line_no}", f"duplicate footnote '[^{fid}]'"
                    )
                )
            else:
                seen_footnotes.add(fid)
                footnotes.append(Footnote(fid, " ".join(parts)))
            continue

        if line.startswith("#"):
            hashes = len(line) - len(line.lstrip("#"))
            text = line[hashes:].strip()
            if 1 <= hashes <= 6 and text:
                flush_all()
                level: Literal[2, 3] = 2 if hashes <= 2 else 3
                blocks.append(HeadingBlock(level, text))
                idx += 1
                continue

        if line.startswith(">"):
            flush_para()
            flush_list()
            quote.append(line[1:].strip())
            idx += 1
            continue

        list_match = _list_item(raw_line)
        if list_match is not None:
            ordered, item_text = list_match
            flush_para()
            flush_quote()
            if list_ordered is not None and list_ordered != ordered:
                flush_list()
            list_ordered = ordered
            items.append(item_text)
            idx += 1
            continue

        flush_quote()
        flush_list()
        para.append(line)
        idx += 1

    flush_all()
    return blocks, tuple(footnotes), errors


def parse_reading(reading_id: str, text: str) -> tuple[Reading | None, list[ReadingError]]:
    if not READING_ID.fullmatch(reading_id):
        msg = "reading file names must be kebab-case, e.g. energy-pass-through.md"
        return None, [ReadingError(reading_id, "file", msg)]

    fm_text, body_text, fm_errors = _split_frontmatter(reading_id, text)
    if fm_errors or fm_text is None:
        return None, fm_errors

    try:
        raw_meta = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        return None, [ReadingError(reading_id, "frontmatter", f"invalid YAML: {exc}")]

    if not isinstance(raw_meta, dict):
        return None, [
            ReadingError(reading_id, "frontmatter", "front matter must be a YAML mapping")
        ]

    try:
        meta = ReadingMeta.model_validate(raw_meta)
    except ValidationError as exc:
        return None, [
            ReadingError(reading_id, f"frontmatter.{_loc(e['loc'])}", e["msg"])
            for e in exc.errors()
        ]

    line_offset = len(fm_text.splitlines()) + 2
    blocks, footnotes, body_errors = _parse_body(reading_id, body_text, line_offset)
    if has_errors(body_errors):
        return None, body_errors
    return Reading(id=reading_id, meta=meta, blocks=blocks, footnotes=footnotes), body_errors


def load_reading(path: Path) -> tuple[Reading | None, list[ReadingError]]:
    reading_id = path.stem
    try:
        text = path.read_text()
    except OSError as exc:
        return None, [ReadingError(reading_id, "file", f"could not read file: {exc}")]
    return parse_reading(reading_id, text)


class ReadingRepository(Protocol):
    def ids(self) -> list[str]: ...
    def get(self, reading_id: str) -> tuple[Reading | None, list[ReadingError]]: ...
    def text(self, reading_id: str) -> str: ...


class FileReadingRepository:
    def __init__(self, root: Path):
        self.root = root

    def path(self, reading_id: str) -> Path:
        return self.root / f"{reading_id}.md"

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.stem for p in self.root.glob("*.md") if READING_ID.fullmatch(p.stem))

    def get(self, reading_id: str) -> tuple[Reading | None, list[ReadingError]]:
        path = self.path(reading_id)
        if not path.exists():
            return None, [
                ReadingError(
                    reading_id,
                    "file",
                    "no such reading",
                    hint=did_you_mean(reading_id, self.ids()),
                )
            ]
        return load_reading(path)

    def text(self, reading_id: str) -> str:
        return self.path(reading_id).read_text()

    def save(self, reading_id: str, text: str) -> list[ReadingError]:
        reading, errors = parse_reading(reading_id, text)
        if reading is None or has_errors(errors):
            return errors
        self.root.mkdir(parents=True, exist_ok=True)
        self.path(reading_id).write_text(text)
        return errors

    def delete(self, reading_id: str) -> bool:
        path = self.path(reading_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def status_info(self, reading_id: str) -> dict | None:
        if not self.path(reading_id).exists():
            return None
        return {
            "status": "published",
            "origin": "file",
            "source_url": None,
            "created_at": None,
            "updated_at": None,
        }


class DbReadingRepository:
    """Readings stored in Postgres (`readings` table), storing full frontmatter + Markdown."""

    def __init__(self, database_url: str, fallback_root: Path | None = None):
        self.database_url = database_url
        self.fallback = FileReadingRepository(fallback_root) if fallback_root is not None else None

    def ids(self) -> list[str]:
        with db.connect(self.database_url) as conn:
            known = {r["id"] for r in conn.execute("SELECT id FROM readings ORDER BY id")}
        if self.fallback is not None:
            known.update(self.fallback.ids())
        return sorted(known)

    def get(self, reading_id: str) -> tuple[Reading | None, list[ReadingError]]:
        with db.connect(self.database_url) as conn:
            row = conn.execute(
                "SELECT body_md FROM readings WHERE id = %s", (reading_id,)
            ).fetchone()
        if row is not None:
            return parse_reading(reading_id, row["body_md"])
        if self.fallback is not None and self.fallback.path(reading_id).exists():
            return self.fallback.get(reading_id)
        return None, [
            ReadingError(
                reading_id,
                "file",
                "no such reading",
                hint=did_you_mean(reading_id, self.ids()),
            )
        ]

    def text(self, reading_id: str) -> str:
        with db.connect(self.database_url) as conn:
            row = conn.execute(
                "SELECT body_md FROM readings WHERE id = %s", (reading_id,)
            ).fetchone()
        if row is not None:
            return row["body_md"]
        if self.fallback is not None:
            return self.fallback.text(reading_id)
        raise FileNotFoundError(f"no such reading: {reading_id}")

    def status_info(self, reading_id: str) -> dict | None:
        with db.connect(self.database_url) as conn:
            row = conn.execute(
                "SELECT status, origin, source_url, created_at, updated_at"
                " FROM readings WHERE id = %s",
                (reading_id,),
            ).fetchone()
        if row is not None:
            return dict(row)
        if self.fallback is not None:
            return self.fallback.status_info(reading_id)
        return None

    def save(
        self,
        reading_id: str,
        text: str,
        *,
        status: str = "published",
        origin: str = "ingested",
    ) -> list[ReadingError]:
        reading, errors = parse_reading(reading_id, text)
        if reading is None or has_errors(errors):
            return errors
        meta = reading.meta
        with db.connect(self.database_url) as conn, conn.transaction():
            conn.execute(
                """
                INSERT INTO readings (
                    id, title, subtitle, source, published, topics, concepts,
                    card_ids, body_md, source_url, status, origin, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    subtitle = EXCLUDED.subtitle,
                    source = EXCLUDED.source,
                    published = EXCLUDED.published,
                    topics = EXCLUDED.topics,
                    concepts = EXCLUDED.concepts,
                    card_ids = EXCLUDED.card_ids,
                    body_md = EXCLUDED.body_md,
                    source_url = EXCLUDED.source_url,
                    status = EXCLUDED.status,
                    origin = EXCLUDED.origin,
                    updated_at = now()
                """,
                (
                    reading_id,
                    meta.title,
                    meta.summary,
                    meta.source,
                    meta.published,
                    meta.tags,
                    meta.concepts,
                    reading.card_ids(),
                    text,
                    meta.url,
                    status,
                    origin,
                ),
            )
        return errors

    def delete(self, reading_id: str) -> bool:
        with db.connect(self.database_url) as conn, conn.transaction():
            cur = conn.execute("DELETE FROM readings WHERE id = %s", (reading_id,))
        return cur.rowcount > 0


def check_reading(
    reading_id: str,
    reading: Reading,
    cards_repo: CardRepository,
    *,
    conn: psycopg.Connection | None = None,
    known_concepts: set[str] | None = None,
) -> list[ReadingError]:
    errors: list[ReadingError] = []

    if known_concepts is not None:
        for i, concept in enumerate(reading.meta.concepts):
            if concept not in known_concepts:
                errors.append(
                    ReadingError(
                        reading_id,
                        f"frontmatter.concepts[{i}]",
                        f"unknown concept '{concept}'",
                        hint=did_you_mean(concept, known_concepts),
                    )
                )

    card_cache: dict[str, tuple[Card | None, list[CardError]]] = {}
    all_cards = cards_repo.ids()
    has_embed = False

    for block in reading.blocks:
        if isinstance(block, CardEmbed):
            has_embed = True
        cid = block.card if isinstance(block, CardEmbed | ClaimBlock) else None
        if not cid:
            continue
        if cid not in card_cache:
            card, card_errs = cards_repo.get(cid)
            if card is not None:
                card_errs = [*card_errs, *check_card(cid, card)[1]]
            card_cache[cid] = (card, card_errs)
        card, card_errs = card_cache[cid]
        if card is None and cid not in all_cards:
            errors.append(
                ReadingError(
                    reading_id,
                    block.path,
                    f"unknown card '{cid}'",
                    hint=did_you_mean(cid, all_cards),
                )
            )
        elif has_card_errors(card_errs):
            first = next(e for e in card_errs if e.level == "error")
            errors.append(
                ReadingError(
                    reading_id,
                    block.path,
                    f"card '{cid}' has errors ({first.path}: {first.message})",
                    hint=first.hint,
                )
            )

    if not has_embed:
        errors.append(ReadingError(reading_id, "body", "reading embeds no cards", level="warning"))

    defined = [f.id for f in reading.footnotes]
    defined_set = set(defined)
    referenced: list[str] = []
    for text in _prose_texts(reading):
        for fid in _FOOTNOTE_REF.findall(text):
            if fid not in referenced:
                referenced.append(fid)
    for fid in referenced:
        if fid not in defined_set:
            errors.append(
                ReadingError(reading_id, "footnotes", f"missing footnote definition '[^{fid}]'")
            )
    referenced_set = set(referenced)
    for fid in defined:
        if fid not in referenced_set:
            errors.append(
                ReadingError(
                    reading_id, "footnotes", f"unused footnote '[^{fid}]'", level="warning"
                )
            )

    if conn is None:
        return errors

    for cid, (card, card_errs) in card_cache.items():
        if card is None or has_card_errors(card_errs):
            continue
        for err in resolve(conn, cid, card)[1]:
            errors.append(
                ReadingError(
                    reading_id,
                    f"card:{cid}.{err.path}",
                    err.message,
                    level=err.level,
                    hint=err.hint,
                )
            )

    all_refs = sorted({ref for claim in reading.claims() for ref in claim.series})
    if all_refs:
        rows = conn.execute(
            "SELECT source || ':' || series_id AS ref, last_synced_at FROM series"
            " WHERE source || ':' || series_id = ANY(%s)",
            (all_refs,),
        ).fetchall()
        found = {r["ref"]: r["last_synced_at"] for r in rows}
        for claim in reading.claims():
            for ref in claim.series:
                source, _, sid = ref.partition(":")
                if ref not in found:
                    errors.append(
                        ReadingError(
                            reading_id,
                            claim.path,
                            f"unknown series '{ref}'",
                            hint=f"Add it with `tempus catalog add {source} {sid} --sync`.",
                        )
                    )
                elif found[ref] is None and claim.status != "unverified":
                    errors.append(
                        ReadingError(
                            reading_id,
                            claim.path,
                            f"series '{ref}' has metadata only (not synced yet)",
                            level="warning",
                            hint=f"Sync it with `tempus catalog add {source} {sid} --sync`.",
                        )
                    )

    return errors


def _series_snapshot(conn: psycopg.Connection, refs: list[str]) -> dict[str, dict]:
    if not refs:
        return {}
    rows = conn.execute(
        """
        SELECT s.source || ':' || s.series_id AS ref, s.source, s.series_id, s.title, s.units,
               latest.ts AS latest_ts, latest.value AS latest_value
        FROM series s
        LEFT JOIN LATERAL (
            SELECT o.ts, o.value
            FROM observations o
            WHERE o.source = s.source AND o.series_id = s.series_id AND o.value IS NOT NULL
            ORDER BY o.ts DESC
            LIMIT 1
        ) latest ON true
        WHERE s.source || ':' || s.series_id = ANY(%s)
        """,
        (refs,),
    ).fetchall()
    return {
        r["ref"]: {
            "ref": r["ref"],
            "source": r["source"],
            "series_id": r["series_id"],
            "title": r["title"],
            "units": r["units"],
            "latest_date": r["latest_ts"].date().isoformat() if r["latest_ts"] else None,
            "latest_value": r["latest_value"],
        }
        for r in rows
    }


def _block_payload(
    block: Block,
    conn: psycopg.Connection,
    cards_repo: CardRepository,
    series_info: dict[str, dict],
) -> dict:
    match block:
        case HeadingBlock(level=level, text=text):
            return {"type": "heading", "level": level, "text": text}
        case ParagraphBlock(text=text):
            return {"type": "paragraph", "text": text}
        case QuoteBlock(text=text):
            return {"type": "quote", "text": text}
        case ListBlock(ordered=ordered, items=items):
            return {"type": "list", "ordered": ordered, "items": list(items)}
        case DividerBlock():
            return {"type": "divider"}
        case CardEmbed(card=cid, caption=caption, start=start, end=end):
            result = (
                run_card(conn, cards_repo, cid, start, end).to_dict()
                if cid in cards_repo.ids()
                else None
            )
            return {
                "type": "card",
                "card": cid,
                "caption": caption,
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "result": result,
            }
        case ClaimBlock(quote=quote, status=status, verdict=verdict, series=refs, card=cid):
            return {
                "type": "claim",
                "quote": quote,
                "status": status,
                "verdict": verdict,
                "series": list(refs),
                "series_info": [series_info[r] for r in refs if r in series_info],
                "card": cid,
            }
        case NoteBlock(title=title, paragraphs=paragraphs):
            return {"type": "note", "title": title, "paragraphs": list(paragraphs)}
    raise AssertionError(f"unexpected block {block!r}")


def run_reading(
    conn: psycopg.Connection,
    readings_repo: ReadingRepository,
    cards_repo: CardRepository,
    reading_id: str,
    *,
    known_concepts: set[str] | None = None,
) -> dict:
    reading, errors = readings_repo.get(reading_id)
    if reading is None:
        return {
            "id": reading_id,
            "meta": None,
            "blocks": [],
            "footnotes": [],
            "word_count": 0,
            "reading_minutes": 1,
            "claim_counts": {s: 0 for s in CLAIM_STATUSES},
            "cards": [],
            "errors": [e.to_dict() for e in errors],
        }

    all_errors = [
        *errors,
        *check_reading(reading_id, reading, cards_repo, conn=conn, known_concepts=known_concepts),
    ]
    all_refs = sorted({ref for claim in reading.claims() for ref in claim.series})
    series_info = _series_snapshot(conn, all_refs)
    blocks = [_block_payload(b, conn, cards_repo, series_info) for b in reading.blocks]

    return {
        "id": reading_id,
        "meta": reading.meta.model_dump(mode="json"),
        "blocks": blocks,
        "footnotes": [{"id": f.id, "text": f.text} for f in reading.footnotes],
        "word_count": reading.word_count(),
        "reading_minutes": reading.reading_minutes(),
        "claim_counts": reading.claim_counts(),
        "cards": reading.card_ids(),
        "errors": [e.to_dict() for e in all_errors],
    }

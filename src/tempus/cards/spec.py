"""The card file format, version 1."""

import re
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from tempus.cards.errors import CardError

CARD_ID = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SERIES_REF = re.compile(r"[a-z][a-z0-9_]*:\S+")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeriesInput(_Strict):
    series: str
    label: str | None = None

    @field_validator("series")
    @classmethod
    def _ref(cls, value: str) -> str:
        if not SERIES_REF.fullmatch(value):
            raise ValueError(f"must look like source:series_id, got {value!r}")
        return value

    @property
    def source(self) -> str:
        return self.series.split(":", 1)[0]

    @property
    def series_id(self) -> str:
        return self.series.split(":", 1)[1]


class ConceptInput(_Strict):
    concept: str
    label: str | None = None


class Window(_Strict):
    start: date | None = None
    end: date | None = None


class Panel(_Strict):
    type: str
    title: str | None = None
    y: list[str] | str | None = None
    data: str | None = None
    change: Literal["yoy", "period"] | None = None


class Card(_Strict):
    version: Literal[1]
    title: str = Field(min_length=1)
    question: str = Field(min_length=1)
    notes: str | None = None
    tags: list[str] = []
    concepts: list[str] = []
    window: Window = Window()
    inputs: dict[str, SeriesInput | ConceptInput]
    formulas: dict[str, str] = {}
    panels: list[Panel] = Field(min_length=1)

    @field_validator("inputs", mode="before")
    @classmethod
    def _shorthand(cls, value):
        if isinstance(value, dict):
            return {k: {"series": v} if isinstance(v, str) else v for k, v in value.items()}
        return value

    @model_validator(mode="after")
    def _names(self):
        for name in [*self.inputs, *self.formulas]:
            if not NAME.fullmatch(name):
                raise ValueError(f"'{name}' is not a valid name (letters, digits, underscores)")
        clash = sorted(set(self.inputs) & set(self.formulas))
        if clash:
            raise ValueError(f"names used for both an input and a formula: {clash}")
        return self


def parse_card(card_id: str, raw: object) -> tuple[Card | None, list[CardError]]:
    if not isinstance(raw, dict):
        return None, [CardError(card_id, "file", "a card must be a YAML mapping")]
    try:
        return Card.model_validate(raw), []
    except ValidationError as exc:
        return None, [CardError(card_id, _loc(e["loc"]), e["msg"]) for e in exc.errors()]


def load_card(path: Path) -> tuple[Card | None, list[CardError]]:
    card_id = path.stem
    if not CARD_ID.fullmatch(card_id):
        msg = "card file names must be kebab-case, e.g. crude-to-gasoline.yaml"
        return None, [CardError(card_id, "file", msg)]
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        return None, [CardError(card_id, "file", f"invalid YAML: {exc}")]
    return parse_card(card_id, raw)


def _loc(loc: tuple) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "file"

"""Errors are collected, not raised, so one pass reports everything wrong with a card."""

import difflib
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Literal


@dataclass(frozen=True)
class CardError:
    card: str
    path: str  # e.g. "formulas.spread", "panels[1]", "file"
    message: str
    level: Literal["error", "warning"] = "error"
    hint: str | None = None

    def format(self) -> str:
        text = f"cards/{self.card}.yaml · {self.path}\n  {self.message}"
        return f"{text} {self.hint}" if self.hint else text

    def to_dict(self) -> dict:
        return asdict(self)


def has_errors(errors: Iterable[CardError]) -> bool:
    return any(e.level == "error" for e in errors)


def did_you_mean(name: str, options: Iterable[str]) -> str | None:
    match = difflib.get_close_matches(name, list(options), n=1)
    return f"Did you mean '{match[0]}'?" if match else None

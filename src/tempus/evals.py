"""The drafting eval set (evals/drafting.yaml): parse, validate offline.

Spec: docs/specs/2026-10-05-question-to-card-design.md → The eval file.
"""

from dataclasses import dataclass

EVALS_PATH = "evals/drafting.yaml"


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    shape: str  # a SHAPES name
    roles: tuple[tuple[str, ...], ...]  # acceptable series refs per role, in the shape's role order
    also: tuple[str, ...] = ()  # other shapes that also answer it
    xfail: str | None = None  # why tempus can't answer it well yet

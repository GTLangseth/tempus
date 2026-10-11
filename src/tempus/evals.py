"""The drafting eval set (evals/drafting.yaml): parse, validate offline.

Spec: docs/specs/2026-10-05-question-to-card-design.md → The eval file.
"""

import difflib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from tempus import catalog
from tempus.drafting import SHAPES

EVALS_PATH = "evals/drafting.yaml"
VERSION = 1
REQUIRED = ("id", "question", "shape", "roles")
OPTIONAL = ("also", "xfail")


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    shape: str  # a SHAPES name
    roles: tuple[tuple[str, ...], ...]  # acceptable series refs per role, in the shape's role order
    also: tuple[str, ...] = ()  # other shapes that also answer it
    xfail: str | None = None  # why tempus can't answer it well yet


class EvalFileError(ValueError):
    """The eval file can't be parsed into cases."""


def load_cases(path: Path) -> list[Case]:
    doc = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(doc, dict) or doc.get("version") != VERSION:
        raise EvalFileError(f"{Path(path).name}: expected `version: {VERSION}`")
    if extra := sorted(set(doc) - {"version", "cases"}):
        raise EvalFileError(f"{Path(path).name}: unknown key {extra[0]!r}")
    cases: list[Case] = []
    seen: set[str] = set()
    for i, raw in enumerate(doc.get("cases") or []):
        case = _parse_case(raw, f"case {i + 1}")
        if case.id in seen:
            raise EvalFileError(f"{case.id}: duplicate id")
        seen.add(case.id)
        cases.append(case)
    return cases


def _parse_case(raw, where: str) -> Case:
    if not isinstance(raw, dict):
        raise EvalFileError(f"{where}: expected a mapping")
    where = str(raw.get("id") or where)
    if extra := sorted(set(raw) - set(REQUIRED) - set(OPTIONAL)):
        raise EvalFileError(f"{where}: unknown key {extra[0]!r}")
    for key in REQUIRED:
        if not raw.get(key):
            raise EvalFileError(f"{where}: missing {key}")
    roles = raw["roles"]
    if not isinstance(roles, list):
        raise EvalFileError(f"{where}: roles must be a list of series lists")
    for n, role in enumerate(roles, 1):
        if not isinstance(role, list):
            raise EvalFileError(f"{where}: roles[{n}] must be a list of series")
        if not role:
            raise EvalFileError(f"{where}: roles[{n}] is empty")
    return Case(
        id=str(raw["id"]),
        question=str(raw["question"]),
        shape=str(raw["shape"]),
        roles=tuple(tuple(str(ref) for ref in role) for role in roles),
        also=tuple(raw.get("also") or ()),
        xfail=raw.get("xfail"),
    )


def known_series(snapshot_path: Path, catalog_path: Path) -> set[str]:
    snapshot = json.loads(Path(snapshot_path).read_text())
    refs = {f"{snapshot['source']}:{s['id']}" for s in snapshot["series"]}
    return refs | {f"{e.source}:{e.id}" for e in catalog.load(Path(catalog_path))}


def check_cases(cases: list[Case], known: set[str]) -> list[str]:
    problems: list[str] = []
    for case in cases:
        shape = SHAPES.get(case.shape)
        if shape is None:
            problems.append(f"{case.id}: unknown shape {case.shape!r}{_did_you_mean(case.shape)}")
        elif len(case.roles) != len(shape.roles):
            problems.append(
                f"{case.id}: {case.shape} has {len(shape.roles)} roles, got {len(case.roles)}"
            )
        for other in case.also:
            if other == case.shape:
                problems.append(f"{case.id}: also repeats shape {other!r}")
            elif other not in SHAPES:
                problems.append(
                    f"{case.id}: also names unknown shape {other!r}{_did_you_mean(other)}"
                )
        for n, role in enumerate(case.roles, 1):
            problems.extend(
                f"{case.id}: roles[{n}] {ref} is not in the snapshot or catalog"
                for ref in role
                if ref not in known
            )
    return problems


def _did_you_mean(name: str) -> str:
    close = difflib.get_close_matches(name, SHAPES, n=1)
    return f" (did you mean {close[0]!r}?)" if close else ""

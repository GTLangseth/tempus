# Cards Slice 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Write a card as YAML, check it, and see it rendered as real charts in a browser that reloads on save.

**Architecture:** A `tempus.cards` package parses card YAML (pydantic), parses formulas with
`ast` and an allow-list, checks them statically, resolves references against Postgres,
evaluates them with Polars, and renders renderer-neutral panel payloads. A FastAPI app
(`tempus serve`) serves those payloads, plus a no-build static front end that draws them
with ECharts and reloads over server-sent events.

**Tech Stack:** Python 3.12, uv, typer, rich, psycopg 3, PyYAML, pydantic 2, **Polars**,
**FastAPI**, **uvicorn**, **watchfiles**, plain ES modules, ECharts 5 from a CDN.

**Spec:** `docs/specs/2026-10-04-cards-slice-1-design.md`. Read it before starting any task.

## Global Constraints

- Card files: `cards/<id>.yaml`. The ID is the filename, kebab-case (`[a-z0-9]+(-[a-z0-9]+)*`). `version: 1` is required.
- Top-level card keys are exactly: `version, title, question, notes, tags, concepts, window, inputs, formulas, panels`. Unknown keys are an error.
- `question` is required. `panels` has at least one entry.
- Formulas are parsed with `ast.parse(text, mode="eval")` and **never evaluated as Python**. Allowed: names, numbers, strings, `+ - * /`, unary `-`, plain-name calls with positional and keyword args.
- Binary operations need matching frequencies; nothing is filled implicitly; operands are inner-joined on `ts`.
- `lagcorr(x, y, nlags=12)`: positive lag k means **x leads y by k periods**.
- Errors are collected (`list[CardError]`), never raised one at a time, and carry `card`, `path`, `level`, `message` and `hint`.
- Panel payloads carry `"payload_version": 1`.
- New dependencies: `polars`, `fastapi`, `uvicorn[standard]`, `watchfiles`. No npm; ECharts is pinned at `https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js`.
- `mise run test` and `mise run lint` must pass after every task. Ruff line length is 100.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Work on branch `grantlangseth/spa-25-card-language-v0-spec-schema-and-validator`, or on a sub-issue's Linear branch if sub-issues exist.

## Review Focus

1. **A series with no data in the card window** (for example, the window starts after the series ends): panels render with a "no data in the window" warning instead of crashing. Test in Task 8.
2. **Weekly FRED series** (`frequency = "Weekly, Ending Monday"`): map to `W`. `yoy` on weekly uses a 52-week offset, so the weekdays line up. Tests in Tasks 3 and 7.
3. **A card file that's broken YAML, or not a mapping, saved while `tempus serve` runs**: it reports a `CardError` (shown in the browser), never a traceback. Tests in Tasks 1 and 10.
4. **A concept input that expands to zero series**: resolve error "concept 'x' has no series filed under it". Test in Task 7.
5. **A series reference that isn't in the database** (typo, or not synced): resolve error with a did-you-mean hint, or the `tempus catalog add … --sync` command. Test in Task 7.

---

## File structure

```
src/tempus/cards/
├── __init__.py      # empty
├── errors.py        # CardError, has_errors, did_you_mean
├── spec.py          # pydantic Card models, load_card, parse_card
├── expr.py          # ast → expression nodes (allow-list)
├── values.py        # Series, SeriesSet, Table, EvalError, frequency helpers
├── functions.py     # function catalog + Polars implementations + binop + call
├── panels.py        # panel catalog
├── check.py         # static checks (no DB), formula order, type inference
├── repository.py    # CardRepository, FileCardRepository, sync_index
├── resolve.py       # references → SeriesInfo via DB
├── evaluate.py      # fetch observations, evaluate formulas
├── render.py        # panel payloads
└── run.py           # run_card orchestration, CardResult
src/tempus/app.py    # FastAPI app: API + SSE + static files
src/tempus/web/      # index.html, app.js, adapters/echarts.js, theme.json, style.css
cards/               # crude-to-gasoline.yaml, coffee-pass-through.yaml
tests/conftest.py    # shared DB fixture (moved from test_sync.py)
tests/cards/         # unit + integration tests, bad/*.yaml
```

---

### Task 1: Card errors and the card file model

**Files:**
- Modify: `pyproject.toml` (no new deps yet; pydantic is already present through pydantic-settings)
- Create: `src/tempus/cards/__init__.py`, `src/tempus/cards/errors.py`, `src/tempus/cards/spec.py`
- Test: `tests/__init__.py`, `tests/cards/__init__.py`, `tests/cards/test_spec.py`
  (both `__init__.py` files are empty; they make test modules `tests.cards.*`, so they can't collide with the repo's `cards/` directory)

**Interfaces:**
- Produces:
  - `CardError(card: str, path: str, message: str, level: Literal["error","warning"]="error", hint: str|None=None)` with `.format() -> str` and `.to_dict() -> dict`
  - `has_errors(errors: Iterable[CardError]) -> bool`
  - `did_you_mean(name: str, options: Iterable[str]) -> str | None`
  - `Card`, `SeriesInput` (`.source`, `.series_id`), `ConceptInput`, `Window`, `Panel`
  - `parse_card(card_id: str, raw: object) -> tuple[Card | None, list[CardError]]`
  - `load_card(path: Path) -> tuple[Card | None, list[CardError]]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/cards/test_spec.py
from pathlib import Path

from tempus.cards.errors import CardError, did_you_mean, has_errors
from tempus.cards.spec import ConceptInput, SeriesInput, load_card, parse_card

VALID = {
    "version": 1,
    "title": "Crude → gasoline",
    "question": "Does crude lead gasoline CPI?",
    "inputs": {
        "crude": "fred:DCOILWTICO",
        "gasoline": {"series": "fred:CUSR0000SETB01", "label": "CPI gasoline"},
        "energy": {"concept": "prices.consumer.energy"},
    },
    "formulas": {"crude_yoy": 'yoy(resample(crude, "M"))'},
    "panels": [{"type": "timeseries", "y": ["crude_yoy"]}],
}


def test_valid_card_parses_with_shorthand_inputs():
    card, errors = parse_card("crude-to-gasoline", VALID)
    assert errors == []
    assert isinstance(card.inputs["crude"], SeriesInput)
    assert (card.inputs["crude"].source, card.inputs["crude"].series_id) == ("fred", "DCOILWTICO")
    assert card.inputs["gasoline"].label == "CPI gasoline"
    assert isinstance(card.inputs["energy"], ConceptInput)


def test_missing_question_and_unknown_key_are_both_reported():
    raw = {k: v for k, v in VALID.items() if k != "question"} | {"colour": "red"}
    card, errors = parse_card("c", raw)
    assert card is None
    paths = {e.path for e in errors}
    assert {"question", "colour"} <= paths


def test_bad_series_reference():
    raw = VALID | {"inputs": {"crude": "DCOILWTICO"}}
    _, errors = parse_card("c", raw)
    assert any("source:series_id" in e.message for e in errors)


def test_name_used_for_input_and_formula():
    raw = VALID | {"formulas": {"crude": "yoy(gasoline)"}}
    _, errors = parse_card("c", raw)
    assert any("both an input and a formula" in e.message for e in errors)


def test_version_must_be_1():
    _, errors = parse_card("c", VALID | {"version": 2})
    assert errors and errors[0].path == "version"


def test_load_card_rejects_non_kebab_filename(tmp_path: Path):
    path = tmp_path / "Crude_Card.yaml"
    path.write_text("version: 1\n")
    card, errors = load_card(path)
    assert card is None and "kebab-case" in errors[0].message


def test_load_card_reports_broken_yaml(tmp_path: Path):
    path = tmp_path / "broken.yaml"
    path.write_text("title: [unclosed\n")
    card, errors = load_card(path)
    assert card is None and errors[0].message.startswith("invalid YAML")


def test_load_card_reports_non_mapping(tmp_path: Path):
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n")
    _, errors = load_card(path)
    assert errors[0].message == "a card must be a YAML mapping"


def test_error_helpers():
    err = CardError("c", "formulas.x", "unknown name 'gasolin'.", hint="Did you mean 'gasoline'?")
    assert err.format() == (
        "cards/c.yaml · formulas.x\n  unknown name 'gasolin'. Did you mean 'gasoline'?"
    )
    assert has_errors([err]) and not has_errors([CardError("c", "p", "m", level="warning")])
    assert did_you_mean("gasolin", ["gasoline", "crude"]) == "Did you mean 'gasoline'?"
    assert did_you_mean("zzz", ["gasoline"]) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/cards/test_spec.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tempus.cards'`

- [ ] **Step 3: Implement**

```python
# src/tempus/cards/__init__.py
"""Cards: saved, named views over series (see docs/specs/2026-10-04-cards-slice-1-design.md)."""
```

```python
# src/tempus/cards/errors.py
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
```

```python
# src/tempus/cards/spec.py
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/cards/test_spec.py -v`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
touch tests/__init__.py tests/cards/__init__.py
mise run lint
git add src/tempus/cards tests/__init__.py tests/cards
git commit -m "Cards: error type and card file model (v1)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Formula parser with an allow-list

**Files:**
- Create: `src/tempus/cards/expr.py`
- Test: `tests/cards/test_expr.py`

**Interfaces:**
- Produces:
  - Node types (frozen dataclasses): `Name(id: str)`, `Num(value: int | float)`, `Str(value: str)`, `BinOp(op: str, left: Node, right: Node)`, `Neg(operand: Node)`, `Call(func: str, args: tuple[Node, ...], kwargs: tuple[tuple[str, Node], ...])`; `Node` is their union
  - `ExprError(ValueError)`
  - `parse(text: str) -> Node`
  - `names(node: Node) -> set[str]`: referenced names, excluding function names

- [ ] **Step 1: Write the failing tests**

```python
# tests/cards/test_expr.py
import pytest

from tempus.cards.expr import BinOp, Call, ExprError, Name, Neg, Num, Str, names, parse


def test_parses_calls_kwargs_and_arithmetic():
    node = parse('zscore(yoy(a)) - lagcorr(a, b, nlags=12) / 2')
    assert isinstance(node, BinOp) and node.op == "-"
    right = node.right
    assert right.op == "/" and right.right == Num(2)
    call = right.left
    assert call == Call("lagcorr", (Name("a"), Name("b")), (("nlags", Num(12)),))


def test_strings_negation_and_names():
    node = parse('-resample(crude, "M", agg="last")')
    assert isinstance(node, Neg)
    assert node.operand.args[1] == Str("M")
    assert names(parse('lagcorr(yoy(a), b) + c * 2')) == {"a", "b", "c"}


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("a.b", "attribute access"),
        ("a[0]", "indexing"),
        ("a > b", "comparisons"),
        ("lambda x: x", "lambdas"),
        ("[x for x in a]", "comprehensions"),
        ("a ** 2", "BinOp"),
        ("f(*a)", "*args"),
        ("obj.method(a)", "plain function calls"),
        ("True", "booleans"),
        ("yoy(", "syntax error"),
    ],
)
def test_rejects_everything_else(text, fragment):
    with pytest.raises(ExprError) as info:
        parse(text)
    assert fragment in str(info.value)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/cards/test_expr.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# src/tempus/cards/expr.py
"""Formulas are a restricted subset of Python expressions, parsed but never evaluated.

Allowed: names, numbers, strings, + - * /, unary -, and plain-name calls with positional and
keyword arguments. Everything else is rejected with a message naming the construct.
"""

import ast
from dataclasses import dataclass


class ExprError(ValueError):
    pass


@dataclass(frozen=True)
class Name:
    id: str


@dataclass(frozen=True)
class Num:
    value: int | float


@dataclass(frozen=True)
class Str:
    value: str


@dataclass(frozen=True)
class BinOp:
    op: str
    left: "Node"
    right: "Node"


@dataclass(frozen=True)
class Neg:
    operand: "Node"


@dataclass(frozen=True)
class Call:
    func: str
    args: tuple["Node", ...]
    kwargs: tuple[tuple[str, "Node"], ...]


Node = Name | Num | Str | BinOp | Neg | Call

_OPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
_DESCRIBE = {
    ast.Attribute: "attribute access (a.b)",
    ast.Subscript: "indexing (a[0])",
    ast.Compare: "comparisons",
    ast.BoolOp: "and/or",
    ast.Lambda: "lambdas",
    ast.ListComp: "comprehensions",
    ast.GeneratorExp: "comprehensions",
    ast.DictComp: "comprehensions",
    ast.SetComp: "comprehensions",
    ast.IfExp: "conditional expressions",
}


def parse(text: str) -> Node:
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"syntax error: {exc.msg}") from exc
    return _convert(tree.body)


def _convert(node: ast.AST) -> Node:
    match node:
        case ast.Name(id=name):
            return Name(name)
        case ast.Constant(value=bool()):
            raise ExprError("booleans aren't supported")
        case ast.Constant(value=int() | float() as value):
            return Num(value)
        case ast.Constant(value=str() as value):
            return Str(value)
        case ast.BinOp(op=op, left=left, right=right) if type(op) in _OPS:
            return BinOp(_OPS[type(op)], _convert(left), _convert(right))
        case ast.UnaryOp(op=ast.USub(), operand=operand):
            return Neg(_convert(operand))
        case ast.Call(func=ast.Name(id=func), args=args, keywords=keywords):
            if any(isinstance(a, ast.Starred) for a in args) or any(k.arg is None for k in keywords):
                raise ExprError("*args and **kwargs aren't supported")
            return Call(
                func,
                tuple(_convert(a) for a in args),
                tuple((k.arg, _convert(k.value)) for k in keywords),
            )
        case ast.Call():
            raise ExprError("only plain function calls like yoy(x) are allowed")
    described = _DESCRIBE.get(type(node), type(node).__name__)
    if isinstance(node, ast.BinOp):
        described = f"BinOp {type(node.op).__name__}"
    raise ExprError(f"{described} isn't allowed in formulas")


def names(node: Node) -> set[str]:
    match node:
        case Name(id=name):
            return {name}
        case BinOp(left=left, right=right):
            return names(left) | names(right)
        case Neg(operand=operand):
            return names(operand)
        case Call(args=args, kwargs=kwargs):
            out: set[str] = set()
            for arg in [*args, *(v for _, v in kwargs)]:
                out |= names(arg)
            return out
    return set()
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/cards/test_expr.py -v`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
mise run lint
git add src/tempus/cards/expr.py tests/cards/test_expr.py
git commit -m "Cards: formula parser with an ast allow-list

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Value types and the time-shape and change functions

**Files:**
- Modify: `pyproject.toml` (add `"polars>=1.30"` to `dependencies`), then run `uv sync`
- Modify: `docs/STACK.md` (Register: add a Polars row)
- Create: `src/tempus/cards/values.py`, `src/tempus/cards/functions.py`
- Test: `tests/cards/test_functions.py`

**Interfaces:**
- Produces (`values.py`):
  - `FREQS = ("D","W","M","Q","Y")`, `FREQ_ORDER: dict[str,int]`
  - `EvalError(ValueError)` with `.hint: str | None`
  - `Series(name: str, label: str, freq: str, df: pl.DataFrame, units: str | None = None)`; `df` has columns `ts: Date` and `value: Float64`, sorted by `ts`
  - `SeriesSet(name: str, members: tuple[Series, ...])`
  - `Table(name: str, df: pl.DataFrame, meta: dict)`
  - `Value = Series | SeriesSet | Table | int | float | str`
  - `frame(points: list[tuple[date, float | None]]) -> pl.DataFrame`
  - `freq_from_fred(text: str | None) -> str`: `D/W/M/Q/Y`, or `"irregular"`
  - `align(a: Series, b: Series, what: str) -> pl.DataFrame` with columns `ts, a, b`; raises `EvalError` on mismatched frequencies
  - `clip(s: Series, start: date | None, end: date | None) -> Series`
- Produces (`functions.py`):
  - `Param(name, kind, default=REQUIRED, choices=None)` where kind ∈ `"series","set","int","number","str","date"`
  - `Function(name, params, returns, doc, caveat, impl, broadcast=True, windowed=False)` where returns ∈ `"same","series","lagcorr"`
  - `CATALOG: dict[str, Function]`, `REQUIRED`
  - `match_args(fn, args: list, kwargs: dict) -> dict[str, object]` (raises `ArgError(ValueError)`)
  - `call(fn: Function, args: list[Value], kwargs: dict[str, Value]) -> Value`
  - `signature(fn: Function) -> str`
  - Functions in this task: `resample, shift, fill, diff, pct_change, yoy, log`

- [ ] **Step 1: Add Polars**

In `pyproject.toml` `dependencies`, add `"polars>=1.30",`, then run `uv sync`.

In `docs/STACK.md` → Register, add this row after the DB driver row:

```markdown
| Card engine dataframes | Polars | OSI (MIT) | Fast, strict types; its expression API matches the formula style. Card functions are small Polars functions | pandas | A function needs a library that only speaks pandas (convert at that function's boundary first) |
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/cards/test_functions.py
from datetime import date

import pytest

from tempus.cards.functions import CATALOG, ArgError, call, match_args, signature
from tempus.cards.values import EvalError, Series, SeriesSet, align, frame, freq_from_fred


def series(name, freq, points, label=None):
    return Series(name, label or name, freq, frame(points))


def values(s):
    return s.df["value"].to_list()


def monthly(name, vals, start_year=2020):
    pts = [(date(start_year + i // 12, i % 12 + 1, 1), v) for i, v in enumerate(vals)]
    return series(name, "M", pts)


def fn(name, *args, **kwargs):
    return call(CATALOG[name], list(args), kwargs)


def test_freq_from_fred():
    assert freq_from_fred("Daily") == "D"
    assert freq_from_fred("Weekly, Ending Monday") == "W"
    assert freq_from_fred("Monthly") == "M"
    assert freq_from_fred("Quarterly") == "Q"
    assert freq_from_fred("Annual") == "Y"
    assert freq_from_fred(None) == "irregular"


def test_resample_daily_to_monthly_mean_and_last():
    d = series("d", "D", [(date(2020, 1, 2), 1.0), (date(2020, 1, 3), None),
                          (date(2020, 1, 31), 3.0), (date(2020, 2, 3), 5.0)])
    m = fn("resample", d, "M")
    assert m.freq == "M"
    assert m.df["ts"].to_list() == [date(2020, 1, 1), date(2020, 2, 1)]
    assert values(m) == [2.0, 5.0]
    assert values(fn("resample", d, "M", agg="last")) == [3.0, 5.0]


def test_resample_sum_of_all_null_period_is_null_not_zero():
    d = series("d", "D", [(date(2020, 1, 2), None), (date(2020, 2, 3), 2.0)])
    assert values(fn("resample", d, "M", agg="sum")) == [None, 2.0]


def test_resample_refuses_to_make_finer():
    with pytest.raises(EvalError, match="coarser"):
        fn("resample", monthly("m", [1.0]), "D")


def test_shift_diff_pct_change_fill_log():
    m = monthly("m", [1.0, 2.0, None, 4.0])
    assert values(fn("shift", m, 1)) == [None, 1.0, 2.0, None]
    assert values(fn("diff", m)) == [None, 1.0, None, None]
    assert values(fn("pct_change", monthly("m", [2.0, 3.0, 0.0, 1.0]))) == [None, 50.0, -100.0, None]
    assert values(fn("fill", m, "ffill")) == [1.0, 2.0, 2.0, 4.0]
    assert values(fn("fill", m, "linear")) == [1.0, 2.0, 3.0, 4.0]
    with pytest.raises(EvalError, match="positive"):
        fn("log", monthly("m", [1.0, 0.0]))


def test_yoy_monthly_matches_by_date_even_with_a_gap():
    vals = [100.0] * 12 + [110.0] * 12
    m = monthly("m", vals)
    m = Series("m", "m", "M", m.df.filter(m.df["ts"] != date(2021, 3, 1)))  # drop one month
    out = fn("yoy", m)
    by_date = dict(zip(out.df["ts"].to_list(), values(out), strict=True))
    assert by_date[date(2021, 1, 1)] == pytest.approx(10.0)
    assert by_date[date(2021, 4, 1)] == pytest.approx(10.0)  # not shifted by the gap
    assert date(2021, 3, 1) not in by_date


def test_yoy_weekly_uses_52_weeks():
    mondays = [date(2020, 1, 6), date(2021, 1, 4)]  # 52 weeks apart, both Mondays
    w = series("w", "W", [(mondays[0], 2.0), (mondays[1], 3.0)])
    assert values(fn("yoy", w))[1] == pytest.approx(50.0)


def test_yoy_on_daily_asks_for_resample():
    d = series("crude", "D", [(date(2020, 1, 2), 1.0)])
    with pytest.raises(EvalError) as info:
        fn("yoy", d)
    assert info.value.hint == 'Resample first: resample(crude, "M")'


def test_functions_broadcast_over_sets():
    s = SeriesSet("set", (monthly("a", [1.0, 2.0]), monthly("b", [2.0, 4.0])))
    out = fn("diff", s)
    assert isinstance(out, SeriesSet)
    assert [values(m) for m in out.members] == [[None, 1.0], [None, 2.0]]


def test_align_rejects_mismatched_frequencies_with_hint():
    d = series("crude", "D", [(date(2020, 1, 2), 1.0)])
    with pytest.raises(EvalError) as info:
        align(d, monthly("gas", [1.0]), "crude / gas")
    assert "frequencies differ (D vs M)" in str(info.value)
    assert info.value.hint == 'Resample first: resample(crude, "M")'


def test_match_args_and_signature():
    f = CATALOG["resample"]
    assert match_args(f, ["x", "M"], {}) == {"x": "x", "freq": "M", "agg": "mean"}
    with pytest.raises(ArgError, match="unknown argument 'how'"):
        match_args(f, ["x", "M"], {"how": "sum"})
    with pytest.raises(ArgError, match="missing argument 'freq'"):
        match_args(f, ["x"], {})
    with pytest.raises(ArgError, match="too many"):
        match_args(f, ["x", "M", "mean", 1], {})
    assert signature(f) == 'resample(x, freq, agg="mean")'
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/cards/test_functions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tempus.cards.values'`

- [ ] **Step 4: Implement `values.py`**

```python
# src/tempus/cards/values.py
"""The values formulas compute: series, sets of series, and tables."""

from dataclasses import dataclass, field
from datetime import date

import polars as pl

FREQS = ("D", "W", "M", "Q", "Y")
FREQ_ORDER = {f: i for i, f in enumerate(FREQS)}
SCHEMA = {"ts": pl.Date, "value": pl.Float64}


class EvalError(ValueError):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class Series:
    name: str
    label: str
    freq: str  # one of FREQS, or "irregular"
    df: pl.DataFrame  # ts: Date, value: Float64, sorted by ts
    units: str | None = None


@dataclass(frozen=True)
class SeriesSet:
    name: str
    members: tuple[Series, ...]


@dataclass(frozen=True)
class Table:
    name: str
    df: pl.DataFrame
    meta: dict = field(default_factory=dict)


Value = Series | SeriesSet | Table | int | float | str


def frame(points: list[tuple[date, float | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"ts": [p[0] for p in points], "value": [p[1] for p in points]}, schema=SCHEMA
    ).sort("ts")


def freq_from_fred(text: str | None) -> str:
    for prefix, freq in (("Daily", "D"), ("Weekly", "W"), ("Monthly", "M"),
                         ("Quarterly", "Q"), ("Annual", "Y")):
        if text and text.startswith(prefix):
            return freq
    return "irregular"


def resample_hint(a: Series, b: Series) -> str | None:
    if "irregular" in (a.freq, b.freq):
        return None
    finer, coarser = sorted((a, b), key=lambda s: FREQ_ORDER[s.freq])
    return f'Resample first: resample({finer.name}, "{coarser.freq}")'


def align(a: Series, b: Series, what: str) -> pl.DataFrame:
    if a.freq != b.freq:
        raise EvalError(f"{what} — frequencies differ ({a.freq} vs {b.freq}).",
                        hint=resample_hint(a, b))
    return (
        a.df.join(b.df, on="ts", how="inner", suffix="_b")
        .rename({"value": "a", "value_b": "b"})
        .sort("ts")
    )


def clip(s: Series, start: date | None, end: date | None) -> Series:
    df = s.df
    if start:
        df = df.filter(pl.col("ts") >= start)
    if end:
        df = df.filter(pl.col("ts") <= end)
    return Series(s.name, s.label, s.freq, df, s.units)
```

- [ ] **Step 5: Implement `functions.py` (catalog machinery + this task's functions)**

```python
# src/tempus/cards/functions.py
"""The function catalog: every function a formula may call.

Each entry has a typed signature, a doc line and a caveat (what it does and when it misleads).
Names are borrowed from pandas, Datadog and PromQL (see docs/STACK.md → Inspirations).
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date

import polars as pl

from tempus.cards.values import FREQ_ORDER, FREQS, EvalError, Series, SeriesSet, Value

REQUIRED = object()
TRUNCATE = {"D": "1d", "W": "1w", "M": "1mo", "Q": "1q", "Y": "1y"}
AGGS = ("mean", "last", "sum", "min", "max")


class ArgError(ValueError):
    pass


@dataclass(frozen=True)
class Param:
    name: str
    kind: str  # series | set | int | number | str | date
    default: object = REQUIRED
    choices: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Function:
    name: str
    params: tuple[Param, ...]
    returns: str  # same | series | lagcorr
    doc: str
    caveat: str
    impl: Callable[..., Value]
    broadcast: bool = True  # applies per member when the first argument is a SeriesSet
    windowed: bool = False  # series arguments are clipped to the card window first


CATALOG: dict[str, Function] = {}


def register(fn: Function) -> Function:
    CATALOG[fn.name] = fn
    return fn


def match_args(fn: Function, args: list, kwargs: dict) -> dict[str, object]:
    if len(args) > len(fn.params):
        raise ArgError(f"too many arguments for {signature(fn)}")
    bound = {p.name: a for p, a in zip(fn.params, args, strict=False)}
    known = {p.name for p in fn.params}
    for key, value in kwargs.items():
        if key not in known:
            raise ArgError(f"unknown argument '{key}' for {signature(fn)}")
        if key in bound:
            raise ArgError(f"argument '{key}' given twice")
        bound[key] = value
    for p in fn.params:
        if p.name not in bound:
            if p.default is REQUIRED:
                raise ArgError(f"missing argument '{p.name}' for {signature(fn)}")
            bound[p.name] = p.default
    return bound


def signature(fn: Function) -> str:
    parts = []
    for p in fn.params:
        if p.default is REQUIRED:
            parts.append(p.name)
        elif isinstance(p.default, str):
            parts.append(f'{p.name}="{p.default}"')
        else:
            parts.append(f"{p.name}={p.default}")
    return f"{fn.name}({', '.join(parts)})"


def _coerce(fn: Function, p: Param, value: object, first: bool) -> object:
    ok = {
        "series": isinstance(value, Series) or (first and fn.broadcast and isinstance(value, SeriesSet)),
        "set": isinstance(value, SeriesSet),
        "int": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "str": isinstance(value, str),
        "date": isinstance(value, str),
    }[p.kind]
    if value is None and p.default is None:
        return None
    if not ok:
        raise EvalError(f"{fn.name}: '{p.name}' must be a {p.kind}")
    if p.choices and value not in p.choices:
        raise EvalError(f"{fn.name}: '{p.name}' must be one of {', '.join(p.choices)}")
    if p.kind == "date":
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise EvalError(f"{fn.name}: '{p.name}' must be a date like 2000-01-01") from exc
    return value


def call(fn: Function, args: list[Value], kwargs: dict[str, Value]) -> Value:
    try:
        bound = match_args(fn, args, kwargs)
    except ArgError as exc:
        raise EvalError(str(exc)) from exc
    bound = {p.name: _coerce(fn, p, bound[p.name], i == 0) for i, p in enumerate(fn.params)}
    first = bound[fn.params[0].name]
    if isinstance(first, SeriesSet) and fn.params[0].kind == "series":
        members = tuple(fn.impl(**{**bound, fn.params[0].name: m}) for m in first.members)
        return SeriesSet(first.name, members)
    return fn.impl(**bound)


def _with(x: Series, expr: pl.Expr, label: str, freq: str | None = None) -> Series:
    df = x.df.select("ts", expr.alias("value"))
    return replace(x, df=df, label=label, freq=freq or x.freq)


def _safe_ratio(num: pl.Expr, den: pl.Expr) -> pl.Expr:
    return pl.when(den == 0).then(None).otherwise(num / den)


# --- time shape -------------------------------------------------------------------------


def _resample(x: Series, freq: str, agg: str) -> Series:
    if x.freq == freq:
        return x
    if x.freq != "irregular" and FREQ_ORDER[freq] < FREQ_ORDER[x.freq]:
        raise EvalError(f"resample can only make a series coarser ({x.freq} → {freq} is finer)")
    v = pl.col("value")
    expr = {
        "mean": v.mean(),
        "last": v.drop_nulls().last(),
        "sum": pl.when(v.count() > 0).then(v.sum()),
        "min": v.min(),
        "max": v.max(),
    }[agg]
    df = (
        x.df.sort("ts")
        .group_by(pl.col("ts").dt.truncate(TRUNCATE[freq]), maintain_order=True)
        .agg(expr.alias("value"))
        .sort("ts")
    )
    return replace(x, df=df, freq=freq)


def _shift(x: Series, n: int) -> Series:
    return _with(x, pl.col("value").shift(n), f"{x.label}, shifted {n}")


def _fill(x: Series, method: str) -> Series:
    v = pl.col("value")
    expr = v.fill_null(strategy="forward") if method == "ffill" else v.interpolate()
    return _with(x, expr, x.label)


# --- change -----------------------------------------------------------------------------


def _diff(x: Series, n: int) -> Series:
    return _with(x, pl.col("value").diff(n), f"{x.label}, change")


def _pct_change(x: Series, n: int) -> Series:
    v = pl.col("value")
    return _with(x, (_safe_ratio(v, v.shift(n)) - 1) * 100, f"{x.label}, % change")


def _yoy(x: Series) -> Series:
    if x.freq not in ("W", "M", "Q", "Y"):
        raise EvalError(
            f"yoy needs a weekly, monthly, quarterly or annual series; '{x.name}' is {x.freq}.",
            hint=f'Resample first: resample({x.name}, "M")',
        )
    offset = "52w" if x.freq == "W" else "1y"
    prior = x.df.with_columns(pl.col("ts").dt.offset_by(offset)).rename({"value": "prior"})
    df = x.df.join(prior, on="ts", how="left").select(
        "ts", ((_safe_ratio(pl.col("value"), pl.col("prior")) - 1) * 100).alias("value")
    )
    return replace(x, df=df.sort("ts"), label=f"{x.label}, YoY %", units="%")


def _log(x: Series) -> Series:
    if (x.df["value"] <= 0).any():
        raise EvalError(f"log needs positive values; '{x.name}' has values ≤ 0")
    return _with(x, pl.col("value").log(), f"log {x.label}")


X = Param("x", "series")

register(Function("resample", (X, Param("freq", "str", choices=FREQS),
                               Param("agg", "str", "mean", AGGS)), "same",
                  "Change frequency to a coarser one (D/W/M/Q/Y), aggregating each period.",
                  "Timestamps mark the start of each period, as FRED does. 'mean' smooths "
                  "spikes; use 'last' for end-of-period levels.", _resample))
register(Function("shift", (X, Param("n", "int")), "same",
                  "Move values n periods later (negative n moves them earlier).",
                  "Shifts by rows, so gaps in the series shift with it.", _shift))
register(Function("fill", (X, Param("method", "str", choices=("ffill", "linear"))), "same",
                  "Fill gaps by carrying the last value forward or interpolating linearly.",
                  "The only way gaps get filled. Filled points look like real data on charts.",
                  _fill))
register(Function("diff", (X, Param("n", "int", 1)), "same",
                  "Change from n periods earlier.",
                  "Works by rows; a gap makes the next change span two periods.", _diff))
register(Function("pct_change", (X, Param("n", "int", 1)), "same",
                  "Percent change from n periods earlier (×100).",
                  "Undefined when the earlier value is 0 (left as a gap).", _pct_change))
register(Function("yoy", (X,), "same",
                  "Percent change from the same date a year earlier (52 weeks for weekly).",
                  "Matches by date, not row, so gaps don't misalign it. Daily series must be "
                  "resampled first.", _yoy))
register(Function("log", (X,), "same", "Natural log.",
                  "Needs positive values. Differences of logs ≈ percent changes for small moves.",
                  _log))
```

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/cards/test_functions.py -v`
Expected: PASS

- [ ] **Step 7: Lint and commit**

```bash
mise run lint
git add pyproject.toml uv.lock docs/STACK.md src/tempus/cards/values.py src/tempus/cards/functions.py tests/cards/test_functions.py
git commit -m "Cards: value types and time-shape/change functions (Polars)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Smoothing, normalization, set aggregation, relationships and arithmetic

**Files:**
- Modify: `src/tempus/cards/functions.py` (append)
- Test: `tests/cards/test_functions_more.py`

**Interfaces:**
- Consumes: Task 3's `values.py` and `functions.py` machinery
- Produces:
  - Functions `rolling, ewm, rebase, zscore, mean, sum, min, max, corr, lagcorr`
  - `binop(op: str, left: Value, right: Value, what: str) -> Value`
  - `lagcorr` returns `Table(name, df[lag:int, r:float|None, n:int], meta={"kind":"lagcorr","x":label,"y":label,"freq":freq,"warnings":[str]})`

- [ ] **Step 1: Write the failing tests**

```python
# tests/cards/test_functions_more.py
from datetime import date

import pytest

from tempus.cards.functions import CATALOG, binop, call
from tempus.cards.values import EvalError, Series, SeriesSet, Table, frame


def monthly(name, vals, start=(2020, 1)):
    y, m = start
    pts = []
    for i, v in enumerate(vals):
        k = (m - 1) + i
        pts.append((date(y + k // 12, k % 12 + 1, 1), v))
    return Series(name, name, "M", frame(pts))


def values(s):
    return s.df["value"].to_list()


def fn(name, *args, **kwargs):
    return call(CATALOG[name], list(args), kwargs)


def test_rolling_ewm_zscore_rebase():
    m = monthly("m", [1.0, 2.0, 3.0, 4.0])
    assert values(fn("rolling", m, 2)) == [None, 1.5, 2.5, 3.5]
    assert values(fn("rolling", m, 2, agg="max")) == [None, 2.0, 3.0, 4.0]
    assert values(fn("ewm", m, 2))[0] == 1.0
    z = values(fn("zscore", m))
    assert z[0] == pytest.approx(-1.161895, rel=1e-5) and sum(z) == pytest.approx(0.0)
    assert values(fn("rebase", m, "2020-02-01")) == [50.0, 100.0, 150.0, 200.0]
    with pytest.raises(EvalError, match="no value at or after"):
        fn("rebase", m, "2030-01-01")


def test_set_aggregation_ignores_missing_members_per_date():
    a = monthly("a", [1.0, 3.0])
    b = monthly("b", [3.0, None])
    s = SeriesSet("s", (a, b))
    assert values(fn("mean", s)) == [2.0, 3.0]
    assert values(fn("sum", s)) == [4.0, 3.0]
    with pytest.raises(EvalError, match="different frequencies"):
        fn("mean", SeriesSet("s", (a, Series("d", "d", "D", a.df))))


def test_lagcorr_finds_a_known_lead():
    base = [float(v) for v in [5, 1, 4, 2, 8, 3, 9, 1, 7, 2, 6, 3, 8, 2, 9, 4, 7, 1, 5, 3]]
    x = monthly("x", base)
    y = monthly("y", [0.0, 0.0] + base[:-2])  # y follows x two months later
    table = fn("lagcorr", x, y, nlags=4)
    assert isinstance(table, Table) and table.meta["kind"] == "lagcorr"
    rows = table.df.to_dicts()
    best = max(rows, key=lambda r: r["r"])
    assert best["lag"] == 2 and best["r"] == pytest.approx(1.0)
    assert [r["lag"] for r in rows] == [0, 1, 2, 3, 4]


def test_lagcorr_warns_on_trending_levels():
    trend = monthly("t", [float(i) for i in range(30)])
    table = fn("lagcorr", trend, trend, nlags=1)
    assert any("trending" in w for w in table.meta["warnings"])


def test_lagcorr_with_too_little_data_gives_none_not_nan():
    table = fn("lagcorr", monthly("a", [1.0]), monthly("b", [1.0]), nlags=0)
    assert table.df.to_dicts() == [{"lag": 0, "r": None, "n": 1}]


def test_corr_rolling():
    a = monthly("a", [1.0, 2.0, 3.0, 4.0])
    b = monthly("b", [2.0, 4.0, 6.0, 8.0])
    out = fn("corr", a, b, 3)
    assert values(out)[2] == pytest.approx(1.0)


def test_binop_series_numbers_and_sets():
    a = monthly("a", [2.0, 4.0])
    b = monthly("b", [1.0, 0.0])
    assert values(binop("/", a, b, "a / b")) == [2.0, None]  # divide by zero → gap
    assert values(binop("*", 2, a, "2 * a")) == [4.0, 8.0]
    s = binop("-", SeriesSet("s", (a, b)), 1, "s - 1")
    assert [values(m) for m in s.members] == [[1.0, 3.0], [0.0, -1.0]]
    with pytest.raises(EvalError, match="frequencies differ"):
        binop("+", a, Series("d", "d", "D", a.df), "a + d")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/cards/test_functions_more.py -v`
Expected: FAIL with `KeyError: 'rolling'` or `ImportError: cannot import name 'binop'`

- [ ] **Step 3: Implement (append to `functions.py`)**

First, change the values import at the top of `functions.py` to:

```python
from tempus.cards.values import (
    FREQ_ORDER, FREQS, EvalError, Series, SeriesSet, Table, Value, align, resample_hint,
)
```

Then append:

```python
# --- smoothing and normalization --------------------------------------------------------


def _rolling(x: Series, n: int, agg: str) -> Series:
    v = pl.col("value")
    expr = {"mean": v.rolling_mean(window_size=n), "sum": v.rolling_sum(window_size=n),
            "min": v.rolling_min(window_size=n), "max": v.rolling_max(window_size=n),
            "std": v.rolling_std(window_size=n)}[agg]
    return _with(x, expr, f"{x.label}, {n}-period rolling {agg}")


def _ewm(x: Series, span: int) -> Series:
    return _with(x, pl.col("value").ewm_mean(span=span), f"{x.label}, EWM span {span}")


def _rebase(x: Series, at: date, to: float) -> Series:
    base = x.df.filter((pl.col("ts") >= at) & pl.col("value").is_not_null()).head(1)
    if base.is_empty():
        raise EvalError(f"rebase: '{x.name}' has no value at or after {at}")
    b = base["value"][0]
    if b == 0:
        raise EvalError(f"rebase: '{x.name}' is 0 at {base['ts'][0]}")
    return _with(x, pl.col("value") / b * to, f"{x.label}, {base['ts'][0]} = {to:g}")


def _zscore(x: Series, window: int | None) -> Series:
    v = pl.col("value")
    if window:
        expr = (v - v.rolling_mean(window_size=window)) / v.rolling_std(window_size=window)
    else:
        expr = (v - v.mean()) / v.std()
    return _with(x, expr, f"{x.label}, z-score")


# --- across a set -----------------------------------------------------------------------


def _set_agg(how: str):
    def impl(s: SeriesSet) -> Series:
        if not s.members:
            raise EvalError(f"{how}: '{s.name}' has no members")
        freqs = sorted({m.freq for m in s.members})
        if len(freqs) > 1:
            raise EvalError(f"{how}: members of '{s.name}' have different frequencies "
                            f"({', '.join(freqs)})", hint="Resample the members first.")
        v = pl.col("value")
        expr = {"mean": v.mean(), "sum": pl.when(v.count() > 0).then(v.sum()),
                "min": v.min(), "max": v.max()}[how]
        df = pl.concat([m.df for m in s.members]).group_by("ts").agg(expr.alias("value"))
        first = s.members[0]
        return Series(s.name, f"{how} of {s.name}", first.freq, df.sort("ts"), first.units)
    return impl


# --- relationships ----------------------------------------------------------------------

_LAG_UNIT = {"D": (1, "d"), "W": (1, "w"), "M": (1, "mo"), "Q": (3, "mo"), "Y": (1, "y")}


def _lag1_autocorr(s: Series) -> float | None:
    df = s.df.drop_nulls()
    if df.height < 3:
        return None
    r = df.select(pl.corr(pl.col("value"), pl.col("value").shift(1))).item()
    return None if r is None or r != r else r


def _corr(x: Series, y: Series, window: int) -> Series:
    df = align(x, y, f"corr({x.name}, {y.name})")
    out = df.select("ts", pl.rolling_corr(pl.col("a"), pl.col("b"), window_size=window)
                    .alias("value"))
    return Series(x.name, f"{window}-period correlation: {x.label} vs {y.label}", x.freq, out)


def _lagcorr(x: Series, y: Series, nlags: int) -> Table:
    if x.freq != y.freq:
        raise EvalError(f"lagcorr({x.name}, {y.name}) — frequencies differ "
                        f"({x.freq} vs {y.freq}).", hint=resample_hint(x, y))
    if x.freq == "irregular":
        raise EvalError("lagcorr needs a regular frequency", hint='Resample first, e.g. "M"')
    if nlags < 0:
        raise EvalError("lagcorr: nlags must be 0 or more")
    mult, unit = _LAG_UNIT[x.freq]
    rows = []
    for k in range(nlags + 1):
        shifted = x.df if k == 0 else x.df.with_columns(
            pl.col("ts").dt.offset_by(f"{k * mult}{unit}"))
        joined = shifted.join(y.df, on="ts", how="inner", suffix="_y").drop_nulls()
        r = None
        if joined.height >= 3:
            r = joined.select(pl.corr("value", "value_y")).item()
            r = None if r is None or r != r else r
        rows.append({"lag": k, "r": r, "n": joined.height})
    warnings = []
    for s in (x, y):
        ac = _lag1_autocorr(s)
        if ac is not None and ac > 0.95:
            warnings.append(f"'{s.name}' looks trending in levels; correlations between trending "
                            "series are often spurious. Try yoy() or diff() first.")
    df = pl.DataFrame(rows, schema={"lag": pl.Int64, "r": pl.Float64, "n": pl.Int64})
    meta = {"kind": "lagcorr", "x": x.label, "y": y.label, "freq": x.freq,
            "warnings": warnings}
    return Table(x.name, df, meta)


# --- arithmetic -------------------------------------------------------------------------

_ARITH = {"+": lambda a, b: a + b, "-": lambda a, b: a - b,
          "*": lambda a, b: a * b, "/": lambda a, b: _safe_ratio(a, b)}


def binop(op: str, left: Value, right: Value, what: str) -> Value:
    if isinstance(left, Table) or isinstance(right, Table) or isinstance(left, str) \
            or isinstance(right, str):
        raise EvalError(f"{what} — tables and strings can't be used in arithmetic")
    if isinstance(left, SeriesSet):
        return SeriesSet(left.name, tuple(binop(op, m, right, what) for m in left.members))
    if isinstance(right, SeriesSet):
        return SeriesSet(right.name, tuple(binop(op, left, m, what) for m in right.members))
    f = _ARITH[op]
    if isinstance(left, Series) and isinstance(right, Series):
        df = align(left, right, what)
        out = df.select("ts", f(pl.col("a"), pl.col("b")).alias("value"))
        return Series(left.name, what, left.freq, out)
    if isinstance(left, Series):
        return _with(left, f(pl.col("value"), pl.lit(right)), what)
    if isinstance(right, Series):
        return _with(right, f(pl.lit(left), pl.col("value")), what)
    return f(left, right) if op != "/" else (None if right == 0 else left / right)


register(Function("rolling", (X, Param("n", "int"),
                              Param("agg", "str", "mean", ("mean", "sum", "min", "max", "std"))),
                  "same", "Rolling statistic over the last n periods.",
                  "The first n-1 points are gaps. Smoothing delays turning points.", _rolling))
register(Function("ewm", (X, Param("span", "int")), "same",
                  "Exponentially weighted moving average.",
                  "Reacts faster than rolling(), but still lags turning points.", _ewm))
register(Function("rebase", (X, Param("at", "date"), Param("to", "number", 100)), "same",
                  "Index the series so its value at `at` equals `to` (default 100).",
                  "Uses the first observation at or after `at`. Rebasing makes series with "
                  "different units comparable without dual axes.", _rebase))
register(Function("zscore", (X, Param("window", "int", None)), "same",
                  "Standardize: (x − mean) / std, over the full history or a rolling window.",
                  "Full-history z-scores use future data at every point: fine for "
                  "comparison, wrong for signals.", _zscore))
for _how in ("mean", "sum", "min", "max"):
    register(Function(_how, (Param("s", "set"),), "series", f"{_how} across a set's members.",
                      "Dates where some members are missing use the members present, which "
                      "can make sums jump.", _set_agg(_how), broadcast=False))
register(Function("corr", (X, Param("y", "series"), Param("window", "int")), "series",
                  "Rolling Pearson correlation between two series.",
                  "Trending series correlate spuriously; correlate yoy() or diff() instead.",
                  _corr, broadcast=False))
register(Function("lagcorr", (X, Param("y", "series"), Param("nlags", "int", 12)), "lagcorr",
                  "Correlation of x and y at lags 0..nlags. Positive lag k means x leads y by "
                  "k periods.",
                  "Uses only points inside the card window. Trending levels correlate "
                  "spuriously; use yoy() or diff() first.", _lagcorr,
                  broadcast=False, windowed=True))
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/cards/test_functions_more.py tests/cards/test_functions.py -v`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
mise run lint
git add src/tempus/cards/functions.py tests/cards/test_functions_more.py
git commit -m "Cards: smoothing, normalization, set, relationship functions and arithmetic

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Panel catalog and static checks

**Files:**
- Create: `src/tempus/cards/panels.py`, `src/tempus/cards/check.py`
- Test: `tests/cards/test_check.py`, `tests/cards/bad/*.yaml`

**Interfaces:**
- Consumes: `Card` (Task 1), `parse/names/ExprError` (Task 2), `CATALOG/match_args/ArgError` (Task 3)
- Produces:
  - `PanelType(name, accepts: tuple[str,...], field: str, multiple: bool, doc: str, when_not: str)`, `PANELS: dict[str, PanelType]`
  - `panel_refs(panel: Panel, ptype: PanelType) -> list[str]`
  - `formula_order(card: Card) -> list[tuple[str, Node]]`: parseable formulas in dependency order (raises nothing; skips unparseable ones and cycles)
  - `check_card(card_id: str, card: Card) -> tuple[dict[str, str], list[CardError]]`: static types (`"series"`, `"set"`, `"lagcorr"`, `"int"`, `"num"`, `"str"`) and errors

- [ ] **Step 1: Write the failing tests and bad cards**

Create these files under `tests/cards/bad/`. Each starts with a comment giving the expected error path and a message fragment:

```yaml
# tests/cards/bad/unknown-name.yaml
# expect: formulas.gas_yoy | unknown name 'gasolin'
version: 1
title: t
question: q
inputs: {gasoline: "fred:CUSR0000SETB01"}
formulas: {gas_yoy: yoy(gasolin)}
panels: [{type: timeseries, y: [gas_yoy]}]
```

```yaml
# tests/cards/bad/unknown-function.yaml
# expect: formulas.g | unknown function 'yoyy'
version: 1
title: t
question: q
inputs: {gasoline: "fred:CUSR0000SETB01"}
formulas: {g: yoyy(gasoline)}
panels: [{type: timeseries, y: [g]}]
```

```yaml
# tests/cards/bad/cycle.yaml
# expect: formulas | cycle
version: 1
title: t
question: q
inputs: {x: "fred:X"}
formulas: {a: "b + x", b: "a + x"}
panels: [{type: timeseries, y: [a]}]
```

```yaml
# tests/cards/bad/panel-type-mismatch.yaml
# expect: panels[0] | needs a lagcorr table
version: 1
title: t
question: q
inputs: {x: "fred:X"}
formulas: {x_yoy: yoy(x)}
panels: [{type: lagcorr, data: x_yoy}]
```

```yaml
# tests/cards/bad/bad-choice.yaml
# expect: formulas.m | must be one of
version: 1
title: t
question: q
inputs: {x: "fred:X"}
formulas: {m: 'resample(x, "monthly")'}
panels: [{type: timeseries, y: [m]}]
```

```yaml
# tests/cards/bad/unknown-panel.yaml
# expect: panels[0] | unknown panel type 'timeseris'
version: 1
title: t
question: q
inputs: {x: "fred:X"}
panels: [{type: timeseris, y: [x]}]
```

```yaml
# tests/cards/bad/attribute.yaml
# expect: formulas.a | attribute access
version: 1
title: t
question: q
inputs: {x: "fred:X"}
formulas: {a: x.values}
panels: [{type: timeseries, y: [a]}]
```

```python
# tests/cards/test_check.py
from pathlib import Path

import pytest

from tempus.cards.check import check_card, formula_order
from tempus.cards.spec import load_card, parse_card

BAD = sorted((Path(__file__).parent / "bad").glob("*.yaml"))


def _expect(path: Path) -> tuple[str, str]:
    line = next(l for l in path.read_text().splitlines() if l.startswith("# expect:"))
    where, fragment = line.removeprefix("# expect:").split("|")
    return where.strip(), fragment.strip()


@pytest.mark.parametrize("path", BAD, ids=[p.stem for p in BAD])
def test_bad_cards_report_the_expected_error(path):
    card, errors = load_card(path)
    assert card is not None, errors
    _, errors = check_card(path.stem, card)
    where, fragment = _expect(path)
    assert any(e.path == where and fragment in e.message for e in errors), errors


def test_did_you_mean_hint_on_unknown_name():
    card, _ = load_card(Path(__file__).parent / "bad" / "unknown-name.yaml")
    _, errors = check_card("unknown-name", card)
    assert errors[0].hint == "Did you mean 'gasoline'?"


GOOD = {
    "version": 1, "title": "t", "question": "q",
    "inputs": {"crude": "fred:DCOILWTICO", "gas": "fred:CUSR0000SETB01",
               "energy": {"concept": "prices.consumer.energy"}},
    "formulas": {
        "passthru": "lagcorr(crude_yoy, gas_yoy, nlags=12)",
        "crude_yoy": 'yoy(resample(crude, "M"))',
        "gas_yoy": "yoy(gas)",
        "energy_yoy": "yoy(energy)",
        "avg": "mean(energy_yoy) - 1",
    },
    "panels": [
        {"type": "timeseries", "y": ["crude_yoy", "gas_yoy", "energy_yoy"]},
        {"type": "lagcorr", "data": "passthru"},
        {"type": "stat", "y": "avg", "change": "yoy"},
    ],
}


def test_good_card_types_and_order():
    card, errors = parse_card("good", GOOD)
    assert errors == []
    types, errors = check_card("good", card)
    assert errors == []
    assert types["energy"] == "set" and types["energy_yoy"] == "set"
    assert types["avg"] == "series" and types["passthru"] == "lagcorr"
    order = [name for name, _ in formula_order(card)]
    assert order.index("crude_yoy") < order.index("passthru")


def test_stat_panel_rejects_a_set_and_multiple_refs():
    raw = GOOD | {"panels": [{"type": "stat", "y": ["crude_yoy", "gas_yoy"]},
                             {"type": "stat", "y": "energy"}]}
    card, _ = parse_card("c", raw)
    _, errors = check_card("c", card)
    assert any(e.path == "panels[0]" and "only one" in e.message for e in errors)
    assert any(e.path == "panels[1]" and "'energy' is a set" in e.message for e in errors)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/cards/test_check.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tempus.cards.check'`

- [ ] **Step 3: Implement `panels.py`**

```python
# src/tempus/cards/panels.py
"""The panel catalog: the closed list of ways a card can show a result.

Adding a type means writing its entry: what it's for and when not to use it. Channel names
follow Vega-Lite; the closed registry follows dbt Charts (see docs/STACK.md → Inspirations).
"""

from dataclasses import dataclass

from tempus.cards.spec import Panel


@dataclass(frozen=True)
class PanelType:
    name: str
    accepts: tuple[str, ...]  # static types: series | set | lagcorr
    field: str  # the panel field holding the reference(s): "y" or "data"
    multiple: bool
    doc: str
    when_not: str


PANELS = {
    p.name: p
    for p in [
        PanelType("timeseries", ("series", "set"), "y", True,
                  "Trends over time on one shared axis, with zoom and a crosshair.",
                  "Series in very different units: rebase() or zscore() them first. Dual "
                  "axes are left out on purpose (Cleveland & McGill)."),
        PanelType("lagcorr", ("lagcorr",), "data", False,
                  "Bars of correlation by lag from lagcorr(); the peak is highlighted and the "
                  "header says which series leads.",
                  "Trending series in levels: correlate yoy() or diff() instead."),
        PanelType("stat", ("series",), "y", False,
                  "The latest value, its change and a sparkline.",
                  "When the path matters more than the latest point: use timeseries."),
        PanelType("table", ("lagcorr", "series"), "data", False, "Raw values.",
                  "As a card's only panel: tables show numbers but argue poorly."),
    ]
}

_ACCEPT_TEXT = {"series": "a series", "set": "a set of series", "lagcorr": "a lagcorr table"}


def accepts_text(ptype: PanelType) -> str:
    return " or ".join(_ACCEPT_TEXT[a] for a in ptype.accepts)


def panel_refs(panel: Panel, ptype: PanelType) -> list[str]:
    value = getattr(panel, ptype.field)
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)
```

- [ ] **Step 4: Implement `check.py`**

```python
# src/tempus/cards/check.py
"""Static checks: everything that can be known without the database."""

from datetime import date
from graphlib import CycleError, TopologicalSorter

from tempus.cards.errors import CardError, did_you_mean
from tempus.cards.expr import BinOp, Call, ExprError, Name, Neg, Node, Num, Str, names, parse
from tempus.cards.functions import CATALOG, ArgError, match_args
from tempus.cards.panels import PANELS, accepts_text, panel_refs
from tempus.cards.spec import Card, ConceptInput

_KIND_ACCEPTS = {"series": {"series"}, "set": {"set"}, "int": {"int"},
                 "number": {"int", "num"}, "str": {"str"}, "date": {"str"}}


def _parsed(card: Card) -> tuple[dict[str, Node], dict[str, str]]:
    nodes, failures = {}, {}
    for name, text in card.formulas.items():
        try:
            nodes[name] = parse(text)
        except ExprError as exc:
            failures[name] = str(exc)
    return nodes, failures


def formula_order(card: Card) -> list[tuple[str, Node]]:
    nodes, _ = _parsed(card)
    graph = {n: names(node) & set(nodes) for n, node in nodes.items()}
    try:
        order = list(TopologicalSorter(graph).static_order())
    except CycleError:
        return []
    return [(n, nodes[n]) for n in order]


def check_card(card_id: str, card: Card) -> tuple[dict[str, str], list[CardError]]:
    errors: list[CardError] = []
    types: dict[str, str] = {
        n: "set" if isinstance(i, ConceptInput) else "series" for n, i in card.inputs.items()
    }
    known = set(card.inputs) | set(card.formulas)
    nodes, failures = _parsed(card)
    for name, message in failures.items():
        errors.append(CardError(card_id, f"formulas.{name}",
                                f"{card.formulas[name]} — {message}"))

    broken = set(failures)
    for name, node in nodes.items():
        for ref in sorted(names(node) - known):
            errors.append(CardError(card_id, f"formulas.{name}",
                                    f"{card.formulas[name]} — unknown name '{ref}'.",
                                    hint=did_you_mean(ref, known)))
            broken.add(name)

    graph = {n: names(node) & set(card.formulas) for n, node in nodes.items()}
    try:
        order = list(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        cycle = " → ".join(exc.args[1])
        errors.append(CardError(card_id, "formulas", f"formulas form a cycle: {cycle}"))
        return types, errors

    for name in order:
        if name not in nodes or name in broken or names(nodes[name]) & broken:
            broken.add(name)
            continue
        path = f"formulas.{name}"
        found = _infer(nodes[name], types, card_id, path, card.formulas[name], errors)
        if found is None:
            broken.add(name)
        else:
            types[name] = found

    for i, panel in enumerate(card.panels):
        errors += _check_panel(card_id, i, panel, types, known, broken)
    return types, errors


def _check_panel(card_id, i, panel, types, known, broken) -> list[CardError]:
    path = f"panels[{i}]"
    ptype = PANELS.get(panel.type)
    if ptype is None:
        return [CardError(card_id, path, f"unknown panel type '{panel.type}'.",
                          hint=did_you_mean(panel.type, PANELS))]
    refs = panel_refs(panel, ptype)
    if not refs:
        return [CardError(card_id, path, f"'{panel.type}' needs '{ptype.field}'")]
    if len(refs) > 1 and not ptype.multiple:
        return [CardError(card_id, path, f"'{panel.type}' shows only one result; "
                                         f"'{ptype.field}' lists {len(refs)}")]
    errors = []
    for ref in refs:
        if ref not in known:
            errors.append(CardError(card_id, path, f"unknown name '{ref}'.",
                                    hint=did_you_mean(ref, known)))
        elif ref in broken:
            continue
        elif types.get(ref) not in ptype.accepts:
            errors.append(CardError(card_id, path, f"type '{panel.type}' needs "
                                                   f"{accepts_text(ptype)}; '{ref}' is a "
                                                   f"{types.get(ref)}"))
    return errors


def _infer(node, types, card_id, path, text, errors) -> str | None:
    def fail(message, hint=None):
        errors.append(CardError(card_id, path, f"{text} — {message}", hint=hint))
        return None

    match node:
        case Name(id=name):
            return types.get(name)
        case Num(value=value):
            return "int" if isinstance(value, int) else "num"
        case Str():
            return "str"
        case Neg(operand=operand):
            t = _infer(operand, types, card_id, path, text, errors)
            if t in ("str", "lagcorr"):
                return fail(f"can't negate a {t}")
            return t
        case BinOp(left=left, right=right):
            lt = _infer(left, types, card_id, path, text, errors)
            rt = _infer(right, types, card_id, path, text, errors)
            if lt is None or rt is None:
                return None
            if {"str", "lagcorr"} & {lt, rt}:
                return fail("tables and strings can't be used in arithmetic")
            if "set" in (lt, rt):
                return "set"
            return "series" if "series" in (lt, rt) else "num"
        case Call(func=func, args=args, kwargs=kwargs):
            fn = CATALOG.get(func)
            if fn is None:
                return fail(f"unknown function '{func}'.", hint=did_you_mean(func, CATALOG))
            try:
                bound = match_args(fn, list(args), dict(kwargs))
            except ArgError as exc:
                return fail(str(exc))
            arg_types = {}
            for i, p in enumerate(fn.params):
                arg = bound[p.name]
                if arg is p.default and not isinstance(arg, Num | Str | Name | Call | BinOp | Neg):
                    continue  # default value, not written in the formula
                t = _infer(arg, types, card_id, path, text, errors)
                if t is None:
                    return None
                allowed = set(_KIND_ACCEPTS[p.kind])
                if i == 0 and p.kind == "series" and fn.broadcast:
                    allowed.add("set")
                if t not in allowed:
                    return fail(f"{fn.name}: '{p.name}' must be a {p.kind}, got a {t}")
                if isinstance(arg, Str) and p.choices and arg.value not in p.choices:
                    return fail(f"{fn.name}: '{p.name}' must be one of {', '.join(p.choices)}")
                if isinstance(arg, Str) and p.kind == "date":
                    try:
                        date.fromisoformat(arg.value)
                    except ValueError:
                        return fail(f"{fn.name}: '{p.name}' must be a date like 2000-01-01")
                arg_types[p.name] = t
            if fn.returns == "same":
                return arg_types[fn.params[0].name]
            return fn.returns
    return None
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/cards/test_check.py -v`
Expected: PASS

- [ ] **Step 6: Lint and commit**

```bash
mise run lint
git add src/tempus/cards/panels.py src/tempus/cards/check.py tests/cards/test_check.py tests/cards/bad
git commit -m "Cards: panel catalog and static checks with did-you-mean errors

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Shared DB fixture, `cards` table and the file repository

**Files:**
- Create: `tests/conftest.py`
- Modify: `tests/test_sync.py` (delete its local `conn` fixture and the imports only it used: `psycopg`, `make_conninfo`, `get_settings`, `TEST_DB`)
- Modify: `src/tempus/schema.sql` (append the `cards` table)
- Modify: `src/tempus/config.py` (add `cards_path`)
- Create: `src/tempus/cards/repository.py`
- Test: `tests/cards/test_repository.py`

**Interfaces:**
- Consumes: `load_card`, `parse_card` (Task 1), `check_card` (Task 5)
- Produces:
  - `Settings.cards_path: Path = Path("cards")`
  - `CardRepository` Protocol: `ids() -> list[str]`, `get(card_id) -> tuple[Card|None, list[CardError]]`, `text(card_id) -> str`, `save(card_id, text) -> list[CardError]`
  - `FileCardRepository(root: Path)`, implementing the Protocol, plus `.root` and `.path(card_id) -> Path`
  - `sync_index(conn, repo) -> tuple[int, list[CardError]]`: loads valid cards into `cards` and removes rows for deleted files
  - The pytest fixture `conn`, available to every test (a throwaway `tempus_test` DB, skipped when unreachable)

- [ ] **Step 1: Move the DB fixture to `tests/conftest.py`**

```python
# tests/conftest.py
"""Shared fixtures. `conn` is a throwaway `tempus_test` database; tests using it skip when the
database isn't reachable (start it with `mise run db:up`)."""

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from tempus import db
from tempus.config import get_settings

TEST_DB = "tempus_test"


@pytest.fixture
def conn():
    url = get_settings().database_url
    try:
        admin = psycopg.connect(url, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("database not reachable")
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    with db.connect(make_conninfo(url, dbname=TEST_DB)) as c:
        db.init_schema(c)
        yield c
```

Delete the identical fixture and its now-unused imports from `tests/test_sync.py`. Then run
`uv run pytest tests/test_sync.py -v`. Expected: PASS (unchanged behavior).

- [ ] **Step 2: Write the failing tests**

```python
# tests/cards/test_repository.py
from pathlib import Path

from tempus.cards.repository import FileCardRepository, sync_index

CARD = """\
version: 1
title: Crude
question: Does crude lead gasoline?
concepts: [prices.consumer.energy]
tags: [energy]
inputs: {crude: "fred:DCOILWTICO"}
panels: [{type: timeseries, y: [crude]}]
"""


def test_ids_get_and_missing(tmp_path: Path):
    (tmp_path / "crude.yaml").write_text(CARD)
    (tmp_path / "notes.txt").write_text("ignored")
    repo = FileCardRepository(tmp_path)
    assert repo.ids() == ["crude"]
    card, errors = repo.get("crude")
    assert errors == [] and card.title == "Crude"
    card, errors = repo.get("crud")
    assert card is None and errors[0].hint == "Did you mean 'crude'?"


def test_missing_root_has_no_cards(tmp_path: Path):
    assert FileCardRepository(tmp_path / "nope").ids() == []


def test_save_refuses_invalid_cards_and_writes_valid_ones(tmp_path: Path):
    repo = FileCardRepository(tmp_path)
    errors = repo.save("broken", "version: 1\ntitle: x\n")
    assert errors and not (tmp_path / "broken.yaml").exists()
    assert repo.save("crude", CARD) == []
    assert repo.text("crude") == CARD


def test_sync_index_loads_valid_and_drops_deleted(conn, tmp_path: Path):
    (tmp_path / "crude.yaml").write_text(CARD)
    (tmp_path / "bad.yaml").write_text("version: 1\n")
    repo = FileCardRepository(tmp_path)
    loaded, errors = sync_index(conn, repo)
    assert loaded == 1 and any(e.card == "bad" for e in errors)
    row = conn.execute("SELECT title, concepts, tags, spec FROM cards WHERE id = 'crude'").fetchone()
    assert row["title"] == "Crude" and row["concepts"] == ["prices.consumer.energy"]
    assert row["spec"]["inputs"]["crude"]["series"] == "fred:DCOILWTICO"
    (tmp_path / "crude.yaml").unlink()
    sync_index(conn, repo)
    assert conn.execute("SELECT count(*) AS n FROM cards").fetchone()["n"] == 0
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/cards/test_repository.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tempus.cards.repository'`

- [ ] **Step 4: Implement the schema, config and repository**

Append to `src/tempus/schema.sql`:

```sql
-- Index of cards/*.yaml (the files are the source of truth), rebuilt by `tempus card load`.
CREATE TABLE IF NOT EXISTS cards (
    id         text        PRIMARY KEY,                  -- the file name, kebab-case
    title      text        NOT NULL,
    question   text        NOT NULL,
    tags       text[]      NOT NULL DEFAULT '{}',
    concepts   text[]      NOT NULL DEFAULT '{}',
    spec       jsonb       NOT NULL,
    file_hash  text        NOT NULL,
    loaded_at  timestamptz NOT NULL DEFAULT now()
);
```

In `src/tempus/config.py`, add to `Settings`: `cards_path: Path = Path("cards")`

```python
# src/tempus/cards/repository.py
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
            return None, [CardError(card_id, "file", "no such card",
                                    hint=did_you_mean(card_id, self.ids()))]
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


def sync_index(conn: psycopg.Connection, repo: CardRepository) -> tuple[int, list[CardError]]:
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
                INSERT INTO cards (id, title, question, tags, concepts, spec, file_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title, question = EXCLUDED.question,
                    tags = EXCLUDED.tags, concepts = EXCLUDED.concepts, spec = EXCLUDED.spec,
                    file_hash = EXCLUDED.file_hash, loaded_at = now()
                """,
                (card_id, card.title, card.question, card.tags, card.concepts,
                 Jsonb(card.model_dump(mode="json")), digest),
            )
            loaded += 1
        conn.execute("DELETE FROM cards WHERE NOT (id = ANY(%s))", (ids,))
    return loaded, problems
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/cards/test_repository.py tests/test_sync.py -v`
Expected: PASS (the DB test skips if the DB is down; run `mise run db:up` first to exercise it)

- [ ] **Step 6: Lint and commit**

```bash
mise run lint
git add tests/conftest.py tests/test_sync.py src/tempus/schema.sql src/tempus/config.py src/tempus/cards/repository.py tests/cards/test_repository.py
git commit -m "Cards: cards index table and file repository; share the DB fixture

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Resolve references against the database

**Files:**
- Create: `src/tempus/cards/resolve.py`
- Test: `tests/cards/test_resolve.py`, `tests/cards/seed.py`

**Interfaces:**
- Consumes: `Card`, `SeriesInput`, `ConceptInput`, `CardError`, `did_you_mean`, `freq_from_fred`
- Produces:
  - `SeriesInfo(source: str, series_id: str, title: str | None, units: str | None, freq: str)` (frozen dataclass)
  - `resolve(conn, card_id: str, card: Card) -> tuple[dict[str, list[SeriesInfo]], list[CardError]]`. Concepts include descendants (`id = c OR id LIKE c || '.%'`)
  - Test helper `seed(conn)` in `tests/cards/seed.py`, reused by Tasks 8–10

- [ ] **Step 1: Write the seed helper and failing tests**

```python
# tests/cards/seed.py
"""Small synthetic dataset for card integration tests."""

from datetime import UTC, datetime

from tempus.sources.base import Observation, SeriesMeta
from tempus.sync import sync_series


class _Source:
    name = "fred"

    def __init__(self, metas, points):
        self.metas, self.points = metas, points

    def fetch_meta(self, series_id):
        return self.metas[series_id]

    def fetch_observations(self, series_id):
        return self.points[series_id]


def _monthly(values, start_year=2018):
    return [Observation(datetime(start_year + i // 12, i % 12 + 1, 1, tzinfo=UTC), v)
            for i, v in enumerate(values)]


def seed(conn):
    base = [100 + (i % 7) * 3 + i * 0.5 for i in range(72)]
    revised = datetime(2024, 1, 1, tzinfo=UTC)
    metas = {
        "XLEAD": SeriesMeta("XLEAD", title="Leader", units="Index", frequency="Monthly",
                            upstream_updated_at=revised),
        "YFOLLOW": SeriesMeta("YFOLLOW", title="Follower", units="Index", frequency="Monthly",
                              upstream_updated_at=revised),
        "WEEKLY": SeriesMeta("WEEKLY", title="Weekly thing", frequency="Weekly, Ending Monday",
                             upstream_updated_at=revised),
    }
    points = {
        "XLEAD": _monthly(base),
        "YFOLLOW": _monthly([100.0] + base[:-1]),  # follows XLEAD one month later
        "WEEKLY": [Observation(datetime(2020, 1, 6, tzinfo=UTC), 1.0),
                   Observation(datetime(2021, 1, 4, tzinfo=UTC), 2.0)],
    }
    src = _Source(metas, points)
    for sid in metas:
        sync_series(conn, src, sid)
    conn.execute("""INSERT INTO concepts (id, parent_id, name, description, position) VALUES
        ('prices', NULL, 'Prices', 'p', 0),
        ('prices.energy', 'prices', 'Energy', 'e', 0),
        ('prices.empty', 'prices', 'Empty', 'e', 1)""")
    conn.execute("""INSERT INTO series_concepts (source, series_id, concept_id, method) VALUES
        ('fred', 'XLEAD', 'prices.energy', 'manual'),
        ('fred', 'YFOLLOW', 'prices', 'manual')""")
```

```python
# tests/cards/test_resolve.py
from tempus.cards.resolve import SeriesInfo, resolve
from tempus.cards.spec import parse_card

from .seed import seed


def card(inputs, concepts=()):
    raw = {"version": 1, "title": "t", "question": "q", "inputs": inputs,
           "concepts": list(concepts), "panels": [{"type": "timeseries", "y": [next(iter(inputs))]}]}
    c, errors = parse_card("c", raw)
    assert errors == []
    return c


def test_series_and_concepts_resolve_with_frequency(conn):
    seed(conn)
    resolved, errors = resolve(conn, "c", card(
        {"x": "fred:XLEAD", "w": "fred:WEEKLY", "p": {"concept": "prices"}},
        concepts=["prices.energy"]))
    assert errors == []
    assert resolved["x"] == [SeriesInfo("fred", "XLEAD", "Leader", "Index", "M")]
    assert resolved["w"][0].freq == "W"
    assert {i.series_id for i in resolved["p"]} == {"XLEAD", "YFOLLOW"}  # includes descendants


def test_missing_series_hints(conn):
    seed(conn)
    _, errors = resolve(conn, "c", card({"x": "fred:XLEAF", "z": "fred:NOPE123"}))
    by_path = {e.path: e for e in errors}
    assert by_path["inputs.x"].hint == "Did you mean 'fred:XLEAD'?"
    assert "tempus catalog add fred NOPE123 --sync" in by_path["inputs.z"].hint


def test_empty_and_unknown_concepts(conn):
    seed(conn)
    _, errors = resolve(conn, "c", card({"e": {"concept": "prices.empty"},
                                         "u": {"concept": "prices.enrgy"}},
                                        concepts=["nope"]))
    messages = {e.path: e.message for e in errors}
    assert messages["inputs.e"] == "concept 'prices.empty' has no series filed under it"
    assert "unknown concept 'prices.enrgy'" in messages["inputs.u"]
    assert "unknown concept 'nope'" in messages["concepts[0]"]
```

- [ ] **Step 2: Run to verify failure**

Run: `mise run db:up && uv run pytest tests/cards/test_resolve.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tempus.cards.resolve'`

- [ ] **Step 3: Implement**

```python
# src/tempus/cards/resolve.py
"""Turn a card's references into series metadata. Frequency comes from here."""

from dataclasses import dataclass

import psycopg

from tempus.cards.errors import CardError, did_you_mean
from tempus.cards.spec import Card, ConceptInput
from tempus.cards.values import freq_from_fred


@dataclass(frozen=True)
class SeriesInfo:
    source: str
    series_id: str
    title: str | None
    units: str | None
    freq: str


def _info(row) -> SeriesInfo:
    return SeriesInfo(row["source"], row["series_id"], row["title"], row["units"],
                      freq_from_fred(row["frequency"]))


def resolve(conn: psycopg.Connection, card_id: str,
            card: Card) -> tuple[dict[str, list[SeriesInfo]], list[CardError]]:
    errors: list[CardError] = []
    resolved: dict[str, list[SeriesInfo]] = {}
    rows = conn.execute("SELECT source, series_id, title, units, frequency FROM series").fetchall()
    series = {(r["source"], r["series_id"]): r for r in rows}
    concept_ids = {r["id"] for r in conn.execute("SELECT id FROM concepts").fetchall()}

    for i, concept in enumerate(card.concepts):
        if concept not in concept_ids:
            errors.append(CardError(card_id, f"concepts[{i}]", f"unknown concept '{concept}'.",
                                    hint=did_you_mean(concept, concept_ids)))

    for name, inp in card.inputs.items():
        path = f"inputs.{name}"
        if isinstance(inp, ConceptInput):
            if inp.concept not in concept_ids:
                errors.append(CardError(card_id, path, f"unknown concept '{inp.concept}'.",
                                        hint=did_you_mean(inp.concept, concept_ids)))
                continue
            members = conn.execute(
                """
                SELECT s.source, s.series_id, s.title, s.units, s.frequency
                FROM series_concepts sc JOIN series s USING (source, series_id)
                WHERE sc.concept_id = %(c)s OR sc.concept_id LIKE %(c)s || '.%%'
                ORDER BY s.source, s.series_id
                """,
                {"c": inp.concept},
            ).fetchall()
            if not members:
                errors.append(CardError(card_id, path,
                                        f"concept '{inp.concept}' has no series filed under it"))
                continue
            resolved[name] = [_info(r) for r in members]
        else:
            row = series.get((inp.source, inp.series_id))
            if row is None:
                refs = [f"{s}:{i}" for s, i in series if s == inp.source]
                hint = did_you_mean(inp.series, refs) or (
                    f"Add it with `tempus catalog add {inp.source} {inp.series_id} --sync`.")
                errors.append(CardError(card_id, path,
                                        f"series '{inp.series}' isn't in the database.",
                                        hint=hint))
                continue
            resolved[name] = [_info(row)]
    return resolved, errors
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/cards/test_resolve.py -v`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
mise run lint
git add src/tempus/cards/resolve.py tests/cards/test_resolve.py tests/cards/seed.py
git commit -m "Cards: resolve series and concept references with hints

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Evaluate, render payloads, and orchestrate `run_card`

**Files:**
- Create: `src/tempus/cards/evaluate.py`, `src/tempus/cards/render.py`, `src/tempus/cards/run.py`
- Test: `tests/cards/test_run.py`, `tests/cards/fixtures/lead.yaml` (reused by Tasks 9 and 10)

**Interfaces:**
- Consumes: everything above. `seed(conn)` from `tests/cards/seed.py`
- Produces:
  - `fetch(conn, infos: Iterable[SeriesInfo]) -> dict[tuple[str,str], pl.DataFrame]`
  - `evaluate(card_id, card, resolved, data, window: tuple[date|None, date|None]) -> tuple[dict[str, Value], list[CardError]]`
  - `PAYLOAD_VERSION = 1`; `render(card_id, card, env, window) -> tuple[list[dict], list[CardError]]`
  - `CardResult(card_id: str, card: Card | None, payloads: list[dict], errors: list[CardError])` with `.to_dict()`
  - `run_card(conn, repo: CardRepository, card_id: str, start: date | None = None, end: date | None = None) -> CardResult`

- [ ] **Step 1: Write the fixture card and the failing tests**

```yaml
# tests/cards/fixtures/lead.yaml
version: 1
title: Leader → follower
question: Does the leader lead?
window: {start: 2019-01-01}
inputs:
  x: fred:XLEAD
  y: {series: fred:YFOLLOW, label: The follower}
formulas:
  x_yoy: yoy(x)
  y_yoy: yoy(y)
  lead: lagcorr(x_yoy, y_yoy, nlags=3)
panels:
  - {type: timeseries, title: YoY, y: [x_yoy, y_yoy]}
  - {type: lagcorr, data: lead}
  - {type: stat, y: x, change: period}
  - {type: table, data: lead}
```

```python
# tests/cards/test_run.py
from datetime import date
from pathlib import Path

import pytest

from tempus.cards.repository import FileCardRepository
from tempus.cards.run import PAYLOAD_VERSION, run_card

from .seed import seed

CARD = (Path(__file__).parent / "fixtures" / "lead.yaml").read_text()


@pytest.fixture
def repo(tmp_path: Path):
    (tmp_path / "lead.yaml").write_text(CARD)
    return FileCardRepository(tmp_path)


def test_run_card_end_to_end(conn, repo):
    seed(conn)
    result = run_card(conn, repo, "lead")
    assert [e for e in result.errors if e.level == "error"] == []
    ts, lag, stat, table = result.payloads
    assert ts["payload_version"] == PAYLOAD_VERSION and ts["type"] == "timeseries"
    assert [s["name"] for s in ts["series"]] == ["x_yoy", "y_yoy"]
    assert ts["series"][1]["label"] == "The follower, YoY %"
    assert ts["series"][0]["points"][0][0] >= "2019-01-01"  # clipped to the window
    assert lag["annotations"]["peak"]["lag"] == 1
    assert lag["annotations"]["lead"] == "Leader, YoY % leads The follower, YoY % by 1 period"
    assert stat["stat"]["date"] == "2023-12-01" and stat["stat"]["change_kind"] == "period"
    assert table["table"]["columns"] == ["lag", "r", "n"]


def test_window_override_and_empty_window_warns(conn, repo):
    seed(conn)
    result = run_card(conn, repo, "lead", start=date(2030, 1, 1))
    assert any(e.level == "warning" and "no data in the window" in e.message
               for e in result.errors)


def test_evaluation_errors_are_collected(conn, tmp_path: Path):
    seed(conn)
    (tmp_path / "mix.yaml").write_text(CARD.replace("y_yoy: yoy(y)", "y_yoy: x + w")
                                       .replace("y: {series: fred:YFOLLOW, label: The follower}",
                                                "y: fred:YFOLLOW\n  w: fred:WEEKLY"))
    result = run_card(conn, FileCardRepository(tmp_path), "mix")
    err = next(e for e in result.errors if e.path == "formulas.y_yoy")
    assert "frequencies differ (M vs W)" in err.message
    assert err.hint == 'Resample first: resample(w, "M")'


def test_unknown_card_and_static_errors_stop_early(conn, repo):
    seed(conn)
    assert run_card(conn, repo, "nope").errors[0].message == "no such card"
    result = run_card(conn, repo, "lead").to_dict()
    assert result["card"]["title"] == "Leader → follower"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/cards/test_run.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tempus.cards.run'`

- [ ] **Step 3: Implement `evaluate.py`**

```python
# src/tempus/cards/evaluate.py
"""Fetch the observations a card needs and evaluate its formulas in dependency order.

Transforms run on full history; windowed functions (lagcorr) see only the card window.
"""

from collections.abc import Iterable
from dataclasses import replace
from datetime import date

import polars as pl
import psycopg

from tempus.cards.check import formula_order
from tempus.cards.errors import CardError
from tempus.cards.expr import BinOp, Call, Name, Neg, Node, Num, Str, names
from tempus.cards.functions import CATALOG, binop, call
from tempus.cards.resolve import SeriesInfo
from tempus.cards.spec import Card, SeriesInput
from tempus.cards.values import SCHEMA, EvalError, Series, SeriesSet, Table, Value, clip


def fetch(conn: psycopg.Connection,
          infos: Iterable[SeriesInfo]) -> dict[tuple[str, str], pl.DataFrame]:
    keys = sorted({(i.source, i.series_id) for i in infos})
    if not keys:
        return {}
    rows = conn.execute(
        """
        SELECT o.source, o.series_id, o.ts::date AS ts, o.value
        FROM observations o
        JOIN unnest(%s::text[], %s::text[]) AS k(source, series_id) USING (source, series_id)
        ORDER BY o.ts
        """,
        ([k[0] for k in keys], [k[1] for k in keys]),
    ).fetchall()
    out: dict[tuple[str, str], list] = {k: [] for k in keys}
    for r in rows:
        out[(r["source"], r["series_id"])].append((r["ts"], r["value"]))
    return {k: pl.DataFrame(v, schema=SCHEMA, orient="row") for k, v in out.items()}


def evaluate(card_id: str, card: Card, resolved: dict[str, list[SeriesInfo]],
             data: dict[tuple[str, str], pl.DataFrame],
             window: tuple[date | None, date | None]) -> tuple[dict[str, Value], list[CardError]]:
    env: dict[str, Value] = {}
    errors: list[CardError] = []
    empty = pl.DataFrame(schema=SCHEMA)
    for name, infos in resolved.items():
        inp = card.inputs[name]
        members = tuple(
            Series(name if isinstance(inp, SeriesInput) else f"{i.source}:{i.series_id}",
                   (inp.label if isinstance(inp, SeriesInput) else None) or i.title
                   or f"{i.source}:{i.series_id}",
                   i.freq, data.get((i.source, i.series_id), empty), i.units)
            for i in infos
        )
        env[name] = members[0] if isinstance(inp, SeriesInput) else SeriesSet(name, members)

    failed: set[str] = set()
    for name, node in formula_order(card):
        if names(node) - set(env):
            failed.add(name)
            continue
        text = card.formulas[name]
        try:
            value = _eval(node, env, window, text)
        except EvalError as exc:
            errors.append(CardError(card_id, f"formulas.{name}", f"{text} — {exc}",
                                    hint=exc.hint))
            failed.add(name)
            continue
        if isinstance(value, Series | SeriesSet | Table):
            value = replace(value, name=name)
        if isinstance(value, Table):
            for warning in value.meta.get("warnings", []):
                errors.append(CardError(card_id, f"formulas.{name}", warning, level="warning"))
        env[name] = value
    return env, errors


def _eval(node: Node, env: dict[str, Value], window, text: str) -> Value:
    match node:
        case Name(id=name):
            return env[name]
        case Num(value=value) | Str(value=value):
            return value
        case Neg(operand=operand):
            return binop("*", -1, _eval(operand, env, window, text), text)
        case BinOp(op=op, left=left, right=right):
            return binop(op, _eval(left, env, window, text), _eval(right, env, window, text),
                         text)
        case Call(func=func, args=args, kwargs=kwargs):
            fn = CATALOG[func]
            values = [_eval(a, env, window, text) for a in args]
            kw = {k: _eval(v, env, window, text) for k, v in kwargs}
            if fn.windowed:
                values = [_clip(v, window) for v in values]
                kw = {k: _clip(v, window) for k, v in kw.items()}
            return call(fn, values, kw)
    raise EvalError("unsupported expression")


def _clip(value: Value, window) -> Value:
    if isinstance(value, Series):
        return clip(value, *window)
    if isinstance(value, SeriesSet):
        return SeriesSet(value.name, tuple(clip(m, *window) for m in value.members))
    return value
```

- [ ] **Step 4: Implement `render.py`**

```python
# src/tempus/cards/render.py
"""Panel payloads: the renderer-neutral JSON that every surface draws (the seam)."""

from datetime import date

import polars as pl

from tempus.cards.errors import CardError
from tempus.cards.panels import PANELS, panel_refs
from tempus.cards.spec import Card
from tempus.cards.values import Series, SeriesSet, Table, Value, clip

PAYLOAD_VERSION = 1


def _points(s: Series) -> list[list]:
    return [[d.isoformat(), v] for d, v in zip(s.df["ts"].to_list(), s.df["value"].to_list(),
                                               strict=True)]


def _series_json(s: Series, window) -> dict:
    c = clip(s, *window)
    return {"name": s.name, "label": s.label, "units": s.units, "freq": s.freq,
            "points": _points(c)}


def _flatten(name: str, value: Value) -> list[Series]:
    if isinstance(value, SeriesSet):
        return [Series(f"{name}.{m.name}", m.label, m.freq, m.df, m.units) for m in value.members]
    return [value] if isinstance(value, Series) else []


def render(card_id: str, card: Card, env: dict[str, Value],
           window: tuple[date | None, date | None]) -> tuple[list[dict], list[CardError]]:
    payloads, errors = [], []
    for i, panel in enumerate(card.panels):
        ptype = PANELS[panel.type]
        refs = panel_refs(panel, ptype)
        if any(r not in env for r in refs):
            continue  # its formula failed; that error is already reported
        p = {"payload_version": PAYLOAD_VERSION, "card": card_id, "panel": i,
             "type": panel.type, "title": panel.title, "encoding": {"x": "ts", "y": "value",
                                                                   "color": "series"},
             "series": [], "table": None, "stat": None, "annotations": {}, "warnings": []}
        value = env[refs[0]]
        if panel.type == "timeseries":
            p["series"] = [_series_json(s, window) for r in refs for s in _flatten(r, env[r])]
            empty = all(not s["points"] for s in p["series"])
        elif panel.type == "lagcorr":
            empty = _lagcorr(p, value)
        elif panel.type == "stat":
            empty = _stat(p, value, panel.change, window)
        else:
            empty = _table(p, value, window)
        if empty:
            msg = "no data in the window"
            p["warnings"].append(msg)
            errors.append(CardError(card_id, f"panels[{i}]", msg, level="warning"))
        payloads.append(p)
    return payloads, errors


def _lagcorr(p: dict, t: Table) -> bool:
    rows = t.df.to_dicts()
    p["encoding"] = {"x": "lag", "y": "r"}
    p["table"] = {"columns": ["lag", "r", "n"], "rows": rows}
    scored = [r for r in rows if r["r"] is not None]
    if not scored:
        return True
    peak = max(scored, key=lambda r: abs(r["r"]))
    plural = "" if peak["lag"] == 1 else "s"
    lead = f"{t.meta['x']} leads {t.meta['y']} by {peak['lag']} period{plural}"
    p["annotations"] = {"peak": {"lag": peak["lag"], "r": peak["r"]}, "lead": lead}
    p["title"] = p["title"] or f"{t.meta['x']} vs {t.meta['y']}"
    return False


def _stat(p: dict, s: Series, change: str | None, window) -> bool:
    c = clip(s, *window).df.drop_nulls()
    p["series"] = [{"name": s.name, "label": s.label, "units": s.units, "freq": s.freq,
                    "points": _points(Series(s.name, s.label, s.freq, c.tail(24)))}]
    if c.is_empty():
        return True
    last_ts, last = c["ts"][-1], c["value"][-1]
    delta = None
    if change == "period" and c.height > 1:
        delta = last - c["value"][-2]
    elif change == "yoy":
        prior = s.df.filter(pl.col("ts") == last_ts.replace(year=last_ts.year - 1))["value"]
        if len(prior) and prior[0]:
            delta = (last / prior[0] - 1) * 100
    p["stat"] = {"value": last, "date": last_ts.isoformat(), "change": delta,
                 "change_kind": change, "units": s.units, "label": s.label}
    p["title"] = p["title"] or s.label
    return False


def _table(p: dict, value: Value, window) -> bool:
    if isinstance(value, Table):
        p["table"] = {"columns": value.df.columns, "rows": value.df.to_dicts()}
        return value.df.is_empty()
    c = clip(value, *window)
    p["table"] = {"columns": ["ts", "value"], "rows": [{"ts": t, "value": v} for t, v in _points(c)]}
    return c.df.is_empty()
```

- [ ] **Step 5: Implement `run.py`**

```python
# src/tempus/cards/run.py
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
        return {"id": self.card_id,
                "card": self.card.model_dump(mode="json") if self.card else None,
                "payloads": self.payloads, "errors": [e.to_dict() for e in self.errors]}


def run_card(conn: psycopg.Connection, repo: CardRepository, card_id: str,
             start: date | None = None, end: date | None = None) -> CardResult:
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
```

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/cards -v`
Expected: PASS (all cards tests)

- [ ] **Step 7: Lint and commit**

```bash
mise run lint
git add src/tempus/cards/evaluate.py src/tempus/cards/render.py src/tempus/cards/run.py tests/cards/test_run.py
git commit -m "Cards: evaluate formulas, render panel payloads, run_card pipeline

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: `tempus card` CLI, example cards and lint

**Files:**
- Modify: `src/tempus/cli.py` (add the `card_app` group)
- Modify: `mise.toml` (`lint` task: add `uv run tempus card check --offline`)
- Create: `cards/crude-to-gasoline.yaml`, `cards/coffee-pass-through.yaml`
- Test: `tests/cards/test_cli.py`

**Interfaces:**
- Consumes: `FileCardRepository`, `check_card`, `run_card`, `sync_index`, `CATALOG`, `signature`, `PANELS`, `has_errors`
- Produces: `tempus card list | new | check | show | load | functions | panels`

- [ ] **Step 1: Write the example cards**

```yaml
# cards/crude-to-gasoline.yaml
version: 1
title: Crude oil → gasoline CPI pass-through
question: Does a move in crude show up in gasoline CPI, and how fast?
notes: |
  First pass (2000+): YoY r ≈ 0.88 at a 1-month lag. Pass-through to core CPI is weak (r ≈ 0.30).
tags: [energy, pass-through]
concepts: [prices.commodities.energy, prices.consumer.energy]
window: {start: 2000-01-01}

inputs:
  crude:    {series: fred:DCOILWTICO, label: Crude oil (WTI)}
  gasoline: {series: fred:CUSR0000SETB01, label: CPI gasoline}

formulas:
  crude_yoy:    yoy(resample(crude, "M", "mean"))
  gasoline_yoy: yoy(gasoline)
  passthrough:  lagcorr(crude_yoy, gasoline_yoy, nlags=12)

panels:
  - type: timeseries
    title: YoY change
    y: [crude_yoy, gasoline_yoy]
  - type: lagcorr
    data: passthrough
```

```yaml
# cards/coffee-pass-through.yaml
version: 1
title: Arabica → CPI coffee pass-through
question: How long does a move in arabica prices take to reach the coffee aisle?
notes: |
  First pass (2000+): YoY r ≈ 0.82 at a 7-month lag.
tags: [agriculture, pass-through]
concepts: [prices.commodities.agriculture, prices.consumer.food]
window: {start: 2000-01-01}

inputs:
  arabica:    {series: fred:PCOFFOTMUSDM, label: Arabica (other milds)}
  cpi_coffee: {series: fred:CUSR0000SEFP01, label: CPI coffee}

formulas:
  arabica_yoy: yoy(arabica)
  coffee_yoy:  yoy(cpi_coffee)
  passthrough: lagcorr(arabica_yoy, coffee_yoy, nlags=12)

panels:
  - type: timeseries
    title: YoY change
    y: [arabica_yoy, coffee_yoy]
  - type: lagcorr
    data: passthrough
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/cards/test_cli.py
import json
from pathlib import Path

from typer.testing import CliRunner

from tempus.cli import app

runner = CliRunner()
ROOT = Path(__file__).parents[2]


def test_check_offline_passes_on_the_example_cards(monkeypatch):
    monkeypatch.chdir(ROOT)
    result = runner.invoke(app, ["card", "check", "--offline"])
    assert result.exit_code == 0, result.output
    assert "crude-to-gasoline" in result.output


def test_check_offline_fails_on_a_bad_card(monkeypatch, tmp_path: Path):
    (tmp_path / "cards").mkdir()
    bad = Path(__file__).parent / "bad" / "unknown-name.yaml"
    (tmp_path / "cards" / "unknown-name.yaml").write_text(bad.read_text())
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["card", "check", "--offline"])
    assert result.exit_code == 1
    assert "Did you mean 'gasoline'?" in result.output


def test_new_writes_a_template_that_parses(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["card", "new", "my-card"]).exit_code == 0
    assert runner.invoke(app, ["card", "new", "my-card"]).exit_code == 1  # exists
    from tempus.cards.spec import load_card
    card, errors = load_card(tmp_path / "cards" / "my-card.yaml")
    assert errors == [] and card.question


def test_functions_and_panels_catalogs():
    out = runner.invoke(app, ["card", "functions", "lagcorr"]).output
    assert "lagcorr(x, y, nlags=12)" in out and "leads" in out
    assert "timeseries" in runner.invoke(app, ["card", "panels"]).output


def test_show_json_against_the_test_db(conn, monkeypatch, tmp_path: Path):
    from psycopg.conninfo import make_conninfo

    from tempus.config import get_settings

    from .seed import seed

    seed(conn)
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "lead.yaml").write_text(
        (Path(__file__).parent / "fixtures" / "lead.yaml").read_text())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL",
                       make_conninfo(get_settings().database_url, dbname="tempus_test"))
    result = runner.invoke(app, ["card", "show", "lead", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["payloads"][1]["annotations"]["peak"]["lag"] == 1
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/cards/test_cli.py -v`
Expected: FAIL (`No such command 'card'`)

- [ ] **Step 4: Implement the CLI group**

Add near the other sub-apps in `src/tempus/cli.py`:

```python
card_app = typer.Typer(help="Cards: saved views over series (cards/*.yaml).",
                       no_args_is_help=True)
app.add_typer(card_app, name="card")
```

Add the commands (after the ontology commands):

```python
CARD_TEMPLATE = """\
version: 1
title: {title}
question: What are you trying to find out?
# notes: |
#   Findings so far.
tags: []
concepts: []                      # e.g. [prices.consumer.energy]
window: {{start: 2000-01-01}}

inputs:
  x: fred:UNRATE                  # source:series_id, or {{concept: labor}}

formulas:
  x_yoy: diff(x, 12)              # see `tempus card functions`

panels:                           # see `tempus card panels`
  - type: timeseries
    y: [x, x_yoy]
"""


def _cards():
    from tempus.cards.repository import FileCardRepository

    return FileCardRepository(get_settings().cards_path)


def _print_card_errors(errors) -> None:
    from rich.markup import escape

    for e in errors:
        color = "red" if e.level == "error" else "yellow"
        console.print(f"[{color}]{escape(e.format())}[/{color}]")  # paths contain [0]


@card_app.command("list")
def card_list():
    """List cards with their title, panel count, concepts and tags."""
    repo = _cards()
    table = Table("id", "title", "panels", "concepts", "tags")
    for card_id in repo.ids():
        card, errors = repo.get(card_id)
        if card is None:
            table.add_row(card_id, "[red]invalid[/red]", "", "", "")
            continue
        table.add_row(card_id, card.title, str(len(card.panels)), ", ".join(card.concepts),
                      ", ".join(card.tags))
    console.print(table)


@card_app.command("new")
def card_new(card_id: str):
    """Write a commented template to cards/<id>.yaml."""
    from tempus.cards.spec import CARD_ID

    if not CARD_ID.fullmatch(card_id):
        console.print("[red]Card ids are kebab-case, e.g. crude-to-gasoline[/red]")
        raise typer.Exit(1)
    path = _cards().path(card_id)
    if path.exists():
        console.print(f"[red]{path} already exists[/red]")
        raise typer.Exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CARD_TEMPLATE.format(title=card_id.replace("-", " ").capitalize()))
    console.print(f"Wrote [bold]{path}[/bold]")


@card_app.command("check")
def card_check(
    card_id: str | None = typer.Argument(None, help="One card; all cards if omitted"),
    offline: bool = typer.Option(False, help="Static checks only, no database"),
):
    """Validate cards. Exits 1 if any card has errors."""
    from tempus.cards.check import check_card
    from tempus.cards.errors import has_errors
    from tempus.cards.resolve import resolve

    repo = _cards()
    ids = [card_id] if card_id else repo.ids()
    conn = None if offline else _connect()
    failed = False
    for cid in ids:
        card, errors = repo.get(cid)
        if card is not None:
            errors += check_card(cid, card)[1]
            if conn is not None and not has_errors(errors):
                errors += resolve(conn, cid, card)[1]
        _print_card_errors(errors)
        if has_errors(errors):
            failed = True
        else:
            console.print(f"[green]✓[/green] {cid}")
    if conn is not None:
        conn.close()
    if failed:
        raise typer.Exit(1)


@card_app.command("show")
def card_show(
    card_id: str,
    start: datetime | None = typer.Option(None, formats=["%Y-%m-%d"]),
    end: datetime | None = typer.Option(None, formats=["%Y-%m-%d"]),
    as_json: bool = typer.Option(False, "--json", help="Print the panel payloads"),
):
    """Evaluate a card. Prints a summary, or the panel payloads with --json."""
    import json

    from tempus.cards.errors import has_errors
    from tempus.cards.run import run_card

    with _connect() as conn:
        result = run_card(conn, _cards(), card_id, start and start.date(), end and end.date())
    if as_json:
        print(json.dumps(result.to_dict(), default=str, indent=2))
        raise typer.Exit(1 if has_errors(result.errors) else 0)
    _print_card_errors(result.errors)
    for p in result.payloads:
        console.print(f"\n[bold]{p['title'] or p['type']}[/bold]")
        if p["type"] == "lagcorr" and p["annotations"]:
            peak = p["annotations"]["peak"]
            console.print(f"  {p['annotations']['lead']} (r = {peak['r']:.3f})")
        elif p["type"] == "stat" and p["stat"]:
            console.print(f"  {p['stat']['value']:.4g} on {p['stat']['date']}")
        for s in p["series"]:
            vals = [v for _, v in s["points"] if v is not None]
            if vals:
                console.print(f"  {s['label']:<40} {_sparkline(vals[-40:])} "
                              f"{vals[-1]:.4g} ({s['points'][-1][0]})")
    if has_errors(result.errors):
        raise typer.Exit(1)


@card_app.command("load")
def card_load():
    """Sync cards/*.yaml into the cards index table."""
    from tempus.cards.repository import sync_index

    with _connect() as conn:
        loaded, errors = sync_index(conn, _cards())
    _print_card_errors(errors)
    console.print(f"Loaded {loaded} card(s)")


@card_app.command("functions")
def card_functions(name: str | None = typer.Argument(None)):
    """The function catalog, or one entry with its caveat."""
    from tempus.cards.functions import CATALOG, signature

    if name:
        fn = CATALOG.get(name)
        if fn is None:
            console.print(f"[red]Unknown function '{name}'[/red]")
            raise typer.Exit(1)
        console.print(f"[bold]{signature(fn)}[/bold] → {fn.returns}\n{fn.doc}\n"
                      f"[yellow]Caveat:[/yellow] {fn.caveat}")
        return
    table = Table("function", "returns", "what it does")
    for fn in CATALOG.values():
        table.add_row(signature(fn), fn.returns, fn.doc)
    console.print(table)


@card_app.command("panels")
def card_panels(name: str | None = typer.Argument(None)):
    """The panel catalog."""
    from tempus.cards.panels import PANELS, accepts_text

    for p in PANELS.values():
        if name and p.name != name:
            continue
        console.print(f"[bold]{p.name}[/bold] ({p.field}: {accepts_text(p)})\n  {p.doc}\n"
                      f"  [yellow]Not for:[/yellow] {p.when_not}")
```

In `mise.toml`, change the `lint` task's `run` list to add the card check:

```toml
run = ["uv run ruff check .", "uv run ruff format --check .", "uv run tempus card check --offline"]
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/cards/test_cli.py -v && mise run lint`
Expected: PASS, and lint prints `✓ coffee-pass-through` and `✓ crude-to-gasoline`

- [ ] **Step 6: Verify the findings against the real data**

Run: `uv run tempus card show crude-to-gasoline && uv run tempus card show coffee-pass-through`
Expected:
- "Crude oil (WTI), YoY % leads CPI gasoline, YoY % by 1 period (r = 0.875)"
- "Arabica (other milds), YoY % leads CPI coffee, YoY % by 7 periods (r = 0.829)"

These match a check run while writing this plan. If either differs, stop and investigate
before going on. Don't adjust the card to make the numbers match.

- [ ] **Step 7: Commit**

```bash
git add src/tempus/cli.py mise.toml cards tests/cards/test_cli.py
git commit -m "Cards: tempus card CLI, example pass-through cards, offline check in lint

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: `tempus serve`: FastAPI app with hot-reload events

**Files:**
- Modify: `pyproject.toml` (add `"fastapi>=0.115"`, `"uvicorn[standard]>=0.30"`, `"watchfiles>=0.24"`), then `uv sync`
- Modify: `docs/STACK.md` (Register rows for FastAPI, uvicorn, watchfiles)
- Create: `src/tempus/app.py`, `src/tempus/web/.gitkeep` (Task 11 fills it)
- Modify: `src/tempus/cli.py` (add the `serve` command)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `FileCardRepository`, `check_card`, `run_card`, `has_errors`, `Settings`
- Produces:
  - `create_app(settings: Settings | None = None) -> FastAPI`
  - `card_event(repo: FileCardRepository, path: Path) -> dict | None`
  - Endpoints: `GET /api/concepts`, `GET /api/cards`, `GET /api/cards/{card_id}?start=&end=`, `GET /api/events`, and static `/`

- [ ] **Step 1: Add the dependencies and register rows**

In `pyproject.toml` `dependencies`, add `"fastapi>=0.115"`, `"uvicorn[standard]>=0.30"` and
`"watchfiles>=0.24"`, then run `uv sync`. In `docs/STACK.md` → Register, add:

```markdown
| Engine API | FastAPI + uvicorn | OSI (MIT; BSD-3) | `tempus serve`: concepts, cards, panel payloads; generates the OpenAPI contract | Starlette alone, Flask | The API needs to serve many users (then revisit workers and auth) |
| File watching | watchfiles | OSI (MIT) | Pushes card changes to the browser over server-sent events | polling | — |
| Charts | ECharts 5 (CDN, pinned) | OSI (Apache-2.0) | Zoom and crosshair tooltips built in; drawn through one adapter per panel type | Lightweight Charts, Vega-Lite, AG Charts | See Open decisions → Chart renderers |
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_app.py
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from tempus.app import card_event, create_app
from tempus.cards.repository import FileCardRepository
from tempus.config import Settings, get_settings

from .cards.seed import seed

LEAD = Path(__file__).parent / "cards" / "fixtures" / "lead.yaml"


def _lead_card() -> str:
    return LEAD.read_text()


@pytest.fixture
def settings(tmp_path: Path):
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "lead.yaml").write_text(_lead_card())
    (tmp_path / "cards" / "broken.yaml").write_text("title: [oops\n")
    url = make_conninfo(get_settings().database_url, dbname="tempus_test")
    return Settings(database_url=url, cards_path=tmp_path / "cards")


def test_cards_list_needs_no_db(settings):
    client = TestClient(create_app(settings))
    cards = {c["id"]: c for c in client.get("/api/cards").json()}
    assert cards["lead"]["title"] == "Leader → follower" and cards["lead"]["errors"] == 0
    assert cards["broken"]["errors"] == 1


def test_card_payloads_and_errors(conn, settings):
    seed(conn)
    client = TestClient(create_app(settings))
    body = client.get("/api/cards/lead").json()
    assert body["payloads"][0]["type"] == "timeseries"
    assert client.get("/api/cards/lead?start=2030-01-01").json()["errors"]
    assert client.get("/api/cards/nope").status_code == 404
    broken = client.get("/api/cards/broken").json()
    assert broken["errors"][0]["message"].startswith("invalid YAML")


def test_concepts_tree_with_counts_and_cards(conn, settings):
    seed(conn)
    tree = TestClient(create_app(settings)).get("/api/concepts").json()
    prices = tree["tree"][0]
    assert prices["id"] == "prices" and prices["series"] == 1
    assert [c["id"] for c in prices["children"]] == ["prices.energy", "prices.empty"]
    assert "lead" in tree["unfiled_cards"]


def test_card_event(settings, tmp_path: Path):
    repo = FileCardRepository(settings.cards_path)
    assert card_event(repo, settings.cards_path / "lead.yaml") == {"event": "card-changed",
                                                                   "card": "lead"}
    invalid = card_event(repo, settings.cards_path / "broken.yaml")
    assert invalid["event"] == "card-invalid" and invalid["errors"]
    assert card_event(repo, settings.cards_path / "gone.yaml") == {"event": "card-removed",
                                                                   "card": "gone"}
    assert card_event(repo, settings.cards_path / "notes.txt") is None


def test_static_index_is_served(settings):
    assert TestClient(create_app(settings)).get("/").status_code == 200
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tempus.app'`

- [ ] **Step 4: Implement `app.py` and a placeholder page**

Create `src/tempus/web/index.html` with `<!doctype html><title>tempus</title>` for now.
Task 11 replaces it.

```python
# src/tempus/app.py
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
            out.append({
                "id": card_id,
                "title": card.title if card else card_id,
                "question": card.question if card else None,
                "concepts": card.concepts if card else [],
                "tags": card.tags if card else [],
                "errors": sum(e.level == "error" for e in errors),
            })
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
        nodes = {r["id"]: {"id": r["id"], "name": r["name"], "series": r["series"],
                           "cards": [c["id"] for c in cards if r["id"] in c["concepts"]],
                           "children": []} for r in rows}
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
```

Add to `src/tempus/cli.py`:

```python
@app.command()
def serve(port: int = typer.Option(8765), host: str = typer.Option("127.0.0.1")):
    """Run the tempus app: browse concepts and cards, with live reload on save."""
    import uvicorn

    from tempus.app import create_app

    console.print(f"tempus → [bold]http://{host}:{port}[/bold]")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 6: Lint and commit**

```bash
mise run lint
git add pyproject.toml uv.lock docs/STACK.md src/tempus/app.py src/tempus/web src/tempus/cli.py tests/test_app.py
git commit -m "tempus serve: FastAPI engine API with hot-reload events

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Static front end with the ECharts adapter

**Files:**
- Create: `src/tempus/web/index.html` (replace the placeholder), `src/tempus/web/style.css`, `src/tempus/web/theme.json`, `src/tempus/web/app.js`, `src/tempus/web/adapters/echarts.js`
- Test: manual (no browser tests in v0, per the spec)

**Interfaces:**
- Consumes: the Task 10 endpoints and the payload shape from Task 8
- Produces: `renderPanel(el: HTMLElement, payload: object, theme: object)` in `adapters/echarts.js`

- [ ] **Step 1: Write `theme.json`**

```json
{
  "color": {
    "bg": "#0f1115", "panel": "#171a21", "border": "#262b36", "text": "#e6e8ee",
    "muted": "#8b93a7", "grid": "#262b36", "accent": "#7aa2f7",
    "error": "#f7768e", "warning": "#e0af68",
    "series": ["#7aa2f7", "#e0af68", "#9ece6a", "#f7768e", "#bb9af7", "#7dcfff"]
  },
  "font": {
    "family": "Inter, system-ui, -apple-system, sans-serif",
    "mono": "ui-monospace, SFMono-Regular, Menlo, monospace",
    "size": 13
  },
  "space": 8,
  "radius": 8
}
```

- [ ] **Step 2: Write `index.html` and `style.css`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>tempus</title>
  <link rel="stylesheet" href="/style.css">
  <script src="https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"></script>
</head>
<body>
  <aside id="sidebar"><h1>tempus</h1><nav id="tree"></nav></aside>
  <main id="main"><p class="muted">Pick a card.</p></main>
  <script type="module" src="/app.js"></script>
</body>
</html>
```

```css
/* Colors, fonts and spacing come from theme.json via CSS variables set in app.js. */
* { box-sizing: border-box; }
body { margin: 0; display: grid; grid-template-columns: 300px 1fr; min-height: 100vh;
       background: var(--bg); color: var(--text); font: var(--size) var(--family); }
aside { border-right: 1px solid var(--border); padding: calc(var(--space) * 2); overflow-y: auto;
        max-height: 100vh; position: sticky; top: 0; }
h1 { font-size: 16px; margin: 0 0 calc(var(--space) * 2); letter-spacing: 0.04em; }
main { padding: calc(var(--space) * 3); max-width: 1200px; }
.muted { color: var(--muted); }
details { margin-left: calc(var(--space) * 1.5); }
summary { cursor: pointer; padding: 2px 0; }
summary .count { color: var(--muted); font-size: 11px; margin-left: 4px; }
a.card { display: block; color: var(--accent); text-decoration: none; padding: 2px 0 2px 18px; }
a.card.active { font-weight: 600; }
a.card.invalid { color: var(--error); }
.question { color: var(--muted); font-size: 15px; margin-top: -8px; }
.notes { white-space: pre-wrap; color: var(--muted); border-left: 2px solid var(--border);
         padding-left: calc(var(--space) * 1.5); }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius);
         padding: calc(var(--space) * 2); margin: calc(var(--space) * 2) 0; }
.panel h3 { margin: 0 0 var(--space); font-size: 14px; }
.panel .sub { color: var(--muted); font-size: 12px; margin-bottom: var(--space); }
.chart { height: 340px; }
.stat .value { font-size: 36px; font-weight: 600; }
.stat .chart { height: 60px; }
.errors { border: 1px solid var(--error); border-radius: var(--radius);
          padding: calc(var(--space) * 1.5); font-family: var(--mono); font-size: 12px;
          white-space: pre-wrap; color: var(--error); }
.warning { color: var(--warning); font-size: 12px; }
table { border-collapse: collapse; font-family: var(--mono); font-size: 12px; }
td, th { padding: 2px 10px; border-bottom: 1px solid var(--border); text-align: right; }
```

- [ ] **Step 3: Write `adapters/echarts.js`**

```js
// Panel payload → ECharts. One function per panel type; the only file that knows ECharts.
const fmt = (v) => (v == null ? "—" : Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2));

function base(theme) {
  const c = theme.color;
  return {
    backgroundColor: "transparent",
    color: c.series,
    textStyle: { color: c.text, fontFamily: theme.font.family },
    grid: { left: 56, right: 24, top: 32, bottom: 64 },
    tooltip: { trigger: "axis", backgroundColor: c.panel, borderColor: c.border,
               textStyle: { color: c.text }, valueFormatter: fmt },
    legend: { top: 0, textStyle: { color: c.muted } },
  };
}

const axis = (theme, extra) => ({
  axisLine: { lineStyle: { color: theme.color.grid } },
  splitLine: { lineStyle: { color: theme.color.grid } },
  axisLabel: { color: theme.color.muted },
  ...extra,
});

const OPTIONS = {
  timeseries(p, theme) {
    return {
      ...base(theme),
      tooltip: { ...base(theme).tooltip, axisPointer: { type: "cross" } },
      xAxis: axis(theme, { type: "time" }),
      yAxis: axis(theme, { type: "value", scale: true }),
      dataZoom: [{ type: "inside" }, { type: "slider", height: 20, bottom: 16 }],
      series: p.series.map((s) => ({
        name: s.label, type: "line", showSymbol: false, connectNulls: false,
        data: s.points,
      })),
    };
  },
  lagcorr(p, theme) {
    const peak = p.annotations.peak;
    const rows = p.table.rows;
    return {
      ...base(theme),
      legend: { show: false },
      xAxis: axis(theme, { type: "category", name: "lag", data: rows.map((r) => r.lag) }),
      yAxis: axis(theme, { type: "value", name: "r", min: -1, max: 1 }),
      series: [{
        type: "bar",
        data: rows.map((r) => ({
          value: r.r,
          itemStyle: { color: peak && r.lag === peak.lag ? theme.color.accent : theme.color.grid },
        })),
      }],
    };
  },
  sparkline(p, theme) {
    return {
      grid: { left: 0, right: 0, top: 4, bottom: 4 },
      xAxis: { type: "time", show: false }, yAxis: { type: "value", show: false, scale: true },
      series: [{ type: "line", showSymbol: false, data: p.series[0].points,
                 lineStyle: { color: theme.color.accent } }],
    };
  },
};

export function renderPanel(el, payload, theme) {
  if (payload.type === "table") {
    const t = payload.table;
    el.innerHTML = `<table><tr>${t.columns.map((c) => `<th>${c}</th>`).join("")}</tr>${
      t.rows.map((r) => `<tr>${t.columns.map((c) => `<td>${
        typeof r[c] === "number" ? fmt(r[c]) : r[c] ?? "—"}</td>`).join("")}</tr>`).join("")
    }</table>`;
    return null;
  }
  if (payload.type === "stat") {
    const s = payload.stat;
    el.classList.add("stat");
    el.innerHTML = s
      ? `<div class="value">${fmt(s.value)}</div><div class="sub">${s.date}${
          s.change == null ? "" : ` · ${s.change >= 0 ? "+" : ""}${fmt(s.change)}${
            s.change_kind === "yoy" ? "% YoY" : " vs prior"}`}</div><div class="chart"></div>`
      : "";
    if (!s) return null;
    el = el.querySelector(".chart");
    payload = { ...payload, type: "sparkline" };
  }
  const chart = echarts.init(el, null, { renderer: "canvas" });
  chart.setOption(OPTIONS[payload.type](payload, theme));
  new ResizeObserver(() => chart.resize()).observe(el);
  return chart;
}
```

- [ ] **Step 4: Write `app.js`**

```js
// The tempus app: concept tree + card view, hot-reloading over /api/events.
import { renderPanel } from "./adapters/echarts.js";

const theme = await (await fetch("/theme.json")).json();
const root = document.documentElement.style;
for (const [k, v] of Object.entries(theme.color)) if (typeof v === "string") root.setProperty(`--${k}`, v);
root.setProperty("--family", theme.font.family);
root.setProperty("--mono", theme.font.mono);
root.setProperty("--size", `${theme.font.size}px`);
root.setProperty("--space", `${theme.space}px`);
root.setProperty("--radius", `${theme.radius}px`);

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
let charts = [];
const current = () => decodeURIComponent(location.hash.replace(/^#\/cards\//, "")) || null;

async function loadTree() {
  const [cards, concepts] = await Promise.all([
    fetch("/api/cards").then((r) => r.json()),
    fetch("/api/concepts").then((r) => r.json()).catch(() => ({ tree: [], unfiled_cards: [] })),
  ]);
  const byId = Object.fromEntries(cards.map((c) => [c.id, c]));
  const link = (id) => {
    const c = byId[id];
    const cls = ["card", id === current() ? "active" : "", c?.errors ? "invalid" : ""].join(" ");
    return `<a class="${cls}" href="#/cards/${esc(id)}" title="${esc(c?.question)}">${esc(c?.title ?? id)}</a>`;
  };
  const node = (n) => {
    const count = n.series + n.cards.length;
    return `<details ${n.cards.length || n.children.some((c) => c.cards.length) ? "open" : ""}>
      <summary>${esc(n.name)}<span class="count">${count || ""}</span></summary>
      ${n.cards.map(link).join("")}${n.children.map(node).join("")}</details>`;
  };
  const unfiled = concepts.unfiled_cards.length
    ? `<details open><summary>Unfiled</summary>${concepts.unfiled_cards.map(link).join("")}</details>` : "";
  document.getElementById("tree").innerHTML = concepts.tree.map(node).join("") + unfiled;
}

async function showCard(id) {
  charts.forEach((c) => c?.dispose());
  charts = [];
  const main = document.getElementById("main");
  if (!id) return;
  const res = await fetch(`/api/cards/${encodeURIComponent(id)}`);
  if (!res.ok) { main.innerHTML = `<p class="muted">No card '${esc(id)}'.</p>`; return; }
  const body = await res.json();
  const card = body.card;
  const errors = body.errors.filter((e) => e.level === "error");
  const warnings = body.errors.filter((e) => e.level === "warning");
  const fmtErr = (e) => `${esc(e.path)}\n  ${esc(e.message)}${e.hint ? " " + esc(e.hint) : ""}`;
  main.innerHTML = `
    <h2>${esc(card?.title ?? id)}</h2>
    ${card ? `<p class="question">${esc(card.question)}</p>` : ""}
    ${card?.notes ? `<div class="notes">${esc(card.notes)}</div>` : ""}
    ${errors.length ? `<div class="errors">${errors.map(fmtErr).join("\n\n")}</div>` : ""}
    ${warnings.map((w) => `<p class="warning">⚠ ${esc(w.path)}: ${esc(w.message)}</p>`).join("")}
    <div id="panels"></div>`;
  const panels = document.getElementById("panels");
  for (const p of body.payloads) {
    const wrap = document.createElement("section");
    wrap.className = "panel";
    const sub = p.annotations?.lead ? `<div class="sub">${esc(p.annotations.lead)} (r = ${p.annotations.peak.r.toFixed(3)})</div>` : "";
    wrap.innerHTML = `<h3>${esc(p.title ?? p.type)}</h3>${sub}<div class="${p.type === "table" || p.type === "stat" ? "" : "chart"}"></div>`;
    panels.appendChild(wrap);
    charts.push(renderPanel(wrap.lastElementChild, p, theme));
  }
}

async function refresh() { await loadTree(); await showCard(current()); }

window.addEventListener("hashchange", refresh);
const events = new EventSource("/api/events");
for (const kind of ["card-changed", "card-invalid", "card-removed"]) {
  events.addEventListener(kind, (e) => {
    const { card } = JSON.parse(e.data);
    loadTree();
    if (card === current()) showCard(card);
  });
}
refresh();
```

- [ ] **Step 5: Verify by hand**

```bash
mise run db:up
uv run tempus card load
uv run tempus serve
```

Open http://127.0.0.1:8765 and check:
1. The tree shows Prices → Commodities/Consumer, with both cards under their concepts.
2. Clicking "Crude oil → gasoline CPI pass-through" shows the question, notes, a zoomable YoY chart with a crosshair, and lag bars with lag 1 highlighted and "… leads … by 1 period (r = 0.875)".
3. Edit `cards/crude-to-gasoline.yaml`: change `nlags=12` to `nlags=6` and save. The lag chart updates within about 2 seconds, without a page refresh.
4. Change `yoy(gasoline)` to `yoy(gasolin)` and save. The card shows the error with "Did you mean 'gasoline'?". Revert, and it renders again.
5. Run `uv run pytest tests/test_app.py::test_static_index_is_served -v`. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
mise run lint
git add src/tempus/web
git commit -m "tempus serve: static front end with ECharts adapter and theme tokens

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Update the docs and close out the spec

**Files:**
- Modify: `docs/ARCHITECTURE.md`, `docs/STACK.md`, `README.md`, `docs/specs/2026-10-04-cards-slice-1-design.md`

- [ ] **Step 1: `ARCHITECTURE.md`**

- Components table:
  - Add `| Cards | ✅ built | cards/, src/tempus/cards/ | Card language v1 (inputs, formulas, panels), static checks, Polars engine, panel payloads |`
  - Add `| App | ✅ built | src/tempus/app.py, src/tempus/web/ | tempus serve: FastAPI engine API + no-build static front end (ECharts), hot reload |`
  - Change the "Web front end" row to point at the App row (status "🟡 cards view built; explore view is SPA-23")
- Data flow: add `cards/*.yaml ──▶ card engine (check · resolve · evaluate · render) ──▶ panel payloads ──▶ tempus serve ──▶ browser`
- Data model: add a **`cards`** bullet: an index of `cards/*.yaml` (the files are the source of truth), rebuilt by `tempus card load`
- Decisions (newest first), dated 2026-10-04:
  - **Cards are evaluated in a Python engine; the web app reads through it.** This reverses "the web app reads TimescaleDB directly": cards are the shared logic that decision said would trigger a revisit. Spec: `docs/specs/2026-10-04-cards-slice-1-design.md`.
  - **Derived data lives in card formulas** (resolves the open question). Functions may later push down to SQL without changing the language.
- Open questions: delete "Derived data"

- [ ] **Step 2: `STACK.md`**

- Open decisions: set Card storage, Expression language, Panel grammar, Engine API and Web framework to Decided. Make Chart renderers "Decided for v0 (ECharts)".
- The register rows for Polars, FastAPI/uvicorn, watchfiles and ECharts were added in Tasks 3 and 10. Check that they're present.

- [ ] **Step 3: `README.md`**

Add a **Cards** section after **CLI**:

````markdown
## Cards

A card is a saved view: inputs (series), formulas that combine them, and panels that show
the result. Cards are YAML files in `cards/`, checked in lint and rendered by `tempus serve`.

```sh
tempus card new my-card            # template in cards/my-card.yaml
tempus card check                  # validate (add --offline to skip the DB)
tempus card show crude-to-gasoline # summary; --json prints the panel payloads
tempus card functions | panels     # the vocabulary
tempus serve                       # http://127.0.0.1:8765, reloads when a card is saved
```

See `cards/crude-to-gasoline.yaml` for the format, and
`docs/specs/2026-10-04-cards-slice-1-design.md` for the full design.
````

- [ ] **Step 4: The spec header**

In `docs/specs/2026-10-04-cards-slice-1-design.md`, change `- **Status:** Approved 2026-10-04 (PR #1)` to
`- **Status:** Implemented <YYYY-MM-DD>`, using the date this task runs. If anything departed
from the spec during implementation, add a `## Changes during implementation` section at the end
listing each change in one line.

- [ ] **Step 5: Final verification and commit**

```bash
mise run test
mise run lint
git add docs README.md
git commit -m "Docs: cards and tempus serve in ARCHITECTURE, STACK and README; spec implemented

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Expected: every test passes (DB tests run with the DB up), and lint passes, including both
cards.

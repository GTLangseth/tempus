"""Grade drafts against the eval set: docs/specs/2026-10-05-question-to-card-design.md → Grading.

A draft passes when every stage passes, in order: shape, series, valid, renders. A grade
names the first stage that failed and why, in one line.
"""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from statistics import mean
from typing import Literal

import psycopg

from tempus.cards.check import check_card
from tempus.cards.errors import CardError
from tempus.cards.run import run_spec
from tempus.drafting import SHAPES, Draft, Drafter
from tempus.evals import Case

Stage = Literal["shape", "series", "valid", "renders"]
STAGES: tuple[Stage, ...] = ("shape", "series", "valid", "renders")
MIN_POINTS = 24


@dataclass(frozen=True)
class Grade:
    case_id: str
    passed: bool
    stage: Stage | None  # the first stage that failed; None when passed
    reason: str | None
    xfail: bool

    def past(self, stage: Stage) -> bool:
        """Whether this draft got past `stage`."""
        return self.passed or STAGES.index(self.stage) > STAGES.index(stage)


def grade(case: Case, draft: Draft, conn: psycopg.Connection | None) -> Grade:
    """Grade one draft. `conn` is only used once the card reaches the renders stage."""
    for stage, check in (
        ("shape", _shape),
        ("series", _series),
        ("valid", _valid),
        ("renders", lambda c, d: _renders(c, d, conn)),
    ):
        reason = check(case, draft)
        if reason is not None:
            return Grade(case.id, False, stage, reason, case.xfail is not None)
    return Grade(case.id, True, None, None, case.xfail is not None)


def _p(probabilities: dict[str, float], name: str) -> str:
    p = probabilities.get(name)
    return f"{name} ({p:.2f})" if p is not None else name


def _shape(case: Case, draft: Draft) -> str | None:
    choice = draft.shape.choice
    if choice == case.shape or choice in case.also:
        return None
    probs = draft.shape.probabilities
    return f"chose {_p(probs, choice)}, expected {_p(probs, case.shape)}"


def _series(case: Case, draft: Draft) -> str | None:
    """Picks against the case's roles, which are written for the case's shape.

    An `also` shape with a different role count can't be compared role by role, so it
    passes. Either order passes when the case's shape or the chosen shape is unordered:
    an unordered case's lists don't encode an order, and an unordered choice doesn't use one.
    """
    chosen = SHAPES[draft.shape.choice]
    if len(chosen.roles) != len(case.roles):
        return None
    if len(draft.picks) != len(case.roles):
        return f"picked {len(draft.picks)} series for {len(case.roles)} roles"
    miss = _first_miss(draft.picks, case.roles)
    if miss is None:
        return None
    unordered = not (chosen.ordered and SHAPES[case.shape].ordered)
    if unordered and _first_miss(draft.picks[::-1], case.roles) is None:
        return None
    pick, acceptable = miss
    return (
        f"{pick.role}: picked {pick.series} ({pick.probability:.2f}), "
        f"acceptable {', '.join(acceptable)}"
    )


def _first_miss(picks, roles):
    for pick, acceptable in zip(picks, roles, strict=True):
        if pick.series not in acceptable:
            return pick, acceptable
    return None


def _first_error(errors: list[CardError]) -> str | None:
    return next((e.message for e in errors if e.level == "error"), None)


def _valid(case: Case, draft: Draft) -> str | None:
    if draft.card is None:
        return draft.error or "no card"
    return _first_error(check_card(case.id, draft.card)[1])


def _renders(case: Case, draft: Draft, conn: psycopg.Connection) -> str | None:
    result = run_spec(conn, case.id, draft.card)
    error = _first_error(result.errors)
    if error is not None:
        return error
    first = next((p for p in result.payloads if p["panel"] == 0), None)
    points = _points(first) if first else 0
    if points < MIN_POINTS:
        return f"first panel has {points} points, needs {MIN_POINTS}"
    return None


def _points(payload: dict) -> int:
    """How much history a panel shows.

    A timeseries counts its shortest series. A stat counts its sparkline, which holds the
    last 24 points at most. A lagcorr table counts the observations behind its correlations
    (its largest `n`), not its handful of lags. A table of a series counts its rows.
    """
    table = payload["table"]
    if table is not None:
        if "n" in table["columns"]:
            return max((r["n"] for r in table["rows"]), default=0)
        return len(table["rows"])
    return min((len(s["points"]) for s in payload["series"]), default=0)


@dataclass(frozen=True)
class Accuracy:
    mean: float
    low: float
    high: float

    def to_json(self) -> dict:
        return {"mean": self.mean, "min": self.low, "max": self.high}


def _accuracy(per_run: list[float]) -> Accuracy:
    return Accuracy(mean(per_run), min(per_run), max(per_run))


def _share(hits: int, total: int) -> float:
    return hits / total if total else 0.0


@dataclass(frozen=True)
class Report:
    """Every grade from an eval, one tuple per run in case order.

    Accuracy, pass rate and failures count only cases without `xfail`; xfail cases are
    reported apart, by which of them passed at least once.
    """

    grades: tuple[tuple[Grade, ...], ...]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def _scored(self, run: tuple[Grade, ...]) -> list[Grade]:
        return [g for g in run if not g.xfail]

    @property
    def stage_accuracy(self) -> dict[Stage, Accuracy]:
        """Per stage, the share of scored cases that got past it, across runs."""
        return {
            stage: _accuracy(
                [
                    _share(sum(g.past(stage) for g in self._scored(run)), len(self._scored(run)))
                    for run in self.grades
                ]
            )
            for stage in STAGES
        }

    @property
    def pass_rate(self) -> Accuracy:
        return _accuracy(
            [
                _share(sum(g.passed for g in self._scored(run)), len(self._scored(run)))
                for run in self.grades
            ]
        )

    @property
    def failures(self) -> dict[Stage, int]:
        counts = Counter(g.stage for run in self.grades for g in self._scored(run))
        return {stage: counts[stage] for stage in STAGES}

    @property
    def limiting_stage(self) -> Stage | None:
        """The stage with the most failures; the earlier stage wins a tie."""
        failures = self.failures
        worst = max(STAGES, key=lambda s: failures[s])
        return worst if failures[worst] else None

    @property
    def case_passes(self) -> dict[str, int]:
        passes: dict[str, int] = {}
        for run in self.grades:
            for g in run:
                passes[g.case_id] = passes.get(g.case_id, 0) + g.passed
        return passes

    @property
    def xfail_passed(self) -> list[str]:
        """The xfail cases that passed in at least one run."""
        return list(
            dict.fromkeys(g.case_id for run in self.grades for g in run if g.xfail and g.passed)
        )

    def _latest_failures(self) -> dict[str, Grade]:
        latest: dict[str, Grade] = {}
        for run in self.grades:
            for g in run:
                if not g.passed:
                    latest[g.case_id] = g
        return latest

    def _cases(self) -> list[dict]:
        latest = self._latest_failures()
        xfail = {g.case_id for run in self.grades for g in run if g.xfail}
        return [
            {
                "id": case_id,
                "passed": passed,
                "xfail": case_id in xfail,
                "stage": latest[case_id].stage if case_id in latest else None,
                "reason": latest[case_id].reason if case_id in latest else None,
            }
            for case_id, passed in self.case_passes.items()
        ]

    def to_text(self) -> str:
        runs = len(self.grades)
        cases = self._cases()
        width = max((len(c["id"]) for c in cases), default=0)
        lines = []
        for c in cases:
            line = f"{c['id']:<{width}}  {c['passed']}/{runs}"
            if c["stage"] is not None:
                tag = "xfail  " if c["xfail"] else ""
                line += f"  {tag}{c['stage']}: {c['reason']}"
            lines.append(line)
        lines += ["", f"{'stage':<8} {'mean':>4}  range"]
        rows = [*self.stage_accuracy.items(), ("passed", self.pass_rate)]
        lines += [f"{name:<8} {a.mean:.2f}  {a.low:.2f}-{a.high:.2f}" for name, a in rows]
        limiting = self.limiting_stage
        if limiting is None:
            lines.append("limiting stage: none")
        else:
            lines.append(f"limiting stage: {limiting} ({self.failures[limiting]} failures)")
        xfail_cases = [c["id"] for c in cases if c["xfail"]]
        if xfail_cases:
            passed = self.xfail_passed
            names = f" ({', '.join(passed)})" if passed else ""
            lines.append(f"xfail: {len(passed)} of {len(xfail_cases)} passed at least once{names}")
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "runs": len(self.grades),
            "timestamp": self.timestamp.isoformat(),
            "stages": {s: a.to_json() for s, a in self.stage_accuracy.items()},
            "passed": self.pass_rate.to_json(),
            "limiting_stage": self.limiting_stage,
            "failures": self.failures,
            "cases": self._cases(),
            "xfail_passed": self.xfail_passed,
            "grades": [[asdict(g) for g in run] for run in self.grades],
        }


def run_eval(
    cases: Sequence[Case],
    drafter: Drafter,
    conn: psycopg.Connection,
    *,
    runs: int = 3,
    on_case: Callable[[int, Case, Grade], None] | None = None,
) -> Report:
    """Draft and grade every case `runs` times; `on_case(run, case, grade)` gets 1-based runs.

    A drafter that raises fails the case at `shape`, the first stage, since nothing was
    graded: the reason reads "drafter raised: <type>: <message>". The four stages stay the
    whole vocabulary, so accuracy and the limiting stage need no fifth bucket.
    """
    grades = []
    for run in range(1, runs + 1):
        graded = []
        for case in cases:
            try:
                draft = drafter.draft(case.question)
            except Exception as exc:
                reason = f"drafter raised: {type(exc).__name__}: {exc}"
                g = Grade(case.id, False, "shape", reason, case.xfail is not None)
            else:
                g = grade(case, draft, conn)
            graded.append(g)
            if on_case is not None:
                on_case(run, case, g)
        grades.append(tuple(graded))
    return Report(tuple(grades))

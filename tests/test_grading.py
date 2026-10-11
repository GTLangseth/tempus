import pytest

from tempus.cards.spec import Card
from tempus.drafting import Draft, Pick
from tempus.evals import Case
from tempus.grading import Grade, grade, run_eval
from tempus.jev import ChoiceAnswer

from .cards.seed import seed

OIL_GAS = Case(
    id="oil-gas",
    question="Does oil lead gasoline?",
    shape="lead_lag",
    roles=(("fred:DCOILWTICO", "fred:DCOILBRENTEU"), ("fred:GASREGW",)),
    also=("compare", "level"),
)
LEAD = Case(
    id="lead",
    question="Does the leader lead the follower?",
    shape="lead_lag",
    roles=(("fred:XLEAD",), ("fred:YFOLLOW",)),
)
SIDE_BY_SIDE = Case(
    id="side",
    question="Leader and follower side by side",
    shape="compare",
    roles=(("fred:XLEAD",), ("fred:YFOLLOW",)),
)
LEVEL = Case(id="level", question="Where is the leader?", shape="level", roles=(("fred:XLEAD",),))
WEEKLY = Case(id="weekly", question="The weekly thing?", shape="level", roles=(("fred:WEEKLY",),))


def card(inputs, panels, formulas=None) -> Card:
    return Card.model_validate(
        {
            "version": 1,
            "title": "t",
            "question": "q",
            "inputs": inputs,
            "formulas": formulas or {},
            "panels": panels,
        }
    )


def draft(choice, picks, card=None, error=None, probabilities=None) -> Draft:
    probabilities = probabilities or {choice: 0.9}
    return Draft(
        question="q",
        shape=ChoiceAnswer(choice, probabilities),
        picks=[Pick(role, series, p) for role, series, p in picks],
        card=card,
        error=error,
    )


LEAD_LAG_PICKS = [
    ("the measure that moves first", "fred:DCOILWTICO", 0.8),
    ("the measure that responds later", "fred:GASREGW", 0.7),
]
TS_X = card({"x": "fred:XLEAD"}, [{"type": "timeseries", "y": "x"}])
TS_XY = card({"x": "fred:XLEAD", "y": "fred:YFOLLOW"}, [{"type": "timeseries", "y": ["x", "y"]}])


def test_shape_miss_names_both_probabilities():
    d = draft("spread", [], probabilities={"spread": 0.55, "lead_lag": 0.37, "compare": 0.08})
    assert grade(OIL_GAS, d, None) == Grade(
        "oil-gas", False, "shape", "chose spread (0.55), expected lead_lag (0.37)", False
    )


def test_also_shape_passes_shape_and_is_graded_against_the_case_roles():
    picks = [
        ("the first measure", "fred:DCOILBRENTEU", 0.6),
        ("the second measure", "fred:GASREGW", 0.5),
    ]
    g = grade(OIL_GAS, draft("compare", picks, error="units differ"), None)
    assert (g.stage, g.reason) == ("valid", "units differ")


def test_also_shape_with_another_role_count_skips_series():
    g = grade(OIL_GAS, draft("level", [("the measure", "fred:CPIAUCSL", 0.4)], error="none"), None)
    assert (g.stage, g.reason) == ("valid", "none")


def test_series_miss_names_role_pick_and_acceptable():
    picks = [
        ("the measure that moves first", "fred:GASREGW", 0.61),
        ("the measure that responds later", "fred:DCOILWTICO", 0.5),
    ]
    g = grade(OIL_GAS, draft("lead_lag", picks), None)
    assert (g.passed, g.stage, g.reason) == (
        False,
        "series",
        "the measure that moves first: picked fred:GASREGW (0.61), "
        "acceptable fred:DCOILWTICO, fred:DCOILBRENTEU",
    )


def test_wrong_number_of_picks_fails_series():
    d = draft("lead_lag", LEAD_LAG_PICKS[:1])
    assert grade(OIL_GAS, d, None).reason == "picked 1 series for 2 roles"


def test_unordered_swap_passes_series():
    picks = [
        ("the first measure", "fred:YFOLLOW", 0.6),
        ("the second measure", "fred:XLEAD", 0.6),
    ]
    g = grade(SIDE_BY_SIDE, draft("compare", picks, error="x"), None)
    assert g.stage == "valid"


def test_unordered_miss_reports_the_first_role_in_order():
    picks = [
        ("the first measure", "fred:YFOLLOW", 0.6),
        ("the second measure", "fred:WEEKLY", 0.3),
    ]
    g = grade(SIDE_BY_SIDE, draft("compare", picks), None)
    assert g.reason == "the first measure: picked fred:YFOLLOW (0.60), acceptable fred:XLEAD"


def test_ordered_swap_fails_series():
    picks = [
        ("the measure that moves first", "fred:YFOLLOW", 0.6),
        ("the measure that responds later", "fred:XLEAD", 0.6),
    ]
    g = grade(LEAD, draft("lead_lag", picks), None)
    assert (g.stage, g.reason) == (
        "series",
        "the measure that moves first: picked fred:YFOLLOW (0.60), acceptable fred:XLEAD",
    )


def test_no_card_fails_valid_with_the_draft_error():
    d = draft("lead_lag", LEAD_LAG_PICKS, error="spread needs the same units: $/bbl vs $/gal")
    g = grade(OIL_GAS, d, None)
    assert (g.stage, g.reason) == ("valid", "spread needs the same units: $/bbl vs $/gal")


def test_check_card_error_fails_valid():
    bad = card({"x": "fred:XLEAD"}, [{"type": "timeseries", "y": "z"}])
    g = grade(OIL_GAS, draft("lead_lag", LEAD_LAG_PICKS, bad), None)
    assert (g.stage, g.reason) == ("valid", "unknown name 'z'.")


def test_short_series_fails_renders(conn):
    seed(conn)
    weekly = card({"w": "fred:WEEKLY"}, [{"type": "timeseries", "y": "w"}])
    g = grade(WEEKLY, draft("level", [("the measure", "fred:WEEKLY", 0.9)], weekly), conn)
    assert g == Grade("weekly", False, "renders", "first panel has 2 points, needs 24", False)


def test_run_errors_fail_renders(conn):
    seed(conn)
    missing = card({"x": "fred:NOPE"}, [{"type": "timeseries", "y": "x"}])
    g = grade(LEVEL, draft("level", [("the measure", "fred:XLEAD", 0.9)], missing), conn)
    assert (g.stage, g.reason) == ("renders", "series 'fred:NOPE' isn't in the database.")


@pytest.mark.parametrize(
    "panel,formulas,points",
    [
        ({"type": "stat", "y": "x"}, {}, None),  # a stat sparkline holds its last 24 points
        ({"type": "lagcorr", "data": "l"}, {"l": "lagcorr(x, y, nlags=3)"}, None),
        ({"type": "table", "data": "y"}, {}, None),
        ({"type": "timeseries", "y": ["x", "w"]}, {}, 2),  # the shortest series counts
    ],
)
def test_points_are_counted_per_panel_type(conn, panel, formulas, points):
    seed(conn)
    c = card({"x": "fred:XLEAD", "y": "fred:YFOLLOW", "w": "fred:WEEKLY"}, [panel], formulas)
    g = grade(LEAD, draft("lead_lag", [("a", "fred:XLEAD", 1), ("b", "fred:YFOLLOW", 1)], c), conn)
    expected = None if points is None else f"first panel has {points} points, needs 24"
    assert g.reason == expected


def test_full_pass(conn):
    seed(conn)
    d = draft("level", [("the measure", "fred:XLEAD", 0.9)], TS_X)
    assert grade(LEVEL, d, conn) == Grade("level", True, None, None, False)


def test_xfail_is_carried_on_the_grade():
    case = Case("hard", "q", "level", (("fred:XLEAD",),), xfail="no such data")
    assert grade(case, draft("spread", []), None).xfail is True


class ScriptedDrafter:
    """Returns the next scripted draft for a question; an exception in the script is raised."""

    def __init__(self, script):
        self.script = {q: list(steps) for q, steps in script.items()}

    def draft(self, question):
        step = self.script[question].pop(0)
        if isinstance(step, Exception):
            raise step
        return step


XFAIL = Case("hard", "Too hard?", "level", (("fred:XLEAD",),), xfail="no such data yet")
LEAD_PICKS = [
    ("the measure that moves first", "fred:XLEAD", 0.8),
    ("the measure that responds later", "fred:YFOLLOW", 0.7),
]
SWAPPED = [
    ("the measure that moves first", "fred:YFOLLOW", 0.55),
    ("the measure that responds later", "fred:XLEAD", 0.5),
]


@pytest.fixture
def report(conn):
    seed(conn)
    good_level = draft("level", [("the measure", "fred:XLEAD", 0.9)], TS_X)
    no_card = draft("level", [("the measure", "fred:WEEKLY", 0.9)], error="no units")
    drafter = ScriptedDrafter(
        {
            LEVEL.question: [good_level] * 3,
            LEAD.question: [
                RuntimeError("jev is down"),
                draft("lead_lag", SWAPPED),
                draft("lead_lag", LEAD_PICKS, TS_XY),
            ],
            WEEKLY.question: [no_card] * 3,
            XFAIL.question: [draft("spread", []), draft("spread", []), good_level],
        }
    )
    seen = []
    result = run_eval(
        [LEVEL, LEAD, WEEKLY, XFAIL],
        drafter,
        conn,
        runs=3,
        on_case=lambda run, case, g: seen.append((run, case.id, g.stage)),
    )
    assert seen[:4] == [
        (1, "level", None),
        (1, "lead", "shape"),
        (1, "weekly", "valid"),
        (1, "hard", "shape"),
    ]
    assert len(seen) == 12
    return result


def test_drafter_exception_fails_shape(report):
    first_lead = report.grades[0][1]
    assert first_lead == Grade(
        "lead", False, "shape", "drafter raised: RuntimeError: jev is down", False
    )


def test_stage_accuracy_mean_and_range(report):
    acc = report.stage_accuracy
    assert acc["shape"].mean == pytest.approx(8 / 9)
    assert (acc["shape"].low, acc["shape"].high) == pytest.approx((2 / 3, 1))
    assert acc["series"].mean == pytest.approx(7 / 9)
    assert (acc["series"].low, acc["series"].high) == pytest.approx((2 / 3, 1))
    assert acc["valid"].mean == pytest.approx(4 / 9)
    assert (acc["valid"].low, acc["valid"].high) == pytest.approx((1 / 3, 2 / 3))
    assert acc["renders"].mean == pytest.approx(4 / 9)
    assert report.pass_rate.mean == pytest.approx(4 / 9)
    assert (report.pass_rate.low, report.pass_rate.high) == pytest.approx((1 / 3, 2 / 3))


def test_limiting_stage_ignores_xfail_cases(report):
    assert report.failures == {"shape": 1, "series": 1, "valid": 3, "renders": 0}
    assert report.limiting_stage == "valid"


def test_case_passes_and_xfail_separation(report):
    assert report.case_passes == {"level": 3, "lead": 1, "weekly": 0, "hard": 1}
    assert report.xfail_passed == ["hard"]


def test_to_text(report):
    lines = report.to_text().splitlines()
    assert lines[0] == "level   3/3"
    assert lines[1] == (
        "lead    1/3  series: the measure that moves first: picked fred:YFOLLOW (0.55), "
        "acceptable fred:XLEAD"
    )
    assert lines[2] == "weekly  0/3  valid: no units"
    assert lines[3] == "hard    1/3  xfail  shape: chose spread (0.90), expected level"
    assert "shape    0.89  0.67-1.00" in lines
    assert "passed   0.44  0.33-0.67" in lines
    assert lines[-2] == "limiting stage: valid (3 failures)"
    assert lines[-1] == "xfail: 1 of 1 passed at least once (hard)"


def test_to_json(report):
    data = report.to_json()
    assert set(data) == {
        "runs",
        "timestamp",
        "stages",
        "passed",
        "limiting_stage",
        "failures",
        "cases",
        "xfail_passed",
        "grades",
    }
    assert data["runs"] == 3
    assert data["stages"]["valid"] == pytest.approx({"mean": 4 / 9, "min": 1 / 3, "max": 2 / 3})
    assert data["cases"][1] == {
        "id": "lead",
        "passed": 1,
        "xfail": False,
        "stage": "series",
        "reason": "the measure that moves first: picked fred:YFOLLOW (0.55), acceptable fred:XLEAD",
    }
    assert len(data["grades"]) == 3 and data["grades"][0][0] == {
        "case_id": "level",
        "passed": True,
        "stage": None,
        "reason": None,
        "xfail": False,
    }

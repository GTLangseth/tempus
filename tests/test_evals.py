import pytest

from tempus.evals import Case, EvalFileError, check_cases, known_series, load_cases

ONE_CASE = """
version: 1
cases:
  - id: gold-versus-dollar
    question: Gold versus the dollar
    shape: compare
    also: [ratio]
    roles:
      - [fred:NASDAQQGLDI]
      - [fred:DTWEXBGS, fred:DTWEXAFEGS]
"""


def load(tmp_path, text: str) -> list[Case]:
    path = tmp_path / "drafting.yaml"
    path.write_text(text)
    return load_cases(path)


def load_error(tmp_path, text: str) -> str:
    with pytest.raises(EvalFileError) as exc:
        load(tmp_path, text)
    return str(exc.value)


def case(**overrides) -> Case:
    fields = {
        "id": "c",
        "question": "q",
        "shape": "level",
        "roles": (("fred:UNRATE",),),
    }
    return Case(**(fields | overrides))


def test_load_parses_a_case(tmp_path):
    assert load(tmp_path, ONE_CASE) == [
        Case(
            id="gold-versus-dollar",
            question="Gold versus the dollar",
            shape="compare",
            roles=(("fred:NASDAQQGLDI",), ("fred:DTWEXBGS", "fred:DTWEXAFEGS")),
            also=("ratio",),
        )
    ]


def test_load_reads_xfail(tmp_path):
    text = ONE_CASE + "    xfail: no gold spot price\n"
    assert load(tmp_path, text)[0].xfail == "no gold spot price"


def test_wrong_version(tmp_path):
    text = ONE_CASE.replace("version: 1", "version: 2")
    assert load_error(tmp_path, text) == "drafting.yaml: expected `version: 1`"


def test_missing_version(tmp_path):
    text = ONE_CASE.replace("version: 1\n", "")
    assert load_error(tmp_path, text) == "drafting.yaml: expected `version: 1`"


def test_unknown_top_level_key(tmp_path):
    text = ONE_CASE.replace("version: 1", "version: 1\nbaseline: x")
    assert load_error(tmp_path, text) == "drafting.yaml: unknown key 'baseline'"


def test_unknown_case_key(tmp_path):
    text = ONE_CASE.replace("    also:", "    shapes: [ratio]\n    also:")
    assert load_error(tmp_path, text) == "gold-versus-dollar: unknown key 'shapes'"


def test_missing_field(tmp_path):
    text = ONE_CASE.replace("    question: Gold versus the dollar\n", "")
    assert load_error(tmp_path, text) == "gold-versus-dollar: missing question"


def test_missing_id_names_the_case_by_position(tmp_path):
    text = ONE_CASE.replace("  - id: gold-versus-dollar\n    question", "  - question")
    assert load_error(tmp_path, text) == "case 1: missing id"


def test_duplicate_id(tmp_path):
    text = ONE_CASE + ONE_CASE.split("cases:\n")[1]
    assert load_error(tmp_path, text) == "gold-versus-dollar: duplicate id"


def test_empty_role_list(tmp_path):
    text = ONE_CASE.replace("[fred:DTWEXBGS, fred:DTWEXAFEGS]", "[]")
    assert load_error(tmp_path, text) == "gold-versus-dollar: roles[2] is empty"


def test_known_series_reads_snapshot_and_catalog(tmp_path):
    (tmp_path / "snapshot.json").write_text(
        '{"source": "fred", "series": [{"id": "DGS10"}, {"id": "UNRATE"}]}'
    )
    (tmp_path / "catalog.toml").write_text('[[series]]\nsource = "fred"\nid = "DCOILBRENTEU"\n')
    assert known_series(tmp_path / "snapshot.json", tmp_path / "catalog.toml") == {
        "fred:DGS10",
        "fred:UNRATE",
        "fred:DCOILBRENTEU",
    }


def test_check_passes_a_good_case():
    assert check_cases([case()], {"fred:UNRATE"}) == []


def test_check_unknown_shape_suggests_one():
    assert check_cases([case(shape="leadlag")], {"fred:UNRATE"}) == [
        "c: unknown shape 'leadlag' (did you mean 'lead_lag'?)"
    ]


def test_check_unknown_shape_without_a_close_match():
    assert check_cases([case(shape="histogram")], {"fred:UNRATE"}) == [
        "c: unknown shape 'histogram'"
    ]


def test_check_also_names_unknown_shape():
    assert check_cases([case(also=("ratios",))], {"fred:UNRATE"}) == [
        "c: also names unknown shape 'ratios' (did you mean 'ratio'?)"
    ]


def test_check_also_repeats_shape():
    assert check_cases([case(also=("level",))], {"fred:UNRATE"}) == [
        "c: also repeats shape 'level'"
    ]


def test_check_role_count():
    roles = (("fred:UNRATE",), ("fred:UNRATE",))
    assert check_cases([case(roles=roles)], {"fred:UNRATE"}) == ["c: level has 1 roles, got 2"]


def test_check_unknown_series():
    assert check_cases([case(roles=(("fred:NOPE",),))], {"fred:UNRATE"}) == [
        "c: roles[1] fred:NOPE is not in the snapshot or catalog"
    ]

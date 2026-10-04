import pytest

from tempus.cards.expr import BinOp, Call, ExprError, Name, Neg, Num, Str, names, parse


def test_parses_calls_kwargs_and_arithmetic():
    node = parse("zscore(yoy(a)) - lagcorr(a, b, nlags=12) / 2")
    assert isinstance(node, BinOp) and node.op == "-"
    right = node.right
    assert right.op == "/" and right.right == Num(2)
    call = right.left
    assert call == Call("lagcorr", (Name("a"), Name("b")), (("nlags", Num(12)),))


def test_strings_negation_and_names():
    node = parse('-resample(crude, "M", agg="last")')
    assert isinstance(node, Neg)
    assert node.operand.args[1] == Str("M")
    assert names(parse("lagcorr(yoy(a), b) + c * 2")) == {"a", "b", "c"}


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

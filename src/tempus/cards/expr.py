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
            if any(isinstance(a, ast.Starred) for a in args) or any(
                k.arg is None for k in keywords
            ):
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

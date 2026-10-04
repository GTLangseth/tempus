"""Static checks: everything that can be known without the database."""

from datetime import date
from graphlib import CycleError, TopologicalSorter

from tempus.cards.errors import CardError, did_you_mean
from tempus.cards.expr import BinOp, Call, ExprError, Name, Neg, Node, Num, Str, names, parse
from tempus.cards.functions import CATALOG, ArgError, match_args
from tempus.cards.panels import PANELS, accepts_text, panel_refs
from tempus.cards.spec import Card, ConceptInput

_KIND_ACCEPTS = {
    "series": {"series"},
    "set": {"set"},
    "int": {"int"},
    "number": {"int", "num"},
    "str": {"str"},
    "date": {"str"},
}


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
        errors.append(CardError(card_id, f"formulas.{name}", f"{card.formulas[name]} — {message}"))

    broken = set(failures)
    for name, node in nodes.items():
        for ref in sorted(names(node) - known):
            errors.append(
                CardError(
                    card_id,
                    f"formulas.{name}",
                    f"{card.formulas[name]} — unknown name '{ref}'.",
                    hint=did_you_mean(ref, known),
                )
            )
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
        return [
            CardError(
                card_id,
                path,
                f"unknown panel type '{panel.type}'.",
                hint=did_you_mean(panel.type, PANELS),
            )
        ]
    refs = panel_refs(panel, ptype)
    if not refs:
        return [CardError(card_id, path, f"'{panel.type}' needs '{ptype.field}'")]
    if len(refs) > 1 and not ptype.multiple:
        return [
            CardError(
                card_id,
                path,
                f"'{panel.type}' shows only one result; '{ptype.field}' lists {len(refs)}",
            )
        ]
    errors = []
    for ref in refs:
        if ref not in known:
            errors.append(
                CardError(card_id, path, f"unknown name '{ref}'.", hint=did_you_mean(ref, known))
            )
        elif ref in broken:
            continue
        elif types.get(ref) not in ptype.accepts:
            errors.append(
                CardError(
                    card_id,
                    path,
                    f"type '{panel.type}' needs "
                    f"{accepts_text(ptype)}; '{ref}' is a "
                    f"{types.get(ref)}",
                )
            )
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

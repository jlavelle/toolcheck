"""Compile a stateless Rule into an ``(env: Env) -> bool`` callable.

Cross-call rules are excluded — callers filter with is_stateless() before
compiling. Cross-call evaluation lives in toolcheck.crosscall.

An ``Env`` bundles the tool_input with ambient session facts (currently the
current context length). Field predicates read tool_input; the context-length
predicate reads the ambient value.

A predicate's ``field`` is resolved by ``resolve_field``: an @-prefixed name
is a derived field (a pure extractor in ``_DERIVED``); any other name is a raw
tool_input key.
"""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass
from typing import Callable, Mapping

from toolcheck import bash as _bash
from toolcheck.schema import (
    AllPredicate,
    AnyPredicate,
    ContextLengthPredicate,
    CrossCallPredicate,
    InPredicate,
    LengthPredicate,
    NotPredicate,
    Predicate,
    PresencePredicate,
    RegexPredicate,
    Rule,
)


@dataclass(frozen=True)
class Env:
    """Evaluation environment: tool_input plus ambient session facts."""

    tool_input: Mapping[str, object]
    context_length: int | None = None


Matcher = Callable[["Env"], bool]

FieldExtractor = Callable[[Mapping[str, object]], object | None]


def _command(inp: Mapping[str, object]) -> str:
    return str(inp.get("command") or "")


_DERIVED: dict[str, FieldExtractor] = {
    "@last_pipe_command": lambda inp: _bash.last_pipe_command(_command(inp)),
}


def known_derived_fields() -> frozenset[str]:
    """The @-prefixed field names the compiler can resolve."""
    return frozenset(_DERIVED)


def resolve_field(field: str, inp: Mapping[str, object]) -> object | None:
    """A field's value: derived extractor for @-names, raw key otherwise.

    Raises ValueError for an unknown derived field.
    """
    if field.startswith("@"):
        extractor = _DERIVED.get(field)
        if extractor is None:
            raise ValueError(f"unknown derived field: {field!r}")
        return extractor(inp)
    return inp.get(field)


_OPS: dict[str, Callable[[int, int], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


def _compile_predicate(pred: Predicate) -> Matcher:
    match pred:
        case RegexPredicate(field=f, pattern=p):
            compiled = re.compile(p)

            def _regex(
                env: Env,
                _c: re.Pattern[str] = compiled,
                _f: str = f,
            ) -> bool:
                val = resolve_field(_f, env.tool_input) or ""
                return bool(_c.search(str(val)))

            return _regex

        case PresencePredicate(field=f, present=want):

            def _present(env: Env, _f: str = f, _want: bool = want) -> bool:
                return (resolve_field(_f, env.tool_input) is not None) == _want

            return _present

        case InPredicate(field=f, values=vs):
            members = frozenset(vs)

            def _in(
                env: Env,
                _f: str = f,
                _members: frozenset[str] = members,
            ) -> bool:
                val = resolve_field(_f, env.tool_input)
                return val is not None and str(val) in _members

            return _in

        case LengthPredicate(field=f, op=op_, value=v):
            cmp = _OPS[op_]

            def _length(
                env: Env,
                _f: str = f,
                _v: int = v,
                _cmp: Callable[[int, int], bool] = cmp,
            ) -> bool:
                val = resolve_field(_f, env.tool_input)
                n = len(str(val)) if val is not None else 0
                return _cmp(n, _v)

            return _length

        case ContextLengthPredicate(op=op_, value=v):
            cmp = _OPS[op_]

            def _context_length(
                env: Env,
                _v: int = v,
                _cmp: Callable[[int, int], bool] = cmp,
            ) -> bool:
                # Unknown context length (no --claude, or first call) holds False.
                return env.context_length is not None and _cmp(env.context_length, _v)

            return _context_length

        case NotPredicate(operand=op):
            inner = _compile_predicate(op)
            return lambda env: not inner(env)

        case AllPredicate(operands=ops):
            fns = [_compile_predicate(o) for o in ops]

            def _all(env: Env, _fns: list[Matcher] = fns) -> bool:
                return all(fn(env) for fn in _fns)

            return _all

        case AnyPredicate(operands=ops):
            fns = [_compile_predicate(o) for o in ops]

            def _any(env: Env, _fns: list[Matcher] = fns) -> bool:
                return any(fn(env) for fn in _fns)

            return _any

        case CrossCallPredicate():
            raise NotImplementedError(
                "CrossCallPredicate requires session state; "
                "filter with is_stateless() before compiling"
            )


def compile_rule(rule: Rule) -> Matcher:
    """Compile a stateless rule to a Python callable over an Env."""
    return _compile_predicate(rule.match)

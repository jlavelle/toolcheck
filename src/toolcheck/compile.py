"""Compile a stateless Rule into an ``(env: Env) -> bool`` callable.

Cross-call rules are excluded — callers filter with is_stateless() before
compiling. Cross-call evaluation lives in toolcheck.crosscall.

An ``Env`` bundles the tool_input with ambient session facts: the current
context length, the agent's working directory and its state before this call.
Field predicates read tool_input; the context-length, cwd and prior-call
predicates read the ambient values.

A predicate's ``field`` is resolved by ``resolve_field``: an @-prefixed name
is a derived field, either a builtin pure extractor or a view the
catalogue declares for the calling tool; any other name is a raw tool_input
key. A view yields ``Values``, and a field predicate on them holds when it
holds for one of them.
"""

from __future__ import annotations

import json
import operator
import re
from collections.abc import Callable, Mapping, Sized
from dataclasses import dataclass, field

from toolcheck import bash as _bash
from toolcheck.schema import (
    AllPredicate,
    AnyPredicate,
    AstPredicate,
    ContextLengthPredicate,
    CrossCallPredicate,
    CwdPredicate,
    InPredicate,
    LengthPredicate,
    NotPredicate,
    Predicate,
    PresencePredicate,
    PriorCallPredicate,
    RegexPredicate,
    Rule,
    View,
    Views,
)
from toolcheck.state import SessionState


@dataclass(frozen=True)
class Env:
    """Evaluation environment: tool_input plus ambient session facts."""

    tool_input: Mapping[str, object]
    context_length: int | None = None
    state: SessionState = field(default_factory=SessionState)
    cwd: str | None = None
    tool: str = ""
    views: Views = field(default_factory=dict[str, Mapping[str, View]])


class ViewCycleError(ValueError):
    """A view that reads itself, directly or through other views."""


@dataclass(frozen=True)
class Values:
    """The strings a view yields."""

    items: tuple[str, ...]


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


def resolve_field(field: str, env: Env) -> object | None:
    """A field's value: derived extractor or view for @-names, raw key otherwise.

    A view the calling tool lacks resolves to None. Raises ValueError for an
    @-name that is neither a builtin nor a view of some tool.
    """
    return _resolve(field, env, frozenset())


def _resolve(field: str, env: Env, seen: frozenset[str]) -> object | None:
    if not field.startswith("@"):
        return env.tool_input.get(field)
    if (extractor := _DERIVED.get(field)) is not None:
        return extractor(env.tool_input)
    view = env.views.get(env.tool, {}).get(field)
    if view is None:
        if any(field in named for named in env.views.values()):
            return None
        raise ValueError(f"unknown derived field: {field!r}")
    if field in seen:
        raise ViewCycleError(f"view {field!r} reads itself")
    source = _resolve(view.field, env, seen | {field})
    texts = (
        source.items
        if isinstance(source, Values)
        else (() if source is None else (field_text(source),))
    )
    if view.query is None:
        return Values(texts)
    from toolcheck import syntax

    return Values(tuple(s for t in texts for s in syntax.extract(view.query, t)))


def _some(value: object | None, test: Callable[[object | None], bool]) -> bool:
    """``test`` on a field's value, or on some item when the value is a view's Values."""
    if isinstance(value, Values):
        return any(test(item) for item in value.items)
    return test(value)


def field_text(value: object) -> str:
    """JSON text for non-strings, so rules match what their author writes in TOML."""
    match value:
        case None:
            return ""
        case str():
            return value
        case _:
            return json.dumps(value, ensure_ascii=False)


def field_length(value: object) -> int:
    if isinstance(value, Sized):
        return len(value)
    return len(field_text(value))


_OPS: dict[str, Callable[[int, int], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


def _compile_ast(pred: AstPredicate) -> Matcher:
    """Raises ValueError for an unknown language or a rule ast-grep rejects."""
    from toolcheck import syntax

    syntax.check(pred.query)
    return lambda env: _some(
        resolve_field(pred.field, env),
        lambda v: syntax.matches(pred.query, field_text(v)),
    )


def _compile_predicate(pred: Predicate) -> Matcher:
    match pred:
        case RegexPredicate(field=f, pattern=p):
            compiled = re.compile(p)

            def _regex(
                env: Env,
                _c: re.Pattern[str] = compiled,
                _f: str = f,
            ) -> bool:
                return _some(
                    resolve_field(_f, env), lambda v: bool(_c.search(field_text(v)))
                )

            return _regex

        case PresencePredicate(field=f, present=want):

            def _present(env: Env, _f: str = f, _want: bool = want) -> bool:
                value = resolve_field(_f, env)
                present = (
                    bool(value.items)
                    if isinstance(value, Values)
                    else value is not None
                )
                return present == _want

            return _present

        case InPredicate(field=f, values=vs):
            members = frozenset(vs)

            def _in(
                env: Env,
                _f: str = f,
                _members: frozenset[str] = members,
            ) -> bool:
                return _some(
                    resolve_field(_f, env),
                    lambda v: v is not None and field_text(v) in _members,
                )

            return _in

        case LengthPredicate(field=f, op=op_, value=v):
            cmp = _OPS[op_]

            def _length(
                env: Env,
                _f: str = f,
                _v: int = v,
                _cmp: Callable[[int, int], bool] = cmp,
            ) -> bool:
                return _some(
                    resolve_field(_f, env), lambda v: _cmp(field_length(v), _v)
                )

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

        case CwdPredicate(pattern=p):
            compiled = re.compile(p)
            return lambda env: env.cwd is not None and bool(compiled.search(env.cwd))

        case AstPredicate():
            return _compile_ast(pred)

        case PriorCallPredicate(tool=t, field=f, value=v):
            return lambda env: v in env.state.history(t, f)

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

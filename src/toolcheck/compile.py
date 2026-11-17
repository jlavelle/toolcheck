"""Compile a stateless Rule into an ``(env: Env) -> bool`` callable.

Cross-call rules are excluded — callers filter with is_stateless() before
compiling. Cross-call evaluation lives in toolcheck.crosscall.

An ``Env`` bundles the tool_input with ambient session facts: the current
context length, the agent's working directory and its state before this call.
Field predicates read tool_input; the context-length, cwd and prior-call
predicates read the ambient values.

A predicate's ``field`` is resolved by ``resolve_field``: an @-prefixed name
is a derived field (a pure extractor in ``_DERIVED``); any other name is a raw
tool_input key.
"""

from __future__ import annotations

import functools
import json
import operator
import re
from collections.abc import Callable, Mapping, Sized
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

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
)
from toolcheck.state import SessionState

if TYPE_CHECKING:
    from ast_grep_py import Config, SgNode


@dataclass(frozen=True)
class Env:
    """Evaluation environment: tool_input plus ambient session facts."""

    tool_input: Mapping[str, object]
    context_length: int | None = None
    state: SessionState = field(default_factory=SessionState)
    cwd: str | None = None


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


@functools.lru_cache(maxsize=32)
def _parse(text: str, language: str) -> SgNode:
    """The syntax tree of ``text``, shared by every ast rule that reads the same field."""
    from ast_grep_py import SgRoot

    return SgRoot(text, language).root()


def _ast_config(pred: AstPredicate) -> Config:
    config: dict[str, object] = {"rule": pred.rule}
    if pred.constraints is not None:
        config["constraints"] = pred.constraints
    if pred.utils is not None:
        config["utils"] = pred.utils
    return cast("Config", config)


def _compile_ast(pred: AstPredicate) -> Matcher:
    """Raises ValueError for an unknown language or a rule ast-grep rejects."""
    config = _ast_config(pred)
    try:
        _parse("", pred.language).find(config)
    except BaseException as e:
        # ast-grep raises Exception for a bad rule and panics, which pyo3
        # surfaces as a BaseException, for an unknown language.
        if not isinstance(e, Exception) and type(e).__name__ != "PanicException":
            raise
        raise ValueError(f"ast rule for {pred.language!r} does not compile: {e}") from e

    def _ast(env: Env) -> bool:
        text = field_text(resolve_field(pred.field, env.tool_input))
        return _parse(text, pred.language).find(config) is not None

    return _ast


def _compile_predicate(pred: Predicate) -> Matcher:
    match pred:
        case RegexPredicate(field=f, pattern=p):
            compiled = re.compile(p)

            def _regex(
                env: Env,
                _c: re.Pattern[str] = compiled,
                _f: str = f,
            ) -> bool:
                return bool(_c.search(field_text(resolve_field(_f, env.tool_input))))

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
                return val is not None and field_text(val) in _members

            return _in

        case LengthPredicate(field=f, op=op_, value=v):
            cmp = _OPS[op_]

            def _length(
                env: Env,
                _f: str = f,
                _v: int = v,
                _cmp: Callable[[int, int], bool] = cmp,
            ) -> bool:
                return _cmp(field_length(resolve_field(_f, env.tool_input)), _v)

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

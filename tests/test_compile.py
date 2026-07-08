"""Compiler laws: boolean algebra over the predicate tree."""

from __future__ import annotations

import pytest
from hypothesis import given

from strategies import bash_predicates, bash_tool_inputs, context_lengths, make_rule
from toolcheck.compile import Env, compile_rule
from toolcheck.schema import (
    AllPredicate,
    AnyPredicate,
    ContextLengthPredicate,
    InPredicate,
    NotPredicate,
    Predicate,
    RegexPredicate,
    Rule,
)


def _run(p: Predicate, inp: dict[str, object], ctx: int | None = None) -> bool:
    return compile_rule(make_rule(p))(Env(inp, ctx))


@given(bash_predicates(), bash_tool_inputs(), context_lengths())
def test_double_negation(p: Predicate, inp: dict[str, object], ctx: int | None):
    assert _run(NotPredicate(NotPredicate(p)), inp, ctx) == _run(p, inp, ctx)


@given(bash_predicates(), bash_tool_inputs(), context_lengths())
def test_all_singleton_is_identity(
    p: Predicate, inp: dict[str, object], ctx: int | None
):
    assert _run(AllPredicate((p,)), inp, ctx) == _run(p, inp, ctx)


@given(bash_predicates(), bash_tool_inputs(), context_lengths())
def test_any_singleton_is_identity(
    p: Predicate, inp: dict[str, object], ctx: int | None
):
    assert _run(AnyPredicate((p,)), inp, ctx) == _run(p, inp, ctx)


@given(bash_predicates(), bash_predicates(), bash_tool_inputs(), context_lengths())
def test_de_morgan(a: Predicate, b: Predicate, inp: dict[str, object], ctx: int | None):
    lhs = _run(NotPredicate(AllPredicate((a, b))), inp, ctx)
    rhs = _run(AnyPredicate((NotPredicate(a), NotPredicate(b))), inp, ctx)
    assert lhs == rhs


@given(bash_tool_inputs())
def test_empty_all_is_true_empty_any_is_false(inp: dict[str, object]):
    assert _run(AllPredicate(()), inp) is True
    assert _run(AnyPredicate(()), inp) is False


def test_unknown_derived_field_raises():
    rule = make_rule(InPredicate(field="@nope", values=("x",)))
    with pytest.raises(ValueError):
        compile_rule(rule)(Env({"command": "x"}))


def test_regex_missing_field_is_false():
    rule = make_rule(RegexPredicate(field="command", pattern="x"))
    assert compile_rule(rule)(Env({})) is False


@pytest.mark.parametrize(
    ("op", "value", "ctx", "expected"),
    [
        (">", 100, 150, True),
        (">", 100, 100, False),
        (">", 100, None, False),  # unknown holds False
        ("<=", 100, 100, True),
        ("==", 100, 100, True),
        ("!=", 100, 100, False),
        (">", 100, 50, False),
    ],
)
def test_context_length_comparison(
    op: str, value: int, ctx: int | None, expected: bool
):
    rule = make_rule(ContextLengthPredicate(op=op, value=value))  # type: ignore[arg-type]
    assert compile_rule(rule)(Env({}, ctx)) is expected


def test_context_length_composes_with_field_predicate():
    rule = make_rule(
        AllPredicate(
            (
                ContextLengthPredicate(op=">", value=100_000),
                RegexPredicate(field="command", pattern="foo"),
            )
        )
    )
    matcher = compile_rule(rule)
    assert matcher(Env({"command": "foo"}, 120_000)) is True
    assert matcher(Env({"command": "foo"}, 50_000)) is False  # under threshold
    assert matcher(Env({"command": "bar"}, 120_000)) is False  # command mismatch
    assert matcher(Env({"command": "foo"}, None)) is False  # unknown context


def _truncate_rule() -> Rule:
    return Rule(
        name="truncate-without-tee",
        tool="Bash",
        message="msg",
        severity="warning",
        dedup=True,
        sample_field="command",
        match=AllPredicate(
            (
                InPredicate(field="@last_pipe_command", values=("head", "tail")),
                NotPredicate(RegexPredicate(field="command", pattern=r"(^|\s)tee\s")),
            )
        ),
    )


def _grep_unscoped_rule() -> Rule:
    from toolcheck.schema import PresencePredicate

    return Rule(
        name="grep-unscoped",
        tool="Grep",
        message="msg",
        severity="warning",
        dedup=True,
        sample_field="pattern",
        match=AllPredicate(
            (
                PresencePredicate(field="path", present=False),
                PresencePredicate(field="glob", present=False),
            )
        ),
    )


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ("rg foo | head", True),
        ("cat x | tail -n 5", True),
        ("rg foo | wc -l", False),
        ("test -f x || head", False),  # || is a separator, not a pipe
        ("rg foo | tee out | head", False),  # tee present
        ("rg foo | head | tee out", False),  # last segment is tee, not head/tail
    ],
)
def test_truncate_without_tee_parity(cmd: str, expected: bool):
    assert compile_rule(_truncate_rule())(Env({"command": cmd})) is expected


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        ({"pattern": "foo"}, True),
        ({"pattern": "foo", "path": "src"}, False),
        ({"pattern": "foo", "glob": "*.py"}, False),
        ({"pattern": "foo", "path": "src", "glob": "*.py"}, False),
    ],
)
def test_grep_unscoped_parity(inp: dict[str, object], expected: bool):
    assert compile_rule(_grep_unscoped_rule())(Env(inp)) is expected

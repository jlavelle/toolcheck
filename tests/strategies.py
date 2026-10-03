"""Shared hypothesis strategies for toolcheck tests."""

from __future__ import annotations

from hypothesis import strategies as st

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

# Surrogate/control-safe for JSON and TOML roundtrips.
SAFE_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=0,
    max_size=40,
)
NONEMPTY_TEXT = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=24,
)

# Fields the Bash compiler can resolve: raw tool_input keys plus derived fields.
BASH_FIELDS = ["command", "file_path", "pattern"]
DERIVED_FIELDS = ["@last_pipe_command"]
SAFE_PATTERNS = ["foo", r"\brg\b", r"\|", "xargs", r"^\s*find", "head", "tail"]
LENGTH_OPS = [">", ">=", "<", "<=", "==", "!="]


def _leaf_predicates() -> st.SearchStrategy[Predicate]:
    regex = st.builds(RegexPredicate, field=NONEMPTY_TEXT, pattern=SAFE_TEXT)
    presence = st.builds(PresencePredicate, field=NONEMPTY_TEXT, present=st.booleans())
    value = st.builds(
        InPredicate,
        field=NONEMPTY_TEXT,
        values=st.lists(NONEMPTY_TEXT, max_size=4).map(tuple),
    )
    length = st.builds(
        LengthPredicate,
        field=NONEMPTY_TEXT,
        op=st.sampled_from(LENGTH_OPS),
        value=st.integers(min_value=-1000, max_value=100000),
    )
    context_length = st.builds(
        ContextLengthPredicate,
        op=st.sampled_from(LENGTH_OPS),
        value=st.integers(min_value=0, max_value=1_000_000),
    )
    cross = st.builds(
        CrossCallPredicate,
        kind=st.sampled_from(
            ["duplicate_field_within_session", "adjacent_near_identical"]
        ),
        field=NONEMPTY_TEXT,
    )
    return st.one_of(regex, presence, value, length, context_length, cross)


def predicates() -> st.SearchStrategy[Predicate]:
    """Arbitrary predicate trees (parse round-trip; not necessarily compilable)."""
    return st.recursive(
        _leaf_predicates(),
        lambda children: st.one_of(
            st.builds(NotPredicate, operand=children),
            st.builds(AllPredicate, operands=st.lists(children, max_size=4).map(tuple)),
            st.builds(AnyPredicate, operands=st.lists(children, max_size=4).map(tuple)),
        ),
        max_leaves=8,
    )


def _bash_leaves() -> st.SearchStrategy[Predicate]:
    regex = st.builds(
        RegexPredicate,
        field=st.sampled_from(BASH_FIELDS),
        pattern=st.sampled_from(SAFE_PATTERNS),
    )
    presence = st.builds(
        PresencePredicate,
        field=st.sampled_from(BASH_FIELDS + DERIVED_FIELDS),
        present=st.booleans(),
    )
    value = st.builds(
        InPredicate,
        field=st.sampled_from(BASH_FIELDS + DERIVED_FIELDS),
        values=st.lists(st.sampled_from(["head", "tail", "foo"]), max_size=3).map(
            tuple
        ),
    )
    length = st.builds(
        LengthPredicate,
        field=st.sampled_from(BASH_FIELDS),
        op=st.sampled_from(LENGTH_OPS),
        value=st.integers(min_value=0, max_value=20),
    )
    context_length = st.builds(
        ContextLengthPredicate,
        op=st.sampled_from(LENGTH_OPS),
        value=st.integers(min_value=0, max_value=200_000),
    )
    return st.one_of(regex, presence, value, length, context_length)


def bash_predicates() -> st.SearchStrategy[Predicate]:
    """Predicate trees that the Bash compiler can compile and run."""
    return st.recursive(
        _bash_leaves(),
        lambda children: st.one_of(
            st.builds(NotPredicate, operand=children),
            st.builds(AllPredicate, operands=st.lists(children, max_size=3).map(tuple)),
            st.builds(AnyPredicate, operands=st.lists(children, max_size=3).map(tuple)),
        ),
        max_leaves=6,
    )


def context_lengths() -> st.SearchStrategy[int | None]:
    """Ambient context lengths, including the unknown (None) case."""
    return st.one_of(st.none(), st.integers(min_value=0, max_value=1_000_000))


def bash_tool_inputs() -> st.SearchStrategy[dict[str, object]]:
    return st.fixed_dictionaries(
        {
            "command": st.text(max_size=60),
            "file_path": st.text(max_size=30),
            "pattern": st.text(max_size=30),
        }
    )


def make_rule(
    pred: Predicate,
    tool: str = "Bash",
    name: str = "t",
    dedup: bool = True,
    every_calls: int | None = None,
    sample_field: str | None = "command",
) -> Rule:
    return Rule(
        name=name,
        tool=tool,
        message="m",
        severity="warning",
        dedup=dedup,
        every_calls=every_calls,
        sample_field=sample_field,
        match=pred,
    )

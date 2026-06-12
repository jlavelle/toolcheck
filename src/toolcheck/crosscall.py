"""Cross-call predicate evaluation against session observation history.

Two kinds:
  duplicate_field_within_session — field value seen earlier this session.
  adjacent_near_identical — field value near-identical to the last observation.

Fire per-occurrence; exempt from the per-rule dedup setting.
"""

from __future__ import annotations

import difflib
from typing import Mapping

from toolcheck.schema import CrossCallPredicate
from toolcheck.state import SessionState

_NEAR_IDENTICAL_RATIO = 0.9


def _normalise(s: str) -> str:
    return " ".join(s.split())


def near_identical(a: str, b: str) -> bool:
    """True when two strings are equal modulo whitespace, or highly similar."""
    na, nb = _normalise(a), _normalise(b)
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= _NEAR_IDENTICAL_RATIO


def field_value(
    pred: CrossCallPredicate, tool_input: Mapping[str, object]
) -> str | None:
    """The current value of the tracked field, or None when absent."""
    v = tool_input.get(pred.field)
    return None if v is None else str(v)


def evaluate(
    pred: CrossCallPredicate,
    tool: str,
    tool_input: Mapping[str, object],
    state: SessionState,
) -> bool:
    """True when the condition holds against prior observations only.

    The current value is recorded by the caller *after* evaluation.
    """
    value = field_value(pred, tool_input)
    if value is None:
        return False
    history = state.history(tool, pred.field)

    match pred.kind:
        case "duplicate_field_within_session":
            return value in history
        case "adjacent_near_identical":
            return bool(history) and near_identical(value, history[-1])

"""Cross-call evaluation and near-identical similarity."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from toolcheck.crosscall import evaluate, near_identical
from toolcheck.schema import CrossCallPredicate
from toolcheck.state import SessionState

_DUP = CrossCallPredicate(kind="duplicate_field_within_session", field="file_path")
_ADJ = CrossCallPredicate(kind="adjacent_near_identical", field="content")


@given(st.text())
def test_near_identical_reflexive(s: str):
    assert near_identical(s, s)


@given(st.text(max_size=40), st.text(max_size=40))
def test_near_identical_symmetric(a: str, b: str):
    assert near_identical(a, b) == near_identical(b, a)


def test_near_identical_whitespace_insensitive():
    assert near_identical("a  b\n c", "a b c")
    assert not near_identical("abc", "xyz")


def test_duplicate_fires_only_after_first_sighting():
    inp = {"file_path": "/x.py"}
    empty = SessionState()
    assert evaluate(_DUP, "Read", inp, empty) is False
    seen = empty.with_observation("Read", "file_path", "/x.py")
    assert evaluate(_DUP, "Read", inp, seen) is True


def test_duplicate_missing_field_is_false():
    assert evaluate(_DUP, "Read", {}, SessionState()) is False


def test_adjacent_compares_against_last_only():
    s = (
        SessionState()
        .with_observation("Edit", "content", "hello world foo")
        .with_observation("Edit", "content", "totally different text here")
    )
    # near to the *last* entry
    assert evaluate(_ADJ, "Edit", {"content": "totally different text heir"}, s)
    # near to an *earlier* entry but not the last → no fire
    assert not evaluate(_ADJ, "Edit", {"content": "hello world foo"}, s)

"""Session state: persistence round-trip and ledger/history laws."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from strategies import NONEMPTY_TEXT, SAFE_TEXT
from toolcheck.state import SessionState, load_state, save_state, state_path

_ROUNDTRIP_FILE = Path(tempfile.mkdtemp(prefix="toolcheck-state-")) / "s.json"

_session_states = st.builds(
    SessionState,
    fired=st.dictionaries(NONEMPTY_TEXT, SAFE_TEXT, max_size=6),
    seen=st.dictionaries(
        NONEMPTY_TEXT, st.lists(SAFE_TEXT, max_size=6).map(tuple), max_size=6
    ),
)


@given(_session_states)
def test_save_load_roundtrip(state: SessionState):
    save_state(_ROUNDTRIP_FILE, state)
    assert load_state(_ROUNDTRIP_FILE) == state


def test_load_missing_is_empty(tmp_path: Path):
    assert load_state(tmp_path / "absent.json") == SessionState()


def test_load_garbage_is_empty(tmp_path: Path):
    p = tmp_path / "g.json"
    p.write_text("not json{", encoding="utf-8")
    assert load_state(p) == SessionState()


@given(NONEMPTY_TEXT, SAFE_TEXT, SAFE_TEXT)
def test_fired_is_idempotent(name: str, t1: str, t2: str):
    once = SessionState().with_fired(name, t1)
    twice = once.with_fired(name, t2)
    assert once.has_fired(name)
    assert twice.has_fired(name)
    assert twice.fired[name] == t2


def test_with_fired_does_not_mutate_original():
    base = SessionState()
    base.with_fired("r", "t")
    assert not base.has_fired("r")


@given(st.lists(SAFE_TEXT, max_size=8))
def test_observation_history_preserves_order(values: list[str]):
    state = SessionState()
    for v in values:
        state = state.with_observation("Read", "file_path", v)
    assert state.history("Read", "file_path") == tuple(values)


def test_history_keys_are_tool_field_scoped():
    s = SessionState().with_observation("Read", "file_path", "a")
    assert s.history("Read", "file_path") == ("a",)
    assert s.history("Write", "file_path") == ()
    assert s.history("Read", "other") == ()


def test_state_path_sanitises_session_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg")
    p = state_path("../../etc/passwd")
    assert ".." not in p.name
    assert p.suffix == ".json"

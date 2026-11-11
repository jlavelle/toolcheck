"""Session state: persistence round-trip and ledger/history laws."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from strategies import NONEMPTY_TEXT, SAFE_TEXT

# Ids as Claude Code makes them: no path separators or dots, which state_path rewrites.
SAFE_ID = st.text(
    alphabet=st.characters(categories=["L", "N"], include_characters="-_"),
    min_size=1,
    max_size=12,
)
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
    p = state_path("../../etc/passwd", "claude-code")
    assert ".." not in p.name
    assert p.suffix == ".json"


def test_state_path_scopes_by_agent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg")
    claude = state_path("same-session", "claude-code")
    pi = state_path("same-session", "pi")
    assert claude.parent.name == "toolcheck"
    assert "claude-code" in claude.parts
    assert "pi" in pi.parts
    assert claude != pi


def test_calls_and_fired_call_roundtrip():
    state = SessionState().with_call().with_call().with_fired_call("r", 2)
    save_state(_ROUNDTRIP_FILE, state)
    loaded = load_state(_ROUNDTRIP_FILE)
    assert loaded.calls == 2
    assert loaded.last_fired_call("r") == 2
    assert loaded == state


def test_ledger_transforms_preserve_calls_and_fired_call():
    base = SessionState(calls=7, fired_call={"r": 3})
    assert base.with_fired("x", "t").calls == 7
    assert base.with_fired("x", "t").last_fired_call("r") == 3
    assert base.with_observation("Read", "file_path", "a").calls == 7
    assert base.with_observation("Read", "file_path", "a").last_fired_call("r") == 3
    assert base.with_call().calls == 8
    assert base.with_call().last_fired_call("r") == 3


def test_state_path_gives_each_subagent_its_own_file(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg")
    main = state_path("session", "claude-code")
    first = state_path("session", "claude-code", "agent-1")
    second = state_path("session", "claude-code", "agent-2")
    assert len({main, first, second}) == 3


@given(SAFE_ID, SAFE_ID, st.one_of(st.none(), SAFE_ID), st.one_of(st.none(), SAFE_ID))
def test_state_path_never_shares_a_file(
    session_a: str, session_b: str, agent_a: str | None, agent_b: str | None
):
    a = state_path(session_a, "claude-code", agent_a)
    b = state_path(session_b, "claude-code", agent_b)
    assert (a == b) == ((session_a, agent_a) == (session_b, agent_b))


def test_empty_agent_id_is_the_main_agent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg")
    assert state_path("session", "claude-code", "") == state_path(
        "session", "claude-code"
    )


def test_state_path_sanitises_agent_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg")
    p = state_path("session", "claude-code", "../../etc")
    assert ".." not in p.name
    assert p.parent.parent == state_path("session", "claude-code").parent


def test_without_deliveries_keeps_observations_and_calls():
    state = (
        SessionState()
        .with_call()
        .with_fired("r", "t")
        .with_fired_call("r", 1)
        .with_observation("Read", "file_path", "a")
    )
    cleared = state.without_deliveries()
    assert not cleared.has_fired("r")
    assert cleared.last_fired_call("r") is None
    assert cleared.history("Read", "file_path") == ("a",)
    assert cleared.calls == 1

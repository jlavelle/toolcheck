"""Per-session state: dedup ledger + cross-call observations.

On-disk: { "fired": { rule_name: iso_timestamp }, "seen": { "<tool>\\x1f<field>": [value, ...] }, "calls": n, "fired_call": { rule_name: call_index } }

SessionState is immutable; transforms return new values. I/O is best-effort:
read failure yields empty state, write failure is swallowed. Never raises on I/O.

Growth is unbounded for now: ``seen`` histories append per observation and
session files are never reaped. Acceptable at session scale; revisit if a
cross-call kind ever needs more than membership + last value.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

_KEY_SEP = "\x1f"  # cannot appear in tool names or fields


@dataclass(frozen=True)
class SessionState:
    fired: dict[str, str] = field(default_factory=dict[str, str])
    seen: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    calls: int = 0
    fired_call: dict[str, int] = field(default_factory=dict[str, int])

    def has_fired(self, rule_name: str) -> bool:
        return rule_name in self.fired

    def with_fired(self, rule_name: str, timestamp: str) -> SessionState:
        return SessionState(
            fired={**self.fired, rule_name: timestamp},
            seen=self.seen,
            calls=self.calls,
            fired_call=self.fired_call,
        )

    def with_call(self) -> SessionState:
        return SessionState(
            fired=self.fired,
            seen=self.seen,
            calls=self.calls + 1,
            fired_call=self.fired_call,
        )

    def last_fired_call(self, rule_name: str) -> int | None:
        return self.fired_call.get(rule_name)

    def with_fired_call(self, rule_name: str, call: int) -> SessionState:
        return SessionState(
            fired=self.fired,
            seen=self.seen,
            calls=self.calls,
            fired_call={**self.fired_call, rule_name: call},
        )

    def history(self, tool: str, field_name: str) -> tuple[str, ...]:
        return self.seen.get(_seen_key(tool, field_name), ())

    def with_observation(self, tool: str, field_name: str, value: str) -> SessionState:
        key = _seen_key(tool, field_name)
        prior = self.seen.get(key, ())
        return SessionState(
            fired=self.fired,
            seen={**self.seen, key: (*prior, value)},
            calls=self.calls,
            fired_call=self.fired_call,
        )


def _seen_key(tool: str, field_name: str) -> str:
    return f"{tool}{_KEY_SEP}{field_name}"


def state_path(session_id: str, agent: str) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    # session_id is untrusted — sanitise path-traversal characters.
    safe = session_id.replace("/", "_").replace("\\", "_").replace("..", "_")
    return base / agent / "toolcheck" / f"{safe}.json"


def _as_dict(x: object) -> dict[str, object] | None:
    return cast("dict[str, object]", x) if isinstance(x, dict) else None


def load_state(path: Path) -> SessionState:
    """Read from disk; empty state on any failure."""
    try:
        with path.open("r", encoding="utf-8") as f:
            data = cast("object", json.load(f))
    except (FileNotFoundError, ValueError, OSError):
        return SessionState()
    d = _as_dict(data)
    if d is None:
        return SessionState()

    fired_raw = _as_dict(d.get("fired")) or {}
    fired = {k: v for k, v in fired_raw.items() if isinstance(v, str)}

    calls_raw = d.get("calls")
    calls = (
        calls_raw
        if isinstance(calls_raw, int) and not isinstance(calls_raw, bool)
        else 0
    )
    calls = max(calls, 0)

    fired_call_raw = _as_dict(d.get("fired_call")) or {}
    fired_call = {
        k: v
        for k, v in fired_call_raw.items()
        if isinstance(v, int) and not isinstance(v, bool)
    }

    seen_raw = _as_dict(d.get("seen")) or {}
    seen: dict[str, tuple[str, ...]] = {}
    for k, v in seen_raw.items():
        if isinstance(v, list):
            seen[k] = tuple(
                item for item in cast("list[object]", v) if isinstance(item, str)
            )
    return SessionState(fired=fired, seen=seen, calls=calls, fired_call=fired_call)


def save_state(path: Path, state: SessionState) -> None:
    """Atomic write; swallows I/O errors."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "fired": state.fired,
            "seen": {k: list(v) for k, v in state.seen.items()},
            "calls": state.calls,
            "fired_call": state.fired_call,
        }
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except OSError:
        pass

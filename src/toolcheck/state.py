"""Per-session state: dedup ledger + cross-call observations.

On-disk: { "fired": { rule_name: iso_timestamp }, "seen": { "<tool>\\x1f<field>": [value, ...] } }

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

    def has_fired(self, rule_name: str) -> bool:
        return rule_name in self.fired

    def with_fired(self, rule_name: str, timestamp: str) -> "SessionState":
        return SessionState(
            fired={**self.fired, rule_name: timestamp},
            seen=self.seen,
        )

    def history(self, tool: str, field_name: str) -> tuple[str, ...]:
        return self.seen.get(_seen_key(tool, field_name), ())

    def with_observation(
        self, tool: str, field_name: str, value: str
    ) -> "SessionState":
        key = _seen_key(tool, field_name)
        prior = self.seen.get(key, ())
        return SessionState(
            fired=self.fired,
            seen={**self.seen, key: (*prior, value)},
        )


def _seen_key(tool: str, field_name: str) -> str:
    return f"{tool}{_KEY_SEP}{field_name}"


def state_path(session_id: str) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    # session_id is untrusted — sanitise path-traversal characters.
    safe = session_id.replace("/", "_").replace("\\", "_").replace("..", "_")
    return base / "claude-code" / "toolcheck" / f"{safe}.json"


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

    seen_raw = _as_dict(d.get("seen")) or {}
    seen: dict[str, tuple[str, ...]] = {}
    for k, v in seen_raw.items():
        if isinstance(v, list):
            seen[k] = tuple(
                item for item in cast("list[object]", v) if isinstance(item, str)
            )
    return SessionState(fired=fired, seen=seen)


def save_state(path: Path, state: SessionState) -> None:
    """Atomic write; swallows I/O errors."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "fired": state.fired,
            "seen": {k: list(v) for k, v in state.seen.items()},
        }
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except OSError:
        pass

"""End-to-end `toolcheck check --pi`: envelope, dedup, context_length, mapping."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import cast


def _run_hook(
    rules_toml: bytes,
    payload: dict[str, object],
    *,
    state_dir: Path | None = None,
    rules_dir: Path | None = None,
) -> dict[str, object]:
    """Run the hook subprocess and return its envelope dict."""
    rules_dir = Path(tempfile.mkdtemp()) if rules_dir is None else rules_dir
    (rules_dir / "test.toml").write_bytes(rules_toml)
    env = {**os.environ, "TOOLCHECK_RULES_DIR": str(rules_dir)}
    if state_dir is not None:
        env["XDG_STATE_HOME"] = str(state_dir)
    project = str(Path(__file__).resolve().parent.parent)
    result = subprocess.run(
        ["uv", "run", "--project", project, "toolcheck", "check", "--pi"],
        input=json.dumps(payload),
        capture_output=True,
        check=False,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"hook failed: {result.stderr}"
    output = cast("dict[str, object]", json.loads(result.stdout))
    assert output["decision"] in ("allow", "deny")
    assert isinstance(output.get("warnings"), list)
    assert isinstance(output.get("errors"), list)
    assert isinstance(output.get("reminders"), list)
    return output


def test_error_severity_produces_deny():
    error_toml = b"""
[[rule]]
name = "no-rm-rf"
tool = "Bash"
message = "destructive command blocked"
severity = "error"

[rule.match]
regex = { field = "command", pattern = '\\brm\\s+-rf\\b' }
"""
    envelope = _run_hook(
        error_toml,
        {"tool_name": "bash", "tool_input": {"command": "rm -rf /"}},
    )
    assert envelope["decision"] == "deny"
    errors = cast("list[str]", envelope["errors"])
    assert errors and "no-rm-rf" in errors[0]
    assert envelope["warnings"] == []


def test_warning_severity_produces_allow():
    warning_toml = b"""
[[rule]]
name = "pipe-to-wc"
tool = "Bash"
message = "use rg -c instead"
severity = "warning"

[rule.match]
regex = { field = "command", pattern = '\\brg\\b[^|]*\\|\\s*wc\\s+-l\\b' }
"""
    envelope = _run_hook(
        warning_toml,
        {"tool_name": "bash", "tool_input": {"command": "rg foo | wc -l"}},
    )
    assert envelope["decision"] == "allow"
    warnings = cast("list[str]", envelope["warnings"])
    assert warnings and "pipe-to-wc" in warnings[0]
    assert envelope["errors"] == []


def test_reminder_severity_produces_allow():
    reminder_toml = b"""
[[rule]]
name = "prefer-rg"
tool = "Bash"
message = "rg is available"
severity = "reminder"

[rule.match]
regex = { field = "command", pattern = '\\bgrep\\b' }
"""
    envelope = _run_hook(
        reminder_toml,
        {"tool_name": "bash", "tool_input": {"command": "grep -r foo ."}},
    )
    assert envelope["decision"] == "allow"
    reminders = cast("list[str]", envelope["reminders"])
    assert reminders and "prefer-rg" in reminders[0]
    assert envelope["warnings"] == []
    assert envelope["errors"] == []


def test_dedup_across_two_calls_same_session(tmp_path: Path):
    warning_toml = b"""
[[rule]]
name = "pipe-to-wc"
tool = "Bash"
message = "use rg -c instead"
severity = "warning"

[rule.match]
regex = { field = "command", pattern = '\\brg\\b[^|]*\\|\\s*wc\\s+-l\\b' }
"""
    payload: dict[str, object] = {
        "tool_name": "bash",
        "tool_input": {"command": "rg foo | wc -l"},
        "session_id": "pi-session-dedup",
    }
    state_dir = tmp_path / "state"
    first = _run_hook(warning_toml, payload, state_dir=state_dir)
    second = _run_hook(warning_toml, payload, state_dir=state_dir)
    assert cast("list[str]", first["warnings"])
    assert second["warnings"] == []
    assert second["decision"] == "allow"


def test_context_length_payload_drives_predicate():
    context_toml = b"""
[[rule]]
name = "read-wide-when-full"
tool = "Read"
message = "context is large"
severity = "warning"

[rule.match]
context_length = { op = ">", value = 1000 }
"""
    base: dict[str, object] = {"tool_name": "read", "tool_input": {"path": "/x.py"}}
    over = _run_hook(context_toml, {**base, "context_length": 2000})
    under = _run_hook(context_toml, {**base, "context_length": 500})
    unknown = _run_hook(context_toml, base)
    assert cast("list[str]", over["warnings"])
    assert under["warnings"] == []
    assert unknown["warnings"] == []


def test_cross_call_fires_after_field_mapping(tmp_path: Path):
    crosscall_toml = b"""
[[rule]]
name = "read-already-read"
tool = "Read"
message = "already read"
severity = "warning"

[rule.match]
cross_call = { kind = "duplicate_field_within_session", field = "file_path" }
"""
    payload: dict[str, object] = {
        "tool_name": "read",
        "tool_input": {"path": "/x.py"},
        "session_id": "pi-session-crosscall",
    }
    state_dir = tmp_path / "state"
    first = _run_hook(crosscall_toml, payload, state_dir=state_dir)
    second = _run_hook(crosscall_toml, payload, state_dir=state_dir)
    assert first["warnings"] == []
    warnings = cast("list[str]", second["warnings"])
    assert warnings and "read-already-read" in warnings[0]


def test_pi_edit_new_string_is_synthesised_for_rules():
    new_string_toml = b"""
[[rule]]
name = "no-comment"
tool = "Edit"
message = "comment detected"
severity = "warning"

[rule.match]
regex = { field = "new_string", pattern = '-- note' }
"""
    fired = _run_hook(
        new_string_toml,
        {
            "tool_name": "edit",
            "tool_input": {
                "path": "/x.hs",
                "edits": [{"oldText": "a", "newText": "x = 1 -- note"}],
            },
        },
    )
    warnings = cast("list[str]", fired["warnings"])
    assert warnings and "no-comment" in warnings[0]

    clean = _run_hook(
        new_string_toml,
        {
            "tool_name": "edit",
            "tool_input": {
                "path": "/x.hs",
                "edits": [{"oldText": "a", "newText": "x = 1"}],
            },
        },
    )
    assert clean["warnings"] == []


def test_non_integer_context_length_is_usage_error():
    error_toml = b"""
[[rule]]
name = "x"
tool = "Bash"
message = "m"
severity = "warning"

[rule.match]
context_length = { op = ">", value = 0 }
"""
    rules_dir = Path(tempfile.mkdtemp())
    (rules_dir / "test.toml").write_bytes(error_toml)
    env = {**os.environ, "TOOLCHECK_RULES_DIR": str(rules_dir)}
    project = str(Path(__file__).resolve().parent.parent)
    payload = {
        "tool_name": "bash",
        "tool_input": {"command": "ls"},
        "context_length": "lots",
    }
    result = subprocess.run(
        ["uv", "run", "--project", project, "toolcheck", "check", "--pi"],
        input=json.dumps(payload),
        capture_output=True,
        check=False,
        text=True,
        env=env,
    )
    assert result.returncode == 2
    assert "context_length" in result.stderr


BROKEN_SIBLING_TOML = b"""
[[rule]]
name = "good"
tool = "Bash"
message = "m"
severity = "warning"

[rule.match]
regex = { field = "command", pattern = 'zzz' }

[[rule]]
name = "bad"
tool = "Bash"

[rule.match]
regex = { field = "command", pattern = 'zzz' }
"""


def test_unparseable_rule_is_reported_as_warning():
    out = _run_hook(
        BROKEN_SIBLING_TOML,
        {"tool_name": "bash", "tool_input": {"command": "ls"}},
    )
    assert out["decision"] == "allow"
    warnings = cast("list[str]", out["warnings"])
    assert any("skipped" in w and "'bad'" in w for w in warnings)


def test_unparseable_rule_notice_is_deduped_per_session(tmp_path: Path):
    payload: dict[str, object] = {
        "tool_name": "bash",
        "tool_input": {"command": "ls"},
        "session_id": "notice-dedup",
    }
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    runs = [
        cast(
            "list[str]",
            _run_hook(
                BROKEN_SIBLING_TOML, payload, state_dir=tmp_path, rules_dir=rules_dir
            )["warnings"],
        )
        for _ in range(2)
    ]
    first, second = runs
    assert any("skipped" in w for w in first)
    assert not any("skipped" in w for w in second)

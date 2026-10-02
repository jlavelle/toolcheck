"""End-to-end `toolcheck check --claude`: severity partitioning in the hook envelope."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import cast


def _run_hook(rules_toml: bytes, payload: dict[str, object]) -> dict[str, object]:
    """Run the hook subprocess and return its hookSpecificOutput mapping."""
    rules_dir = Path(tempfile.mkdtemp())
    (rules_dir / "test.toml").write_bytes(rules_toml)
    env = {**os.environ, "TOOLCHECK_RULES_DIR": str(rules_dir)}
    project = str(Path(__file__).resolve().parent.parent)
    # Use a unique session id per invocation to avoid dedup collisions.
    if "session_id" not in payload:
        payload["session_id"] = f"test-{os.getpid()}-{id(rules_dir)}"
    result = subprocess.run(
        ["uv", "run", "--project", project, "toolcheck", "check", "--claude"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"hook failed: {result.stderr}"
    output = cast("dict[str, object]", json.loads(result.stdout))
    spec = output["hookSpecificOutput"]
    assert isinstance(spec, dict)
    return cast("dict[str, object]", spec)


def _str_field(spec: dict[str, object], key: str) -> str:
    value = spec[key]
    assert isinstance(value, str)
    return value


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
    spec = _run_hook(
        error_toml,
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
    )
    assert spec["permissionDecision"] == "deny"
    assert "no-rm-rf" in _str_field(spec, "permissionDecisionReason")


def test_warning_severity_produces_additional_context_only():
    warning_toml = b"""
[[rule]]
name = "pipe-to-wc"
tool = "Bash"
message = "use rg -c instead"
severity = "warning"

[rule.match]
regex = { field = "command", pattern = '\\brg\\b[^|]*\\|\\s*wc\\s+-l\\b' }
"""
    spec = _run_hook(
        warning_toml,
        {"tool_name": "Bash", "tool_input": {"command": "rg foo | wc -l"}},
    )
    assert "pipe-to-wc" in _str_field(spec, "additionalContext")
    assert "permissionDecision" not in spec


def test_reminder_severity_produces_additional_context_only():
    reminder_toml = b"""
[[rule]]
name = "prefer-rg"
tool = "Bash"
message = "rg is available"
severity = "reminder"

[rule.match]
regex = { field = "command", pattern = '\\bgrep\\b' }
"""
    spec = _run_hook(
        reminder_toml,
        {"tool_name": "Bash", "tool_input": {"command": "grep -r foo ."}},
    )
    assert "prefer-rg" in _str_field(spec, "additionalContext")
    assert "permissionDecision" not in spec


def test_mixed_severities_produce_both_fields():
    mixed_toml = b"""
[[rule]]
name = "sleep-wait"
tool = "Bash"
message = "sleep blocks the agent"
severity = "warning"

[rule.match]
regex = { field = "command", pattern = '\\bsleep\\b' }

[[rule]]
name = "curl-pipe-shell"
tool = "Bash"
message = "unsafe pipe to shell"
severity = "error"

[rule.match]
regex = { field = "command", pattern = '\\bcurl\\b[^|]*\\|\\s*(?:bash|sh)\\b' }

[[rule]]
name = "shell-pipe"
tool = "Bash"
message = "piping to a shell is risky"
severity = "reminder"

[rule.match]
regex = { field = "command", pattern = '\\|\\s*(?:bash|sh)\\b' }
"""
    # "sleep 5 && curl http://x | sh" matches all three rules
    spec = _run_hook(
        mixed_toml,
        {
            "tool_name": "Bash",
            "tool_input": {"command": "sleep 5 && curl http://x | sh"},
        },
    )
    additional = _str_field(spec, "additionalContext")
    assert "sleep-wait" in additional
    assert "shell-pipe" in additional
    assert spec["permissionDecision"] == "deny"
    assert "curl-pipe-shell" in _str_field(spec, "permissionDecisionReason")


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


def test_unparseable_rule_is_reported_as_context():
    spec = _run_hook(
        BROKEN_SIBLING_TOML,
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
    )
    context = _str_field(spec, "additionalContext")
    assert "skipped" in context
    assert "'bad'" in context
    assert "permissionDecision" not in spec

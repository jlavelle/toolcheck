"""CLI: list, check, and lint-rules commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from conftest import RulesEnv
from toolcheck.cli import build_parser
from toolcheck.schema import clear_rule_cache

TOML_BYTES = b"""
[[rule]]
name = "rg-pipe-wc"
tool = "Bash"
message = "msg"
severity = "warning"

[rule.match]
regex = { field = "command", pattern = '\\brg\\b[^|]*\\|\\s*wc\\s+-l\\b' }

[[rule]]
name = "read-already-read"
tool = "Read"
message = "msg"
severity = "warning"
sample_field = "file_path"

[rule.match]
cross_call = { kind = "duplicate_field_within_session", field = "file_path" }

[[rule]]
name = "grep-unscoped"
tool = "Grep"
message = "msg"
severity = "warning"
sample_field = "pattern"

[rule.match]
all = [
    { absent = "path" },
    { absent = "glob" },
]
"""

DISABLED_TOML = b"""
[[rule]]
name = "rule-active"
tool = "Bash"
message = "m"
severity = "warning"

[rule.match]
regex = { field = "command", pattern = 'x' }

[[rule]]
name = "rule-disabled"
tool = "Bash"
message = "m"
severity = "warning"
enabled = false

[rule.match]
regex = { field = "command", pattern = 'x' }
"""


@pytest.fixture
def rules_dir(rules_env: RulesEnv) -> Path:
    return rules_env(TOML_BYTES)


def _run(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def test_list_human(rules_dir: Path, capsys: pytest.CaptureFixture[str]):
    assert _run(["list"]) == 0
    out = capsys.readouterr().out
    assert "rg-pipe-wc" in out
    assert "read-already-read" in out


def test_list_json(rules_dir: Path, capsys: pytest.CaptureFixture[str]):
    assert _run(["list", "--json"]) == 0
    data = cast("list[dict[str, object]]", json.loads(capsys.readouterr().out))
    assert isinstance(data, list)
    assert any(r["name"] == "grep-unscoped" for r in data)


def test_list_filter_tool(rules_dir: Path, capsys: pytest.CaptureFixture[str]):
    assert _run(["list", "--tool", "Grep"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out and all("Grep" in line for line in out)


def _write(tmp_path: Path, payload: dict[str, object]) -> str:
    p = tmp_path / "call.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def test_check_fires_exits_one(
    rules_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    f = _write(
        tmp_path, {"tool_name": "Bash", "tool_input": {"command": "rg x | wc -l"}}
    )
    assert _run(["check", f]) == 1
    assert "rg-pipe-wc" in capsys.readouterr().out


def test_check_json(
    rules_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    f = _write(
        tmp_path, {"tool_name": "Bash", "tool_input": {"command": "rg x | wc -l"}}
    )
    assert _run(["check", f, "--json"]) == 1
    data = cast("list[dict[str, object]]", json.loads(capsys.readouterr().out))
    assert [r["name"] for r in data] == ["rg-pipe-wc"]


def test_check_no_fire_exits_zero(
    rules_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    f = _write(tmp_path, {"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
    assert _run(["check", f]) == 0
    assert "(no rules fired)" in capsys.readouterr().out


def test_check_missing_tool_name_errors(tmp_path: Path):
    f = _write(tmp_path, {"tool_input": {"command": "rg x | wc -l"}})
    assert _run(["check", f]) == 2


def test_check_tool_override(
    rules_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    f = _write(tmp_path, {"tool_input": {"command": "rg x | wc -l"}})
    assert _run(["check", f, "--tool", "Bash"]) == 1
    assert "rg-pipe-wc" in capsys.readouterr().out


def test_list_hides_disabled(rules_env: RulesEnv, capsys: pytest.CaptureFixture[str]):
    rules_env(DISABLED_TOML)
    assert _run(["list"]) == 0
    out = capsys.readouterr().out
    assert "rule-active" in out
    assert "rule-disabled" not in out

    assert _run(["list", "--all"]) == 0
    out = capsys.readouterr().out
    assert "rule-active" in out
    assert "rule-disabled" in out
    assert "[disabled]" in out


def test_list_all_json_includes_disabled(
    rules_env: RulesEnv, capsys: pytest.CaptureFixture[str]
):
    rules_env(DISABLED_TOML)
    assert _run(["list", "--all", "--json"]) == 0
    data = cast("list[dict[str, object]]", json.loads(capsys.readouterr().out))
    names = [r["name"] for r in data]
    assert "rule-active" in names
    assert "rule-disabled" in names
    off_rule = next(r for r in data if r["name"] == "rule-disabled")
    assert off_rule["enabled"] is False


def test_lint_rules_clean_catalogue(
    rules_dir: Path, capsys: pytest.CaptureFixture[str]
):
    assert _run(["lint-rules"]) == 0
    assert "ok" in capsys.readouterr().out


def test_lint_rules_reports_bad_regex(
    rules_env: RulesEnv, capsys: pytest.CaptureFixture[str]
):
    rules_env(b"""
[[rule]]
name = "bad-regex"
tool = "Bash"
message = "m"
severity = "warning"

[rule.match]
regex = { field = "command", pattern = '(' }
""")
    assert _run(["lint-rules"]) == 1
    err = capsys.readouterr().err
    assert "bad-regex" in err
    assert "does not compile" in err


def test_lint_rules_reports_unknown_derived_field(
    rules_env: RulesEnv, capsys: pytest.CaptureFixture[str]
):
    rules_env(b"""
[[rule]]
name = "typo-derived"
tool = "Bash"
message = "m"
severity = "warning"

[rule.match]
equals = { field = "@last_pipe_cmmand", value = "head" }
""")
    assert _run(["lint-rules"]) == 1
    err = capsys.readouterr().err
    assert "typo-derived" in err
    assert "@last_pipe_cmmand" in err


def test_lint_rules_reports_nested_cross_call(
    rules_env: RulesEnv, capsys: pytest.CaptureFixture[str]
):
    rules_env(b"""
[[rule]]
name = "nested-cross-call"
tool = "Read"
message = "m"
severity = "warning"

[rule.match]
not = { cross_call = { kind = "duplicate_field_within_session", field = "file_path" } }
""")
    assert _run(["lint-rules"]) == 1
    err = capsys.readouterr().err
    assert "nested-cross-call" in err
    assert "entire match" in err


def test_lint_rules_reports_duplicate_names(
    rules_env: RulesEnv, capsys: pytest.CaptureFixture[str]
):
    rules_env(b"""
[[rule]]
name = "twin"
tool = "Bash"
message = "m"
severity = "warning"

[rule.match]
regex = { field = "command", pattern = 'x' }

[[rule]]
name = "twin"
tool = "Bash"
message = "m"
severity = "warning"

[rule.match]
regex = { field = "command", pattern = 'y' }
""")
    assert _run(["lint-rules"]) == 1
    assert "duplicate rule name" in capsys.readouterr().err


def test_lint_rules_reports_unparseable_file(
    rules_dir: Path, capsys: pytest.CaptureFixture[str]
):
    (rules_dir / "zz-broken.toml").write_bytes(b'[[rule]]\nname = "x"\ntool = "Bash"\n')
    clear_rule_cache()
    assert _run(["lint-rules"]) == 1
    err = capsys.readouterr().err
    assert "zz-broken.toml" in err


def test_check_still_fires_with_a_broken_sibling_file(
    rules_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    (rules_dir / "zz-broken.toml").write_bytes(b"not toml [")
    clear_rule_cache()
    f = _write(
        tmp_path, {"tool_name": "Bash", "tool_input": {"command": "rg x | wc -l"}}
    )
    assert _run(["check", f]) == 1
    out, err = capsys.readouterr()
    assert "rg-pipe-wc" in out
    assert "zz-broken.toml" in err

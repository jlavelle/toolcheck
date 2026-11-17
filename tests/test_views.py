"""Views: per-tool derived fields that let one rule read several tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from toolcheck.cli import evaluate_call, lint_rules, render
from toolcheck.compile import Env, Values, resolve_field
from toolcheck.schema import AstQuery, Rule, parse_rules, views_by_tool
from toolcheck.state import SessionState
from toolcheck.syntax import extract

SHELL_VIEWS = b"""
[[view]]
name = "@shell"
tool = "Bash"
field = "command"

[[view]]
name = "@shell"
tool = "mcp__x__run"
field = "code"
language = "typescript"
[view.rule]
any = [
  { kind = "template_string", inside = { kind = "call_expression", has = { field = "function", regex = "^sh$" } } },
  { any = [{ kind = "string" }, { kind = "template_string" }], inside = { kind = "arguments", inside = { kind = "call_expression", has = { field = "function", regex = "^exec$" } } } },
]
"""

GH_RULE = b"""
[[rule]]
name = "gh-pr-create"
tool = ["Bash", "mcp__x__run"]
message = "m"
severity = "error"
[rule.match.ast]
field = "@shell"
language = "bash"
rule = { kind = "command", has = { field = "name", regex = "^gh$" }, all = [{ has = { kind = "word", regex = "^create$" } }] }
"""

SCRIPT = """
const s = "sh`gh pr create`";
const out = await sh`cd ${dir} && gh pr create --title ${title}`;
await exec("git commit -m \\"fix it\\"", { cwd });
"""


def _catalogue(toml: bytes) -> tuple[list[Rule], Env]:
    parsed = parse_rules(toml, Path("x.toml"))
    assert parsed.errors == ()
    return list(parsed.rules), Env({}, views=views_by_tool(parsed.views))


def _fired(toml: bytes, tool: str, tool_input: dict[str, object]) -> list[str]:
    rules, env = _catalogue(toml)
    fired, _ = evaluate_call(
        tool, tool_input, SessionState(), False, "", rules, views=env.views
    )
    return [r.name for r in fired]


def test_view_extracts_shell_text_from_script_calls():
    _, env = _catalogue(SHELL_VIEWS)
    script = Env({"code": SCRIPT}, tool="mcp__x__run", views=env.views)
    assert resolve_field("@shell", script) == Values(
        ("cd _ && gh pr create --title _", 'git commit -m "fix it"')
    )


@pytest.mark.parametrize(
    ("tool", "tool_input", "fires"),
    [
        ("Bash", {"command": "cd x && gh pr create --fill"}, True),
        ("Bash", {"command": "echo 'gh pr create'"}, False),
        ("mcp__x__run", {"code": SCRIPT}, True),
        ("mcp__x__run", {"code": 'const s = "sh`gh pr create`";'}, False),
        ("Read", {"file_path": "gh pr create"}, False),
    ],
)
def test_one_rule_reads_the_view_of_each_of_its_tools(
    tool: str, tool_input: dict[str, object], fires: bool
):
    assert bool(_fired(SHELL_VIEWS + GH_RULE, tool, tool_input)) is fires


def test_field_predicates_hold_when_some_item_holds():
    toml = (
        SHELL_VIEWS
        + b"""
[[rule]]
name = "commits"
tool = "mcp__x__run"
message = "m"
[rule.match]
regex = { field = "@shell", pattern = '^git commit' }

[[rule]]
name = "no-push"
tool = "mcp__x__run"
message = "m"
[rule.match]
not = { regex = { field = "@shell", pattern = 'git push' } }

[[rule]]
name = "runs-shell"
tool = "mcp__x__run"
message = "m"
[rule.match]
present = "@shell"
"""
    )
    tool = "mcp__x__run"
    assert _fired(toml, tool, {"code": SCRIPT}) == ["commits", "no-push", "runs-shell"]
    assert _fired(toml, tool, {"code": "console.log(1)"}) == ["no-push"]


def test_view_can_read_another_view():
    toml = (
        SHELL_VIEWS
        + b"""
[[view]]
name = "@commands"
tool = "mcp__x__run"
field = "@shell"
language = "bash"
rule = { kind = "command_name" }
"""
    )
    _, env = _catalogue(toml)
    script = Env({"code": SCRIPT}, tool="mcp__x__run", views=env.views)
    assert resolve_field("@commands", script) == Values(("cd", "gh", "git"))


def test_sample_defaults_by_the_calling_tool():
    rules, env = _catalogue(SHELL_VIEWS + GH_RULE)
    (rule,) = rules
    bash = Env({"command": "gh pr create"}, tool="Bash", views=env.views)
    script = Env({"code": SCRIPT}, tool="mcp__x__run", views=env.views)
    assert render(rule, bash) == "[gh-pr-create] m\n> gh pr create"
    assert render(rule, script) == "[gh-pr-create] m"


def test_view_sample_lists_each_item():
    rules, env = _catalogue(
        SHELL_VIEWS + GH_RULE.replace(b'severity = "error"', b'sample_field = "@shell"')
    )
    script = Env({"code": SCRIPT}, tool="mcp__x__run", views=env.views)
    assert render(rules[0], script) == (
        '[gh-pr-create] m\n> cd _ && gh pr create --title _\ngit commit -m "fix it"'
    )


def test_view_with_a_bad_query_is_a_load_error():
    parsed = parse_rules(
        b"""
[[view]]
name = "@d"
tool = "Bash"
field = "command"
language = "bash"
rule = { kind = "comand" }
""",
        Path("x.toml"),
    )
    assert parsed.views == ()
    assert "does not compile" in parsed.errors[0].message


@pytest.mark.parametrize(
    ("language", "code", "expected"),
    [
        ("typescript", 'f(""); f(`a\\`b`)', ("", "a`b")),
        ("python", 'f("a\\nb")', ("a\nb",)),
    ],
)
def test_string_literals_read_as_their_contents(
    language: str, code: str, expected: tuple[str, ...]
):
    rule: dict[str, object] = {"regex": '^[`"]', "not": {"inside": {"regex": '^[`"]'}}}
    assert extract(AstQuery(language, rule), code) == expected


def test_view_name_must_be_derived():
    parsed = parse_rules(
        b'[[view]]\nname = "shell"\ntool = "Bash"\nfield = "command"\n', Path("x.toml")
    )
    assert parsed.views == ()
    assert "view[0] ('shell')" in parsed.errors[0].message


def test_lint_reports_view_problems():
    toml = b"""
[[view]]
name = "@a"
tool = "Bash"
field = "@b"

[[view]]
name = "@b"
tool = "Bash"
field = "@a"

[[view]]
name = "@c"
tool = "Bash"
field = "@nonesuch"

[[rule]]
name = "reads-other-tools-view"
tool = "Read"
message = "m"
[rule.match]
present = "@a"
"""
    parsed = parse_rules(toml, Path("x.toml"))
    problems = lint_rules(parsed.rules, parsed.views)
    assert any(
        p.startswith("view @a on Bash: ") and "reads itself" in p for p in problems
    )
    assert any(p.startswith("view @c on Bash: unknown derived field") for p in problems)
    assert "reads-other-tools-view: unknown derived field: '@a'" in problems

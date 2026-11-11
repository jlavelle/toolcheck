"""Schema: predicate parse round-trip and catalogue well-formedness."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given

from conftest import RulesEnv
from strategies import predicates
from toolcheck.compile import compile_rule
from toolcheck.schema import (
    AllPredicate,
    AnyPredicate,
    ContextLengthPredicate,
    CrossCallPredicate,
    InPredicate,
    LengthPredicate,
    NotPredicate,
    Predicate,
    PresencePredicate,
    RegexPredicate,
    clear_rule_cache,
    cross_call_of,
    is_stateless,
    load_catalogue,
    load_errors,
    parse_predicate,
    parse_rules,
)

TOML_BYTES = b"""
[[rule]]
name = "read-already-read"
tool = "Read"
message = "file already Read this session -- skip the redundant Read"
severity = "warning"
sample_field = "file_path"

[rule.match]
cross_call = { kind = "duplicate_field_within_session", field = "file_path" }
"""


def pred_to_toml(p: Predicate) -> dict[str, object]:
    """Inverse of parse_predicate — the TOML mapping for a predicate."""
    match p:
        case RegexPredicate(field=f, pattern=pat):
            return {"regex": {"field": f, "pattern": pat}}
        case PresencePredicate(field=f, present=want):
            return {"present": f} if want else {"absent": f}
        case InPredicate(field=f, values=vs):
            if len(vs) == 1:
                return {"equals": {"field": f, "value": vs[0]}}
            return {"in": {"field": f, "values": list(vs)}}
        case LengthPredicate(field=f, op=op, value=v):
            return {"length": {"field": f, "op": op, "value": v}}
        case ContextLengthPredicate(op=op, value=v):
            return {"context_length": {"op": op, "value": v}}
        case NotPredicate(operand=o):
            return {"not": pred_to_toml(o)}
        case AllPredicate(operands=ops):
            return {"all": [pred_to_toml(o) for o in ops]}
        case AnyPredicate(operands=ops):
            return {"any": [pred_to_toml(o) for o in ops]}
        case CrossCallPredicate(kind=k, field=f):
            return {"cross_call": {"kind": k, "field": f}}


@given(predicates())
def test_predicate_parse_roundtrip(p: Predicate):
    assert parse_predicate(pred_to_toml(p)) == p


def test_parse_rejects_multiple_keys():
    with pytest.raises(ValueError):
        parse_predicate({"regex": {"field": "command", "pattern": "x"}, "all": []})


def test_parse_rejects_unknown_key():
    with pytest.raises(ValueError):
        parse_predicate({"regexp": {"field": "command", "pattern": "x"}})


def test_parse_rejects_unknown_cross_call_kind():
    with pytest.raises(ValueError):
        parse_predicate({"cross_call": {"kind": "bogus", "field": "file_path"}})


def test_catalogue_loads(rules_env: RulesEnv):
    rules_env(TOML_BYTES)
    rules = load_catalogue()
    assert isinstance(rules, list)
    names = [r.name for r in rules]
    assert len(names) == len(set(names)), "rule names must be unique"


def test_catalogue_loads_from_multiple_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "one.toml").write_bytes(
        b'[[rule]]\nname = "from-a"\ntool = "Read"\nmessage = "a"\n[rule.match]\npresent = "file_path"\n'
    )
    (b / "two.toml").write_bytes(
        b'[[rule]]\nname = "from-b"\ntool = "Read"\nmessage = "b"\n[rule.match]\npresent = "file_path"\n'
    )
    # Surrounding whitespace and empty segments must be tolerated.
    monkeypatch.setenv("TOOLCHECK_RULES_DIRS", f"{a}, {b} ,,")
    monkeypatch.delenv("TOOLCHECK_RULES_DIR", raising=False)
    clear_rule_cache()
    try:
        names = {r.name for r in load_catalogue()}
        assert {"from-a", "from-b"} <= names
    finally:
        clear_rule_cache()


def test_later_dir_overrides_rule_last_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "one.toml").write_bytes(
        b'[[rule]]\nname = "dup"\ntool = "Read"\nmessage = "from a"\n[rule.match]\npresent = "file_path"\n'
    )
    (b / "two.toml").write_bytes(
        b'[[rule]]\nname = "dup"\ntool = "Read"\nmessage = "from b"\n[rule.match]\npresent = "file_path"\n'
    )
    monkeypatch.setenv("TOOLCHECK_RULES_DIRS", f"{a},{b}")
    monkeypatch.delenv("TOOLCHECK_RULES_DIR", raising=False)
    clear_rule_cache()
    try:
        dups = [r for r in load_catalogue() if r.name == "dup"]
        assert len(dups) == 1
        assert dups[0].message == "from b"
    finally:
        clear_rule_cache()


def test_catalogue_memoised(rules_env: RulesEnv):
    rules_env(TOML_BYTES)
    a, b = load_catalogue(), load_catalogue()
    assert len(a) == len(b)
    assert all(x is y for x, y in zip(a, b))


def test_every_rule_well_formed(rules_env: RulesEnv):
    rules_env(TOML_BYTES)
    for r in load_catalogue():
        assert r.name and r.tool and r.message
        assert r.severity in ("warning", "error", "reminder")


def test_stateless_rules_compile(rules_env: RulesEnv):
    rules_env(TOML_BYTES)
    for r in load_catalogue():
        if is_stateless(r):
            compile_rule(r)


def test_read_already_read_is_cross_call(rules_env: RulesEnv):
    rules_env(TOML_BYTES)
    rules = {r.name: r for r in load_catalogue()}
    rule = rules["read-already-read"]
    assert not is_stateless(rule)
    cc = cross_call_of(rule)
    assert cc is not None
    assert cc.kind == "duplicate_field_within_session"
    assert cc.field == "file_path"


def test_dedup_defaults_warning_true_error_false():
    toml = b"""
[[rule]]
name = "warn-rule"
tool = "Bash"
message = "m"
severity = "warning"

[rule.match]
regex = { field = "command", pattern = 'x' }

[[rule]]
name = "error-rule"
tool = "Bash"
message = "m"
severity = "error"

[rule.match]
regex = { field = "command", pattern = 'x' }

[[rule]]
name = "error-with-dedup"
tool = "Bash"
message = "m"
severity = "error"
dedup = true

[rule.match]
regex = { field = "command", pattern = 'x' }

[[rule]]
name = "warn-no-dedup"
tool = "Bash"
message = "m"
severity = "warning"
dedup = false

[rule.match]
regex = { field = "command", pattern = 'x' }

[[rule]]
name = "reminder-rule"
tool = "Bash"
message = "m"
severity = "reminder"

[rule.match]
regex = { field = "command", pattern = 'x' }
"""
    rules = {r.name: r for r in parse_rules(toml, Path("x.toml")).rules}
    assert rules["warn-rule"].dedup is True
    assert rules["error-rule"].dedup is False
    assert rules["error-with-dedup"].dedup is True
    assert rules["warn-no-dedup"].dedup is False
    assert rules["reminder-rule"].dedup is True


def test_every_calls_parses_and_defaults_none():
    toml = b"""
[[rule]]
name = "throttled"
tool = "Bash"
message = "m"
severity = "reminder"
every_calls = 10

[rule.match]
regex = { field = "command", pattern = 'x' }

[[rule]]
name = "unthrottled"
tool = "Bash"
message = "m"
severity = "reminder"

[rule.match]
regex = { field = "command", pattern = 'x' }
"""
    rules = {r.name: r for r in parse_rules(toml, Path("x.toml")).rules}
    assert rules["throttled"].every_calls == 10
    assert rules["unthrottled"].every_calls is None


def test_every_calls_rejects_nonpositive():
    toml = b"""
[[rule]]
name = "bad"
tool = "Bash"
message = "m"
severity = "reminder"
every_calls = 0

[rule.match]
regex = { field = "command", pattern = 'x' }
"""
    catalogue = parse_rules(toml, Path("x.toml"))
    assert catalogue.rules == ()
    assert len(catalogue.errors) == 1
    assert "every_calls" in catalogue.errors[0].message


def test_sample_field_defaults_per_tool():
    toml = b"""
[[rule]]
name = "bash-rule"
tool = "Bash"
message = "m"
severity = "warning"

[rule.match]
regex = { field = "command", pattern = 'x' }

[[rule]]
name = "unknown-tool-rule"
tool = "Nonesuch"
message = "m"
severity = "warning"

[rule.match]
regex = { field = "command", pattern = 'x' }
"""
    rules = {r.name: r for r in parse_rules(toml, Path("x.toml")).rules}
    assert rules["bash-rule"].sample_field == "command"
    assert rules["unknown-tool-rule"].sample_field is None


def test_enabled_defaults_true():
    toml = b"""
[[rule]]
name = "on-by-default"
tool = "Bash"
message = "m"
severity = "warning"

[rule.match]
regex = { field = "command", pattern = 'x' }

[[rule]]
name = "disabled"
tool = "Bash"
message = "m"
severity = "warning"
enabled = false

[rule.match]
regex = { field = "command", pattern = 'x' }

[[rule]]
name = "explicitly-enabled"
tool = "Bash"
message = "m"
severity = "warning"
enabled = true

[rule.match]
regex = { field = "command", pattern = 'x' }
"""
    rules = {r.name: r for r in parse_rules(toml, Path("x.toml")).rules}
    assert rules["on-by-default"].enabled is True
    assert rules["disabled"].enabled is False
    assert rules["explicitly-enabled"].enabled is True


def test_load_catalogue_excludes_disabled(rules_env: RulesEnv):
    rules_env(b"""
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
""")
    names = [r.name for r in load_catalogue()]
    assert "rule-active" in names
    assert "rule-disabled" not in names


BROKEN_RULE_TOML = b"""
[[rule]]
name = "good"
tool = "Read"
message = "m"
[rule.match]
present = "file_path"

[[rule]]
name = "bad"
tool = "Read"
[rule.match]
present = "file_path"
"""


def test_parse_rules_keeps_siblings_of_a_bad_rule():
    cat = parse_rules(BROKEN_RULE_TOML, Path("x.toml"))
    assert [r.name for r in cat.rules] == ["good"]
    assert len(cat.errors) == 1
    assert cat.errors[0].path == Path("x.toml")
    assert "'bad'" in cat.errors[0].message
    assert "'message'" in cat.errors[0].message


def test_parse_rules_undecodable_file_is_one_error():
    cat = parse_rules(b"[[rule]\nname = ", Path("x.toml"))
    assert cat.rules == ()
    assert len(cat.errors) == 1


def test_catalogue_survives_a_broken_sibling_file(rules_env: RulesEnv):
    d = rules_env(TOML_BYTES)
    (d / "zz-broken.toml").write_bytes(b"not toml at all [")
    clear_rule_cache()
    assert [r.name for r in load_catalogue()] == ["read-already-read"]
    errors = load_errors()
    assert [e.path for e in errors] == [d / "zz-broken.toml"]


def _message_file_rule(message_keys: str) -> bytes:
    return f"""
[[rule]]
name = "from-file"
tool = "Bash"
{message_keys}
[rule.match]
present = "command"
""".encode()


def test_message_file_resolves_beside_the_rules_file(tmp_path: Path):
    (tmp_path / "hints").mkdir()
    (tmp_path / "hints" / "git.txt").write_text(
        "Commit on a branch.\n\nNever on main.\n"
    )
    cat = parse_rules(
        _message_file_rule('message_file = "hints/git.txt"'), tmp_path / "x.toml"
    )
    assert cat.errors == ()
    assert cat.rules[0].message == "Commit on a branch.\n\nNever on main."


def test_message_file_takes_an_absolute_path(tmp_path: Path):
    hint = tmp_path / "hint.txt"
    hint.write_text("absolute\n")
    cat = parse_rules(
        _message_file_rule(f"message_file = {str(hint)!r}"), Path("elsewhere/x.toml")
    )
    assert cat.rules[0].message == "absolute"


def test_message_and_message_file_together_is_an_error(tmp_path: Path):
    (tmp_path / "hint.txt").write_text("x")
    cat = parse_rules(
        _message_file_rule('message = "m"\nmessage_file = "hint.txt"'),
        tmp_path / "x.toml",
    )
    assert cat.rules == ()
    assert "not both" in cat.errors[0].message


def test_missing_message_file_is_a_load_error(tmp_path: Path):
    cat = parse_rules(
        _message_file_rule('message_file = "absent.txt"'), tmp_path / "x.toml"
    )
    assert cat.rules == ()
    assert "absent.txt" in cat.errors[0].message


def test_message_file_that_is_not_utf8_is_a_load_error(tmp_path: Path):
    (tmp_path / "hint.txt").write_bytes(b"\xff\xfe\x00")
    cat = parse_rules(
        _message_file_rule('message_file = "hint.txt"'), tmp_path / "x.toml"
    )
    assert cat.rules == ()
    assert "not UTF-8" in cat.errors[0].message


def test_message_file_that_is_a_directory_is_a_load_error(tmp_path: Path):
    (tmp_path / "hints").mkdir()
    cat = parse_rules(_message_file_rule('message_file = "hints"'), tmp_path / "x.toml")
    assert cat.rules == ()
    assert "hints" in cat.errors[0].message

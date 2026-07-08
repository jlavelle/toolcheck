"""Schema: predicate parse round-trip and catalogue well-formedness."""

from __future__ import annotations

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
    cross_call_of,
    is_stateless,
    load_catalogue,
    load_toml,
    parse_predicate,
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


def test_catalogue_memoised(rules_env: RulesEnv):
    rules_env(TOML_BYTES)
    a, b = load_catalogue(), load_catalogue()
    assert len(a) == len(b)
    assert all(x is y for x, y in zip(a, b))


def test_every_rule_well_formed(rules_env: RulesEnv):
    rules_env(TOML_BYTES)
    for r in load_catalogue():
        assert r.name and r.tool and r.message
        assert r.severity in ("warning", "error")


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
"""
    rules = {r.name: r for r in load_toml(toml)}
    assert rules["warn-rule"].dedup is True
    assert rules["error-rule"].dedup is False
    assert rules["error-with-dedup"].dedup is True
    assert rules["warn-no-dedup"].dedup is False


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
    rules = {r.name: r for r in load_toml(toml)}
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
    rules = {r.name: r for r in load_toml(toml)}
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

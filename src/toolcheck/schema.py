"""Declarative hint catalogue: schema, TOML loader, catalogue accessor.

Predicate algebra over tool-input fields. A field name is either a raw
tool_input key or, when @-prefixed, a derived field computed by a pure
extractor (see toolcheck.compile):
  RegexPredicate     — regex search on a field
  PresencePredicate  — a field is present (non-null) or absent
  InPredicate        — a field's value is one of a fixed set (equals = singleton)
  LengthPredicate    — compare a field's length to a constant
  ContextLengthPredicate — compare the session's current context length to a constant
  NotPredicate       — logical NOT
  AllPredicate       — logical AND
  AnyPredicate       — logical OR
  CrossCallPredicate — session-scoped; excluded by is_stateless() before compilation
"""

from __future__ import annotations

import functools
import os
import tomllib
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast


@dataclass(frozen=True)
class RegexPredicate:
    field: str  # tool-input field name (e.g. "command", "file_path")
    pattern: str  # regex pattern string


@dataclass(frozen=True)
class PresencePredicate:
    field: str  # raw or @-derived field name
    present: bool  # True: field resolves non-null; False: field absent/null


@dataclass(frozen=True)
class InPredicate:
    field: str  # raw or @-derived field name
    values: tuple[str, ...]  # field's str value must be one of these


@dataclass(frozen=True)
class NotPredicate:
    operand: Predicate


@dataclass(frozen=True)
class AllPredicate:
    operands: tuple[Predicate, ...]


@dataclass(frozen=True)
class AnyPredicate:
    operands: tuple[Predicate, ...]


@dataclass(frozen=True)
class LengthPredicate:
    """Compare a field's char length to a constant — avoids per-tool macros for size checks."""

    field: str
    op: Literal[">", ">=", "<", "<=", "==", "!="]
    value: int


@dataclass(frozen=True)
class ContextLengthPredicate:
    """Compare the session's current context length (in tokens) to a constant.

    The value is ambient — not a tool-input field — so it reads from the
    evaluation environment. It is unknown (None) outside --claude and on the
    first call of a session; an unknown comparison holds False.
    """

    op: Literal[">", ">=", "<", "<=", "==", "!="]
    value: int


CrossCallKind = Literal["duplicate_field_within_session", "adjacent_near_identical"]


@dataclass(frozen=True)
class CrossCallPredicate:
    """Session-scoped; excluded by is_stateless() before compilation."""

    kind: CrossCallKind
    field: str  # which tool_input field to track across calls


Predicate = (
    RegexPredicate
    | PresencePredicate
    | InPredicate
    | LengthPredicate
    | ContextLengthPredicate
    | NotPredicate
    | AllPredicate
    | AnyPredicate
    | CrossCallPredicate
)


@dataclass(frozen=True)
class Rule:
    name: str
    tool: str  # "Bash", "Read", "Edit", "Grep", "mcp__*", …
    message: str  # one-line combined diagnosis + remedy
    severity: Literal["warning", "error", "reminder"]
    dedup: bool  # True: fire at most once per (session, rule); False: fire every match
    sample_field: (
        str | None
    )  # field shown as the sample line in hint output; None: no sample
    match: Predicate
    every_calls: int | None = (
        None  # throttle: at most once per N session tool calls; supersedes dedup
    )
    enabled: bool = True  # False: rule is skipped entirely


_DEFAULT_SAMPLE: dict[str, str] = {
    "Bash": "command",
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "Grep": "pattern",
    "Glob": "pattern",
    "Agent": "description",
}


_LENGTH_OPS: frozenset[str] = frozenset({">", ">=", "<", "<=", "==", "!="})
_SEVERITIES: frozenset[str] = frozenset({"warning", "error", "reminder"})
_CROSS_CALL_KINDS: frozenset[str] = frozenset(
    {"duplicate_field_within_session", "adjacent_near_identical"}
)
_PREDICATE_KEYS: frozenset[str] = frozenset(
    {
        "regex",
        "present",
        "absent",
        "equals",
        "in",
        "length",
        "context_length",
        "not",
        "all",
        "any",
        "cross_call",
    }
)


def _mapping(x: object, ctx: str) -> dict[str, object]:
    if not isinstance(x, dict):
        raise TypeError(f"{ctx} must be a mapping, got {type(x).__name__}")
    return cast("dict[str, object]", x)


def _seq(x: object, ctx: str) -> list[object]:
    if not isinstance(x, list):
        raise TypeError(f"{ctx} must be a list, got {type(x).__name__}")
    return cast("list[object]", x)


def _string(x: object, ctx: str) -> str:
    if not isinstance(x, str):
        raise TypeError(f"{ctx} must be a string, got {type(x).__name__}")
    return x


def _integer(x: object, ctx: str) -> int:
    # bool is an int subclass; reject explicitly.
    if isinstance(x, bool) or not isinstance(x, int):
        raise TypeError(f"{ctx} must be an integer, got {type(x).__name__}")
    return x


def _severity(x: object) -> Literal["warning", "error", "reminder"]:
    s = "warning" if x is None else _string(x, "severity")
    if s not in _SEVERITIES:
        raise ValueError(f"severity must be one of {sorted(_SEVERITIES)}, got {s!r}")
    return cast('Literal["warning", "error", "reminder"]', s)


def _length_op(x: object, ctx: str) -> Literal[">", ">=", "<", "<=", "==", "!="]:
    op = _string(x, ctx)
    if op not in _LENGTH_OPS:
        raise ValueError(f"{ctx} must be one of {sorted(_LENGTH_OPS)}, got {op!r}")
    return cast('Literal[">", ">=", "<", "<=", "==", "!="]', op)


def parse_predicate(d: object) -> Predicate:
    """Parse a single predicate mapping. Useful for rule authoring and tests."""
    m = _mapping(d, "predicate")
    unknown = set(m) - _PREDICATE_KEYS
    if unknown:
        raise ValueError(f"unknown predicate keys: {sorted(unknown)!r}")
    if len(m) != 1:
        raise ValueError(
            f"predicate must have exactly one key, got {sorted(m.keys())!r}"
        )
    if "regex" in m:
        r = _mapping(m["regex"], "regex")
        return RegexPredicate(
            field=_string(r["field"], "regex.field"),
            pattern=_string(r["pattern"], "regex.pattern"),
        )
    if "present" in m:
        return PresencePredicate(field=_string(m["present"], "present"), present=True)
    if "absent" in m:
        return PresencePredicate(field=_string(m["absent"], "absent"), present=False)
    if "equals" in m:
        eq = _mapping(m["equals"], "equals")
        return InPredicate(
            field=_string(eq["field"], "equals.field"),
            values=(_string(eq["value"], "equals.value"),),
        )
    if "in" in m:
        i = _mapping(m["in"], "in")
        return InPredicate(
            field=_string(i["field"], "in.field"),
            values=tuple(
                _string(v, "in.values[]") for v in _seq(i["values"], "in.values")
            ),
        )
    if "length" in m:
        lp = _mapping(m["length"], "length")
        return LengthPredicate(
            field=_string(lp["field"], "length.field"),
            op=_length_op(lp["op"], "length.op"),
            value=_integer(lp["value"], "length.value"),
        )
    if "context_length" in m:
        cl = _mapping(m["context_length"], "context_length")
        return ContextLengthPredicate(
            op=_length_op(cl["op"], "context_length.op"),
            value=_integer(cl["value"], "context_length.value"),
        )
    if "not" in m:
        return NotPredicate(operand=parse_predicate(m["not"]))
    if "all" in m:
        return AllPredicate(
            operands=tuple(parse_predicate(p) for p in _seq(m["all"], "all"))
        )
    if "any" in m:
        return AnyPredicate(
            operands=tuple(parse_predicate(p) for p in _seq(m["any"], "any"))
        )
    cc = _mapping(m["cross_call"], "cross_call")
    kind = _string(cc["kind"], "cross_call.kind")
    if kind not in _CROSS_CALL_KINDS:
        raise ValueError(
            f"cross_call kind must be one of {sorted(_CROSS_CALL_KINDS)}, got {kind!r}"
        )
    return CrossCallPredicate(
        kind=cast("CrossCallKind", kind),
        field=_string(cc["field"], "cross_call.field"),
    )


def _parse_rule(d: dict[str, object]) -> Rule:
    tool = _string(d["tool"], "tool")
    severity = _severity(d.get("severity"))
    sample_raw = d.get("sample_field", _DEFAULT_SAMPLE.get(tool))
    sample_field = None if sample_raw is None else _string(sample_raw, "sample_field")
    dedup_raw = d.get("dedup")
    if dedup_raw is None:
        dedup = severity != "error"
    elif isinstance(dedup_raw, bool):
        dedup = dedup_raw
    else:
        raise ValueError(f"dedup must be a boolean, got {type(dedup_raw).__name__}")
    enabled_raw = d.get("enabled")
    if enabled_raw is None:
        enabled = True
    elif isinstance(enabled_raw, bool):
        enabled = enabled_raw
    else:
        raise ValueError(f"enabled must be a boolean, got {type(enabled_raw).__name__}")

    every_raw = d.get("every_calls")
    if every_raw is None:
        every_calls = None
    else:
        every_calls = _integer(every_raw, "every_calls")
        if every_calls < 1:
            raise ValueError(f"every_calls must be >= 1, got {every_calls}")

    return Rule(
        name=_string(d["name"], "name"),
        tool=tool,
        message=_string(d["message"], "message"),
        severity=severity,
        enabled=enabled,
        dedup=dedup,
        every_calls=every_calls,
        sample_field=sample_field,
        match=parse_predicate(d["match"]),
    )


@dataclass(frozen=True)
class LoadError:
    path: Path
    message: str


@dataclass(frozen=True)
class Catalogue:
    """Rules that parsed plus the problems met along the way, in load order."""

    rules: tuple[Rule, ...]
    errors: tuple[LoadError, ...]


def _rule_label(index: int, raw: object) -> str:
    name = cast("dict[str, object]", raw).get("name") if isinstance(raw, dict) else None
    return f"rule[{index}] ({name!r})" if isinstance(name, str) else f"rule[{index}]"


def _problem(e: Exception) -> str:
    return f"missing required key {e.args[0]!r}" if isinstance(e, KeyError) else str(e)


def parse_rules(data: bytes, path: Path) -> Catalogue:
    """Parse one TOML file, keeping every well-formed rule and recording the rest.

    A file that fails to decode yields no rules and one error. A rule that
    fails to parse yields one error and leaves its siblings intact.
    """
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
        raws = _seq(parsed.get("rule", []), "rule")
    except (ValueError, TypeError, KeyError) as e:
        return Catalogue((), (LoadError(path, _problem(e)),))
    rules: list[Rule] = []
    errors: list[LoadError] = []
    for i, raw in enumerate(raws):
        try:
            rules.append(_parse_rule(_mapping(raw, "rule")))
        except (ValueError, TypeError, KeyError) as e:
            errors.append(LoadError(path, f"{_rule_label(i, raw)}: {_problem(e)}"))
    return Catalogue(tuple(rules), tuple(errors))


def _rules_dirs() -> list[Path]:
    """Resolve the catalogue directories from env or XDG defaults.

    TOOLCHECK_RULES_DIRS holds a comma-separated list. TOOLCHECK_RULES_DIR is
    the singular predecessor, kept for backwards compatibility; when both are
    set, entries from both are used in order.
    """
    dirs: list[Path] = []
    if plural := os.environ.get("TOOLCHECK_RULES_DIRS"):
        dirs.extend(Path(s) for p in plural.split(",") if (s := p.strip()))
    if single := os.environ.get("TOOLCHECK_RULES_DIR"):
        dirs.append(Path(single))
    if dirs:
        return dirs
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return [base / "toolcheck" / "rules"]


@functools.cache
def _load_all_rules() -> Catalogue:
    """Load every *.toml across the config rules directories, in load order without deduplication.

    Load order is directory order (TOOLCHECK_RULES_DIRS left to right, then
    TOOLCHECK_RULES_DIR), then filename order within each directory. A file
    or rule that fails to parse becomes an error rather than aborting the
    load. Memoised; call cache_clear() to invalidate.
    """
    rules: list[Rule] = []
    errors: list[LoadError] = []
    for root in _rules_dirs():
        if not root.is_dir():
            continue
        toml_names = sorted(
            p.name for p in root.iterdir() if p.is_file() and p.name.endswith(".toml")
        )
        for name in toml_names:
            path = root / name
            try:
                parsed = parse_rules(path.read_bytes(), path)
            except OSError as e:
                errors.append(LoadError(path, str(e)))
                continue
            rules.extend(parsed.rules)
            errors.extend(parsed.errors)
    return Catalogue(tuple(rules), tuple(errors))


def _dedup_last_wins(rules: Iterable[Rule]) -> list[Rule]:
    """Collapse rules sharing a name, keeping the last definition in load order and giving it that position."""
    by_name: dict[str, Rule] = {}
    for rule in rules:
        by_name.pop(rule.name, None)
        by_name[rule.name] = rule
    return list(by_name.values())


def load_catalogue() -> list[Rule]:
    """Enabled rules, last-wins by name. Memoised via _load_all_rules."""
    return [r for r in _dedup_last_wins(_load_all_rules().rules) if r.enabled]


def load_errors() -> Sequence[LoadError]:
    """Files and rules that failed to parse on the last load. Memoised via _load_all_rules."""
    return _load_all_rules().errors


def load_all_rules() -> Sequence[Rule]:
    """All rules including disabled, in load order without dedup (for linting). Memoised via _load_all_rules."""
    return _load_all_rules().rules


def clear_rule_cache() -> None:
    """Invalidate the memoised catalogue (e.g. after changing TOOLCHECK_RULES_DIR)."""
    _load_all_rules.cache_clear()


def _pred_has_cross_call(pred: Predicate) -> bool:
    match pred:
        case CrossCallPredicate():
            return True
        case NotPredicate(operand=op):
            return _pred_has_cross_call(op)
        case AllPredicate(operands=ops) | AnyPredicate(operands=ops):
            return any(_pred_has_cross_call(o) for o in ops)
        case (
            RegexPredicate()
            | PresencePredicate()
            | InPredicate()
            | LengthPredicate()
            | ContextLengthPredicate()
        ):
            return False


def is_stateless(rule: Rule) -> bool:
    """True when the rule's predicate requires no session-scoped state."""
    return not _pred_has_cross_call(rule.match)


def predicate_fields(pred: Predicate) -> Iterator[str]:
    """Every field name referenced anywhere in the predicate tree."""
    match pred:
        case (
            RegexPredicate(field=f)
            | PresencePredicate(field=f)
            | InPredicate(field=f)
            | LengthPredicate(field=f)
            | CrossCallPredicate(field=f)
        ):
            yield f
        case NotPredicate(operand=op):
            yield from predicate_fields(op)
        case AllPredicate(operands=ops) | AnyPredicate(operands=ops):
            for o in ops:
                yield from predicate_fields(o)
        case ContextLengthPredicate():
            return  # ambient, references no tool-input field


def cross_call_of(rule: Rule) -> CrossCallPredicate | None:
    """Return the CrossCallPredicate when the match is a bare one (no nesting)."""
    return rule.match if isinstance(rule.match, CrossCallPredicate) else None

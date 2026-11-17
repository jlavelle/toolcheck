"""Declarative hint catalogue: schema, TOML loader, catalogue accessor.

Predicate algebra over tool-input fields. A field name is either a raw
tool_input key or, when @-prefixed, a derived field computed by a pure
extractor (see toolcheck.compile):
  RegexPredicate     — regex search on a field
  PresencePredicate  — a field is present (non-null) or absent
  InPredicate        — a field's value is one of a fixed set (equals = singleton)
  LengthPredicate    — compare a field's length to a constant
  ContextLengthPredicate — compare the session's current context length to a constant
  CwdPredicate       — regex search on the agent's working directory
  AstPredicate       — ast-grep rule over a field parsed as source code
  NotPredicate       — logical NOT
  AllPredicate       — logical AND
  AnyPredicate       — logical OR
  CrossCallPredicate — session-scoped; excluded by is_stateless() before compilation
"""

from __future__ import annotations

import functools
import os
import tomllib
from collections.abc import Iterable, Iterator, Mapping, Sequence
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


@dataclass(frozen=True)
class CwdPredicate:
    """Regex search on the agent's working directory.

    Ambient like ContextLengthPredicate. The directory is unknown when the
    payload carries no ``cwd``, and a search of an unknown directory holds False.
    """

    pattern: str


@dataclass(frozen=True)
class AstQuery:
    """An ast-grep rule config over source in ``language``.

    ``rule``, ``constraints`` and ``utils`` are the keys of an ast-grep rule
    config, kept as the plain data the TOML gave.
    """

    language: str
    rule: dict[str, object]
    constraints: dict[str, object] | None = None
    utils: dict[str, object] | None = None


@dataclass(frozen=True)
class AstPredicate:
    """The query matches somewhere in the field parsed as its language."""

    field: str
    query: AstQuery


@dataclass(frozen=True)
class View:
    """A derived field ``name`` that rules on ``tool`` can read.

    Its value is the list of strings that ``field`` yields: the field itself,
    or, with a query, each node the query matches in it, string literals read
    as their contents.
    """

    name: str
    tool: str
    field: str
    query: AstQuery | None = None


CrossCallKind = Literal["duplicate_field_within_session", "adjacent_near_identical"]


@dataclass(frozen=True)
class CrossCallPredicate:
    """Session-scoped; excluded by is_stateless() before compilation."""

    kind: CrossCallKind
    field: str  # which tool_input field to track across calls


@dataclass(frozen=True)
class PriorCallPredicate:
    """An earlier call this agent made to ``tool`` had ``value`` in ``field``.

    Ambient like ContextLengthPredicate: it reads the agent's history rather
    than the current input, so it composes with every other predicate.
    """

    tool: str
    field: str
    value: str


Predicate = (
    RegexPredicate
    | PresencePredicate
    | InPredicate
    | LengthPredicate
    | ContextLengthPredicate
    | CwdPredicate
    | AstPredicate
    | NotPredicate
    | AllPredicate
    | AnyPredicate
    | CrossCallPredicate
    | PriorCallPredicate
)


@dataclass(frozen=True)
class Rule:
    name: str
    tools: tuple[str, ...]  # "Bash", "Read", "Edit", "Grep", "mcp__*", …
    message: str  # diagnosis + remedy, from `message` or the file `message_file` names
    severity: Literal["warning", "error", "reminder"]
    dedup: bool  # True: fire at most once per (session, rule); False: fire every match
    sample_field: (
        str | None
    )  # field shown as the sample line in hint output; None: the calling tool's default
    match: Predicate
    every_calls: int | None = (
        None  # throttle: at most once per N session tool calls; supersedes dedup
    )
    enabled: bool = True  # False: rule is skipped entirely


DEFAULT_SAMPLE: dict[str, str] = {
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
        "cwd",
        "ast",
        "not",
        "all",
        "any",
        "cross_call",
        "prior_call",
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
    if "cwd" in m:
        return CwdPredicate(pattern=_string(m["cwd"], "cwd"))
    if "ast" in m:
        return _ast_predicate(_mapping(m["ast"], "ast"))
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
    if "prior_call" in m:
        pc = _mapping(m["prior_call"], "prior_call")
        return PriorCallPredicate(
            tool=_string(pc["tool"], "prior_call.tool"),
            field=_string(pc["field"], "prior_call.field"),
            value=_string(pc["value"], "prior_call.value"),
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


def _optional_mapping(x: object, ctx: str) -> dict[str, object] | None:
    return None if x is None else _mapping(x, ctx)


def _unknown_keys(d: dict[str, object], known: set[str], ctx: str) -> None:
    unknown = set(d) - known
    if unknown:
        raise ValueError(f"unknown {ctx} keys: {sorted(unknown)!r}")


_QUERY_KEYS = {"language", "rule", "constraints", "utils"}


def _ast_query(a: dict[str, object], ctx: str) -> AstQuery:
    return AstQuery(
        language=_string(a["language"], f"{ctx}.language"),
        rule=_mapping(a["rule"], f"{ctx}.rule"),
        constraints=_optional_mapping(a.get("constraints"), f"{ctx}.constraints"),
        utils=_optional_mapping(a.get("utils"), f"{ctx}.utils"),
    )


def _ast_predicate(a: dict[str, object]) -> AstPredicate:
    _unknown_keys(a, {"field"} | _QUERY_KEYS, "ast")
    return AstPredicate(
        field=_string(a["field"], "ast.field"), query=_ast_query(a, "ast")
    )


def _view(d: dict[str, object]) -> View:
    _unknown_keys(d, {"name", "tool", "field"} | _QUERY_KEYS, "view")
    name = _string(d["name"], "view.name")
    if not name.startswith("@"):
        raise ValueError(f"view name must start with '@', got {name!r}")
    query = _ast_query(d, "view") if "language" in d or "rule" in d else None
    if query is not None:
        from toolcheck import syntax

        syntax.check(query)
    return View(
        name=name,
        tool=_string(d["tool"], "view.tool"),
        field=_string(d["field"], "view.field"),
        query=query,
    )


def _tools(x: object) -> tuple[str, ...]:
    if isinstance(x, str):
        return (x,)
    tools = tuple(_string(t, "tool[]") for t in _seq(x, "tool"))
    if not tools:
        raise ValueError("tool must name at least one tool")
    return tools


def _parse_rule(d: dict[str, object], rules_dir: Path) -> Rule:
    tools = _tools(d["tool"])
    severity = _severity(d.get("severity"))
    sample_raw = d.get("sample_field")
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
        tools=tools,
        message=_message(d, rules_dir),
        severity=severity,
        enabled=enabled,
        dedup=dedup,
        every_calls=every_calls,
        sample_field=sample_field,
        match=parse_predicate(d["match"]),
    )


def _message(d: dict[str, object], rules_dir: Path) -> str:
    """The rule's message, read from `message_file` relative to the rule's file when set."""
    if "message_file" not in d:
        return _string(d["message"], "message")
    if "message" in d:
        raise ValueError("set message or message_file, not both")
    path = Path(_string(d["message_file"], "message_file")).expanduser()
    if not path.is_absolute():
        path = rules_dir / path
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ValueError(f"message_file {str(path)!r}: {e.strerror}") from e
    except UnicodeDecodeError as e:
        raise ValueError(f"message_file {str(path)!r} is not UTF-8") from e


@dataclass(frozen=True)
class LoadError:
    path: Path
    message: str


@dataclass(frozen=True)
class Catalogue:
    """Rules and views that parsed plus the problems met along the way, in load order."""

    rules: tuple[Rule, ...]
    errors: tuple[LoadError, ...]
    views: tuple[View, ...] = ()


def _item_label(kind: str, index: int, raw: object) -> str:
    name = cast("dict[str, object]", raw).get("name") if isinstance(raw, dict) else None
    return (
        f"{kind}[{index}] ({name!r})" if isinstance(name, str) else f"{kind}[{index}]"
    )


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
        raw_views = _seq(parsed.get("view", []), "view")
    except (ValueError, TypeError, KeyError) as e:
        return Catalogue((), (LoadError(path, _problem(e)),))
    rules: list[Rule] = []
    views: list[View] = []
    errors: list[LoadError] = []
    for i, raw in enumerate(raws):
        try:
            rules.append(_parse_rule(_mapping(raw, "rule"), path.parent))
        except (ValueError, TypeError, KeyError) as e:
            errors.append(
                LoadError(path, f"{_item_label('rule', i, raw)}: {_problem(e)}")
            )
    for i, raw in enumerate(raw_views):
        try:
            views.append(_view(_mapping(raw, "view")))
        except (ValueError, TypeError, KeyError) as e:
            errors.append(
                LoadError(path, f"{_item_label('view', i, raw)}: {_problem(e)}")
            )
    return Catalogue(tuple(rules), tuple(errors), tuple(views))


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
    views: list[View] = []
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
            views.extend(parsed.views)
            errors.extend(parsed.errors)
    return Catalogue(tuple(rules), tuple(errors), tuple(views))


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


Views = Mapping[str, Mapping[str, View]]
"""Views by tool, then by name."""


def views_by_tool(views: Iterable[View]) -> dict[str, dict[str, View]]:
    """Index views by tool and name, a later view replacing an earlier one."""
    index: dict[str, dict[str, View]] = {}
    for view in views:
        index.setdefault(view.tool, {})[view.name] = view
    return index


@functools.cache
def load_views() -> Views:
    """Views from every rules file, last-wins per tool and name. Memoised with the catalogue."""
    return views_by_tool(_load_all_rules().views)


def load_all_views() -> Sequence[View]:
    """All views in load order, for linting. Memoised with the catalogue."""
    return _load_all_rules().views


def load_errors() -> Sequence[LoadError]:
    """Files and rules that failed to parse on the last load. Memoised via _load_all_rules."""
    return _load_all_rules().errors


def load_all_rules() -> Sequence[Rule]:
    """All rules including disabled, in load order without dedup (for linting). Memoised via _load_all_rules."""
    return _load_all_rules().rules


def clear_rule_cache() -> None:
    """Invalidate the memoised catalogue (e.g. after changing TOOLCHECK_RULES_DIR)."""
    _load_all_rules.cache_clear()
    load_views.cache_clear()


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
            | CwdPredicate()
            | AstPredicate()
            | PriorCallPredicate()
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
            | AstPredicate(field=f)
            | CrossCallPredicate(field=f)
        ):
            yield f
        case NotPredicate(operand=op):
            yield from predicate_fields(op)
        case AllPredicate(operands=ops) | AnyPredicate(operands=ops):
            for o in ops:
                yield from predicate_fields(o)
        case ContextLengthPredicate() | CwdPredicate() | PriorCallPredicate():
            return  # ambient, references no field of this rule's tool input


def prior_calls(pred: Predicate) -> Iterator[PriorCallPredicate]:
    """Every PriorCallPredicate in the predicate tree."""
    match pred:
        case PriorCallPredicate():
            yield pred
        case NotPredicate(operand=op):
            yield from prior_calls(op)
        case AllPredicate(operands=ops) | AnyPredicate(operands=ops):
            for o in ops:
                yield from prior_calls(o)
        case (
            RegexPredicate()
            | PresencePredicate()
            | InPredicate()
            | LengthPredicate()
            | ContextLengthPredicate()
            | CwdPredicate()
            | AstPredicate()
            | CrossCallPredicate()
        ):
            return


def cross_call_of(rule: Rule) -> CrossCallPredicate | None:
    """Return the CrossCallPredicate when the match is a bare one (no nesting)."""
    return rule.match if isinstance(rule.match, CrossCallPredicate) else None

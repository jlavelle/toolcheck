"""toolcheck — TOML-driven linter for LLM tool calls.

  toolcheck list [--tool TOOL] [--json]
      Enumerate the catalogue.

  toolcheck check [FILE] [--tool TOOL] [--claude] [--json]
      Evaluate a tool-call JSON against the catalogue and report which rules
      fire. The input is read from FILE, or stdin when FILE is omitted, and
      may be either a bare {"tool_name", "tool_input"} object or a full
      PreToolUse payload (any extra keys are ignored).

      --claude enables Claude PreToolUse hook behaviour: emits hints as
      additionalContext, denies calls that match error-severity rules,
      deduplicates once per (session, rule), and evaluates cross-call rules
      (which need per-session history to fire).

  toolcheck lint-rules
      Validate every rule in the catalogue: compile stateless predicates,
      resolve derived field names, reject nested cross-call matches.

Without --claude, cross-call rules are skipped and output is plain text.

Exit codes: 0 — clean (and always for --claude, which reports via the hook
envelope); 1 — rules fired (check) or problems found (lint-rules); 2 — usage
error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Mapping, cast

from toolcheck.compile import Env, compile_rule, known_derived_fields, resolve_field
from toolcheck.crosscall import evaluate as eval_cross_call
from toolcheck.crosscall import field_value
from toolcheck.schema import (
    Rule,
    cross_call_of,
    is_stateless,
    load_all_rules,
    load_catalogue,
    predicate_fields,
)
from toolcheck.state import SessionState, load_state, save_state, state_path
from toolcheck.transcript import read_context_length


def _as_str(x: object) -> str | None:
    return x if isinstance(x, str) else None


def _dedup_disabled() -> bool:
    return os.environ.get("TOOLCHECK_NO_DEDUP", "") not in ("", "0", "false")


def render(rule: Rule, tool_input: Mapping[str, object]) -> str:
    """Two-line hint: ``[name] <message>\\n> <sample>``.

    Message first (actionable up front); sample on its own line so multi-line
    inputs (heredocs, etc.) lay out naturally.
    """
    sample_str = _sample(rule, tool_input)
    if sample_str:
        return f"[{rule.name}] {rule.message}\n> {sample_str}"
    return f"[{rule.name}] {rule.message}"


def _sample(rule: Rule, tool_input: Mapping[str, object]) -> str:
    if rule.sample_field is None:
        return ""
    try:
        sample = resolve_field(rule.sample_field, tool_input)
    except ValueError:
        return ""
    return str(sample) if sample is not None else ""


def _eval_stateless(rule: Rule, env: Env) -> bool:
    """Compile and run a single stateless rule; False on any exception."""
    try:
        matcher = compile_rule(rule)
    except Exception:
        return False
    try:
        return matcher(env)
    except Exception:
        return False


def evaluate_call(
    tool_name: str,
    tool_input: Mapping[str, object],
    state: SessionState,
    use_dedup: bool,
    now: str,
    rules: list[Rule],
    context_length: int | None = None,
) -> tuple[list[Rule], SessionState]:
    """Evaluate every rule for the tool against the input and prior state.

    Returns the rules that fired and the updated state (new fired entries for
    stateless rules, plus recorded observations for cross-call fields). Pure:
    no I/O, deterministic given its arguments. ``context_length`` is the
    current session context length in tokens, or None when unknown.
    """
    rules = [r for r in rules if r.tool == tool_name]
    env = Env(tool_input, context_length)
    fired: list[Rule] = []
    next_state = state

    for rule in rules:
        cc = cross_call_of(rule)
        if cc is not None:
            try:
                if eval_cross_call(cc, tool_name, tool_input, state):
                    fired.append(rule)
            except Exception:
                pass
            continue

        if not is_stateless(rule):
            continue  # nested cross-call: unsupported, skip defensively
        if use_dedup and rule.dedup and next_state.has_fired(rule.name):
            continue
        if _eval_stateless(rule, env):
            fired.append(rule)
            if use_dedup and rule.dedup:
                next_state = next_state.with_fired(rule.name, now)

    observed: set[tuple[str, str]] = set()  # two rules on one field: record once
    for rule in rules:
        cc = cross_call_of(rule)
        if cc is None:
            continue
        value = field_value(cc, tool_input)
        if value is not None and (cc.field, value) not in observed:
            observed.add((cc.field, value))
            next_state = next_state.with_observation(tool_name, cc.field, value)

    return fired, next_state


def _cmd_list(args: argparse.Namespace) -> int:
    tool_filter = cast("str | None", args.tool)
    source = load_all_rules() if args.all else load_catalogue()
    rules = [r for r in source if tool_filter is None or r.tool == tool_filter]
    rules.sort(key=lambda r: (r.tool, r.name))

    if args.json:
        print(json.dumps([_rule_dict(r) for r in rules], indent=2))
        return 0

    if not rules:
        print("(no rules match)", file=sys.stderr)
        return 0
    for r in rules:
        kind = "stateless" if is_stateless(r) else "cross-call"
        status = " [disabled]" if not r.enabled else ""
        print(f"{r.tool:8} {r.name:28} {r.severity:8} {kind:10} {r.message}{status}")
    return 0


def _rule_dict(r: Rule) -> dict[str, object]:
    return {
        "name": r.name,
        "tool": r.tool,
        "severity": r.severity,
        "enabled": r.enabled,
        "stateless": is_stateless(r),
        "message": r.message,
    }


def _read_input(args: argparse.Namespace) -> dict[str, object]:
    raw = sys.stdin.read() if args.file is None else _read_file(cast("str", args.file))
    data = cast("object", json.loads(raw))
    if not isinstance(data, dict):
        raise ValueError("input must be a JSON object")
    return cast("dict[str, object]", data)


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _cmd_check(args: argparse.Namespace) -> int:
    try:
        payload = _read_input(args)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) and args.tool is None:
        print("error: input lacks 'tool_name'; pass --tool", file=sys.stderr)
        return 2
    tool = cast("str", args.tool) if args.tool is not None else cast("str", tool_name)

    tool_input_raw = payload.get("tool_input", payload)
    if not isinstance(tool_input_raw, dict):
        print("error: 'tool_input' must be an object", file=sys.stderr)
        return 2
    tool_input = cast("dict[str, object]", tool_input_raw)

    if args.claude:
        return _check_claude(payload, tool, tool_input)

    fired, _ = evaluate_call(
        tool,
        tool_input,
        SessionState(),
        use_dedup=False,
        now="",
        rules=load_catalogue(),
    )

    if args.json:
        print(json.dumps([_rule_dict(r) for r in fired], indent=2))
        return 1 if fired else 0

    if not fired:
        print("(no rules fired)")
        return 0
    for r in fired:
        print(render(r, tool_input))
    return 1


def _check_claude(
    payload: dict[str, object],
    tool: str,
    tool_input: dict[str, object],
) -> int:
    session_id = _as_str(payload.get("session_id"))
    has_session = bool(session_id)
    use_dedup = has_session and not _dedup_disabled()

    path = state_path(session_id) if has_session else None  # type: ignore[arg-type]
    state = load_state(path) if path is not None else SessionState()

    transcript_path = _as_str(payload.get("transcript_path"))
    context_length = read_context_length(transcript_path) if transcript_path else None

    now = datetime.now(timezone.utc).isoformat()
    catalogue = load_catalogue()
    fired, next_state = evaluate_call(
        tool, tool_input, state, use_dedup, now, catalogue, context_length
    )

    if path is not None:
        save_state(path, next_state)

    if not fired:
        return 0

    warnings = [r for r in fired if r.severity == "warning"]
    errors = [r for r in fired if r.severity == "error"]

    output: dict[str, object] = {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}
    spec = cast("dict[str, object]", output["hookSpecificOutput"])

    if warnings:
        spec["additionalContext"] = "\n\n".join(render(r, tool_input) for r in warnings)

    if errors:
        spec["permissionDecision"] = "deny"
        spec["permissionDecisionReason"] = "\n\n".join(
            render(r, tool_input) for r in errors
        )

    print(json.dumps(output))
    return 0


def lint_rule(rule: Rule) -> list[str]:
    """Authoring mistakes that would make the rule silently never fire."""
    issues: list[str] = []
    if not is_stateless(rule) and cross_call_of(rule) is None:
        issues.append("cross_call must be the entire match (nesting is unsupported)")
    elif is_stateless(rule):
        try:
            compile_rule(rule)
        except Exception as e:
            issues.append(f"does not compile: {e}")
    known = known_derived_fields()
    fields = set(predicate_fields(rule.match))
    if rule.sample_field is not None:
        fields.add(rule.sample_field)
    for f in sorted(fields):
        if f.startswith("@") and f not in known:
            issues.append(f"unknown derived field: {f!r}")
    return issues


def lint_rules(rules: list[Rule]) -> list[str]:
    """Catalogue-wide problems, one human-readable line each."""
    problems = [
        f"duplicate rule name: {name!r} ({n} definitions)"
        for name, n in sorted(Counter(r.name for r in rules).items())
        if n > 1
    ]
    for rule in rules:
        problems.extend(f"{rule.name}: {issue}" for issue in lint_rule(rule))
    return problems


def _cmd_lint_rules(args: argparse.Namespace) -> int:
    rules = load_all_rules()
    problems = lint_rules(rules)
    if not problems:
        print(f"ok: {len(rules)} rules")
        return 0
    for p in problems:
        print(p, file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toolcheck", description="Lint LLM tool calls."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="enumerate the rule catalogue")
    p_list.add_argument("--tool", help="filter by tool name (e.g. Bash)")
    p_list.add_argument("--all", action="store_true", help="include disabled rules")
    p_list.add_argument("--json", action="store_true", help="emit JSON")
    p_list.set_defaults(func=_cmd_list)

    p_check = sub.add_parser(
        "check", help="evaluate a tool-call JSON against the catalogue"
    )
    p_check.add_argument("file", nargs="?", help="JSON file; stdin when omitted")
    p_check.add_argument("--tool", help="override/supply the tool name")
    p_check.add_argument(
        "--claude", action="store_true", help="emit Claude PreToolUse hook output"
    )
    p_check.add_argument("--json", action="store_true", help="emit JSON")
    p_check.set_defaults(func=_cmd_check)

    p_lint = sub.add_parser("lint-rules", help="validate every rule in the catalogue")
    p_lint.set_defaults(func=_cmd_lint_rules)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    func = cast("Callable[[argparse.Namespace], int]", args.func)
    sys.exit(func(args))


if __name__ == "__main__":
    main()

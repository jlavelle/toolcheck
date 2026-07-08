# toolcheck

A linter for LLM tool calls. Run `toolcheck check` against tool call JSON on stdin. With `--claude` it emits the Claude PreToolUse hook envelope, injecting hints as `additionalContext` and refusing error-severity rules with `"permissionDecision": "deny"`.

## Install

```sh
uv tool install .
```

## Claude Code hook configuration

After installing on your PATH, add to Claude Code's `settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      { "hooks": [ { "type": "command", "command": "toolcheck check --claude" } ] }
    ]
  }
}
```

With `--claude`, each `(session, rule)` pair fires at most once per session when `dedup = true` (the default for warnings). Error-severity rules default to `dedup = false` so they block every occurrence. Set `TOOLCHECK_NO_DEDUP=1` to disable dedup globally, which can be useful while you're developing rules.

## Running manually

Pipe JSON over stdin:

```sh
echo '{"tool_name":"Bash","tool_input":{"command":"rg foo | wc -l"}}' | toolcheck check
# [rg-pipe-wc] `rg ... | wc -l` — use `rg -c` (or `--count-matches`)
# > rg foo | wc -l
```

Exit codes: 0 — no rules fired; 1 — at least one rule fired; 2 — usage error. The `--claude` hook path always exits 0 and reports through the hook envelope instead.

## Rules

Rules are TOML files in `~/.config/toolcheck/rules/`. Override the location with `$TOOLCHECK_RULES_DIRS`, a comma-separated list of directories (`$TOOLCHECK_RULES_DIR`, singular, is still honoured for backwards compatibility). See the `examples/` for ideas to get started.

Rules sharing a name are deduplicated last-wins, so a later directory can override an earlier one's rule. Load order is directory order (`$TOOLCHECK_RULES_DIRS` left to right, then `$TOOLCHECK_RULES_DIR`), then filename order within each directory.

Browse active rule names:

```sh
toolcheck list
```

Include disabled rules with `--all`.

Validate the catalogue after editing — `toolcheck lint-rules` compiles every stateless rule, resolves derived field names, and rejects nested cross-call matches. It exits 1 and prints one line per problem when anything is broken.

## Rule format

A rule names the tool it inspects, the hint to show, and a `match` predicate. Start from `examples/bash.toml` (`grep.toml`, `read.toml`, and `write.toml` cover the other tools):

```toml
[[rule]]
name     = "rg-pipe-wc"    # Must be unique
tool     = "Bash"          # Must match the name of the tool the model calls
message  = "`rg ... | wc -l` — use `rg -c` (or `--count-matches`)"
severity = "warning"       # "warning" | "error"
dedup    = true            # optional: fire once per (session, rule), default true for warnings, false for errors
enabled  = true            # optional: set false to disable without removing, default true
[rule.match]
regex = { field = "command", pattern = '\brg\b[^|]*\|\s*wc\s+-l\b' }
```

An optional `sample_field` names the tool-input field echoed as the `> sample` second line of the hint (derived `@`-fields work too). It defaults per tool — Bash: `command`; Read/Edit/Write: `file_path`; Grep/Glob: `pattern`; Agent: `description` — and to no sample line for other tools.

### Predicates

A predicate tests a `field` of the tool input. Compose them freely with `not`, `all`, and `any` — except `cross_call`, which composes with nothing and must be the entire `match`.

| Predicate | TOML shape                                                             | Holds when                                |
|-----------|------------------------------------------------------------------------|-------------------------------------------|
| regex     | `{ regex = { field = "command", pattern = '…' } }`                     | the pattern matches the field (see below) |
| present   | `{ present = "path" }`                                                 | the field is set                          |
| absent    | `{ absent = "path" }`                                                  | the field is missing                      |
| equals    | `{ equals = { field = "@last_pipe_command", value = "head" } }`        | the field equals the value                |
| in        | `{ in = { field = "@last_pipe_command", values = ["head", "tail"] } }` | the field is one of the values            |
| length    | `{ length = { field = "content", op = ">", value = 1500 } }`           | the field's length compares true          |
| context_length | `{ context_length = { op = ">", value = 120000 } }`               | the session's context length compares true (see below) |
| not       | `{ not = <predicate> }`                                                | the inner predicate fails                 |
| all       | `{ all = [ <predicate>, … ] }`                                         | every operand holds                       |
| any       | `{ any = [ <predicate>, … ] }`                                         | some operand holds                        |

#### Length ops

The `length` predicate's `op` field accepts: `>`, `>=`, `<`, `<=`, `==`, `!=`.

#### Context length

`context_length` compares the session's current context length, in tokens, against a constant with the same ops as `length`. Unlike the others it tests no field — the value is ambient, so it composes freely with `not`/`all`/`any` but takes no `field`.

The PreToolUse payload carries no token count. `toolcheck` estimates it by summing the token counts (`input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`) in the last usage-bearing entry of the transcript named by `transcript_path`. Two consequences:

- The value is only available under `toolcheck check --claude`. Under plain `check` it is unknown.
- It is unknown on the first call of a session, and the transcript is written asynchronously so it may lag the current turn by a message.

A comparison against an unknown context length holds `False` — so `context_length > N` stays quiet until the number is known, and `not { context_length > N }` holds while unknown.

For example, nudge on a broad read once the window is well-filled:

```toml
[[rule]]
name    = "read-wide-when-full"
tool    = "Read"
message = "context is large — read a slice with offset/limit rather than the whole file"
[rule.match]
all = [
    { context_length = { op = ">", value = 120000 } },
    { absent = "limit" },
]
```

### Regex engine

Patterns use Python's `re` module with `search` semantics. See the [documentation](https://docs.python.org/3/library/re.html) for more info.

### Builtin derived fields

A field starting with `@` is computed from the tool input. These are hardcoded in `toolcheck` and there is currently no way to extend it without modifying the code.

Available derived fields:

- `@last_pipe_command` — the command name in the rightmost pipe segment (`rg foo | head -5` → `head`).

Example — head/tail truncation with no `tee`:

```toml
[rule.match]
all = [
    { in = { field = "@last_pipe_command", values = ["head", "tail"] } },
    { not = { regex = { field = "command", pattern = '(^|\s)tee\s' } } },
]
```

### Cross-call checks

Special builtin stateful checks that detect relationships between tool calls within a session. Like derived fields, these are built into `toolcheck` and not currently extensible. A `cross_call` cannot be nested under `not`/`all`/`any`; it must be the whole `match`:

```toml
[rule.match]
cross_call = { kind = "duplicate_field_within_session", field = "file_path" }
```

- `duplicate_field_within_session` — the field value was already seen in this session.
- `adjacent_near_identical` — the field value nearly matches the previous call.

Cross-call rules fire every recurrence (not subject to dedup) and need session state; only `toolcheck check --claude` evaluates them.

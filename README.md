# toolcheck

A linter for LLM tool calls. Run `toolcheck check` against tool call JSON on stdin. With `--claude` it emits the Claude PreToolUse hook envelope, injecting warning and reminder hints as `additionalContext` and refusing error-severity rules with `"permissionDecision": "deny"`.

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

With `--claude`, each `(session, rule)` pair fires at most once per session when `dedup = true` (the default for warnings and reminders). Error-severity rules default to `dedup = false` so they block every occurrence. Set `TOOLCHECK_NO_DEDUP=1` to disable dedup globally, which can be useful while you're developing rules.

## pi support

The same rule catalogue can lint tool calls under [pi](https://github.com/earendil-works/pi): `toolcheck check --pi` speaks a toolcheck-defined envelope, and a thin pi extension spawns it per tool call.

Install by copying the extension to pi's global extension directory:

```sh
cp pi/toolcheck.ts ~/.pi/agent/extensions/
```

(For a quick test without installing: `pi -e pi/toolcheck.ts`.)

On each tool call the extension runs `toolcheck check --pi` with the call's input, the session id, and the current context length. Error-severity rules block the call; warnings and reminders are appended to the tool result as model-visible text. If `toolcheck` is missing, times out, or misbehaves, the extension allows the call and notifies once per session.

Calls a tool makes through `ctx.executeTool()`, such as those a `codemode` script issues, are checked the same way. They have no tool result of their own, so their hints are appended to the parent call's result, along with the reason for each blocked nested call, since a script can catch the error and hide it from the model. Each nested call spawns `toolcheck` once (about 80 ms).

### Envelope contract

Input (stdin): `{"tool_name": "read", "tool_input": {…}, "session_id": "…", "context_length": 12345}` — `session_id` and `context_length` optional; `context_length` must be an integer when present. Output (stdout, always exit 0):

```json
{
  "decision": "allow" | "deny",
  "warnings": ["[rule] msg\n> sample"],
  "reminders": ["[rule] msg\n> sample"],
  "errors": ["[rule] msg\n> sample"]
}
```

Reminders are non-blocking context delivered exactly like warnings, and the `reminders` key is always present (empty when none). `decision` is `"deny"` iff an error-severity rule fired. Dedup and cross-call state work exactly as under `--claude`, stored under `~/.local/state/pi/toolcheck/` so pi and Claude Code sessions never collide. `context_length` is taken straight from the payload (`ctx.getContextUsage()`), not estimated from a transcript.

### Vocabulary normalisation

pi tool names and input keys are mapped to the canonical Claude-shaped vocabulary before evaluation; unmapped names and keys pass through untouched, so pi-specific rules can target `find`, `ls`, or custom tools by pi name.

| pi | canonical | field mapping |
|---|---|---|
| `bash` | `Bash` | none (`command` matches) |
| `read` | `Read` | `path` → `file_path`; `offset`/`limit` pass through |
| `write` | `Write` | `path` → `file_path` |
| `edit` | `Edit` | `path` → `file_path`; `edits` passes through, and its `newText` values are joined into a synthetic `new_string` |
| `grep` | `Grep` | none (`pattern` matches) |
| anything else | unchanged | unchanged |

The synthetic `new_string` means a rule written against Claude's single-string edit fires for pi's list-of-edits too. It is `"\n".join(newText for each edit)` and is only added when no `new_string` is already present. Rules on `old_string` still won't fire: pi's `edit` exposes only the per-edit `oldText`, which is not joined. For cross-tool coverage of removed text, target `file_path` (mapped from `path`) instead.

The same applies to `cross_call` fields: observations are recorded under the canonical field name after normalisation, so `cross_call = { field = "path" }` on `Read`/`Write`/`Edit` reads nothing and silently never fires. Target `file_path`. `toolcheck lint-rules` will not flag it — cross-call fields aren't validated (only `@`-derived fields are).

## Running manually

Pipe JSON over stdin:

```sh
echo '{"tool_name":"Bash","tool_input":{"command":"rg foo | wc -l"}}' | toolcheck check
# [rg-pipe-wc] `rg ... | wc -l` — use `rg -c` (or `--count-matches`)
# > rg foo | wc -l
```

Exit codes: 0 — no rules fired; 1 — at least one rule fired; 2 — usage error. The `--claude` hook path always exits 0 and reports through the hook envelope instead.

Cross-call rules fire every recurrence (not subject to dedup) and need session state; only `toolcheck check --claude` and `toolcheck check --pi` evaluate them.

## Rules

Rules are TOML files in `~/.config/toolcheck/rules/`. Override the location with `$TOOLCHECK_RULES_DIRS`, a comma-separated list of directories (`$TOOLCHECK_RULES_DIR`, singular, is still honoured for backwards compatibility). See the `examples/` for ideas to get started.

`skills/toolcheck/` is an agent skill that teaches Claude Code and pi how to write and test rules. Symlink it into your skills directory:

```sh
ln -s "$PWD/skills/toolcheck" ~/.claude/skills/toolcheck
```

Rules sharing a name are deduplicated last-wins, so a later directory can override an earlier one's rule. Load order is directory order (`$TOOLCHECK_RULES_DIRS` left to right, then `$TOOLCHECK_RULES_DIR`), then filename order within each directory.

Browse active rule names:

```sh
toolcheck list
```

Include disabled rules with `--all`.

Validate the catalogue after editing — `toolcheck lint-rules` compiles every stateless rule, resolves derived field names, and rejects nested cross-call matches. It exits 1 and prints one line per problem when anything is broken.

A file or rule that fails to parse is skipped, so the rest of the catalogue keeps running. The hooks tell the model which item was skipped and why, once per session (in `additionalContext` under `--claude`, in `warnings` under `--pi`); `list` and plain `check` print it on stderr, and `lint-rules` lists it as a problem.

## Rule format

A rule names the tool it inspects, the hint to show, and a `match` predicate. Start from `examples/bash.toml` (`grep.toml`, `read.toml`, and `write.toml` cover the other tools):

```toml
[[rule]]
name     = "rg-pipe-wc"    # Must be unique
tool     = "Bash"          # Must match the name of the tool the model calls
message  = "`rg ... | wc -l` — use `rg -c` (or `--count-matches`)"
severity = "warning"       # "warning" | "error" | "reminder"
dedup    = true            # optional: fire once per (session, rule), default true for warnings and reminders, false for errors
enabled  = true            # optional: set false to disable without removing, default true
[rule.match]
regex = { field = "command", pattern = '\brg\b[^|]*\|\s*wc\s+-l\b' }
```

An optional `sample_field` names the tool-input field echoed as the `> sample` second line of the hint (derived `@`-fields work too). It defaults per tool — Bash: `command`; Read/Edit/Write: `file_path`; Grep/Glob: `pattern`; Agent: `description` — and to no sample line for other tools. `reminder` is non-blocking like `warning` but frames the note as context rather than a problem — that framing lives in the `message` text, not in any prefix toolcheck adds.

### Predicates

A predicate tests a `field` of the tool input. Compose them freely with `not`, `all`, and `any` — except `cross_call`, which composes with nothing and must be the entire `match`.

| Predicate | TOML shape                                                             | Holds when                                |
|-----------|------------------------------------------------------------------------|-------------------------------------------|
| regex     | `{ regex = { field = "command", pattern = '…' } }`                     | the pattern matches the field (see below) |
| present   | `{ present = "path" }`                                                 | the field is set                          |
| absent    | `{ absent = "path" }`                                                  | the field is missing                      |
| equals    | `{ equals = { field = "@last_pipe_command", value = "head" } }`        | the field equals the value                |
| in        | `{ in = { field = "@last_pipe_command", values = ["head", "tail"] } }` | the field is one of the values            |
| length    | `{ length = { field = "content", op = ">", value = 1500 } }`           | the field's length (characters of a string, items of a list or object) compares true |
| context_length | `{ context_length = { op = ">", value = 120000 } }`               | the session's context length compares true (see below) |
| not       | `{ not = <predicate> }`                                                | the inner predicate fails                 |
| all       | `{ all = [ <predicate>, … ] }`                                         | every operand holds                       |
| any       | `{ any = [ <predicate>, … ] }`                                         | some operand holds                        |

#### Length ops

The `length` predicate's `op` field accepts: `>`, `>=`, `<`, `<=`, `==`, `!=`.

#### Non-string fields

`regex`, `equals`, and `in` see a non-string value as its JSON text: booleans are `true`/`false`, numbers their decimal form, lists and objects JSON. `{ equals = { field = "replace_all", value = "true" } }` matches an `Edit` with `replace_all` set.

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

Cross-call rules fire every recurrence (not subject to dedup) and need session state; only `toolcheck check --claude` and `toolcheck check --pi` evaluate them.

# toolcheck

A linter for LLM tool calls. Run `toolcheck check` against tool call JSON on stdin. With `--claude` it emits the Claude PreToolUse hook envelope, injecting warning and reminder hints as `additionalContext` and refusing error-severity rules with `"permissionDecision": "deny"`.

## Install

```sh
uv tool install .
```

With Nix, run it from the flake or add `packages.<system>.toolcheck` to your profile or home-manager config:

```sh
nix run github:jlavelle/toolcheck -- check --claude
```

## Development

`nix develop` gives a shell with Python, uv, just, ruff, Biome, bun and Node. `just check` runs pyright, ruff, and Biome over the pi extension. In it, run the tests and checks:

```sh
uv run pytest
just check
bun test pi/toolcheck.test.ts
```

`nix build` builds the package and runs the tests, except the two hook tests, which run the CLI through `uv run` and need the network.

## Claude Code hook configuration

After installing on your PATH, add to Claude Code's `settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      { "hooks": [ { "type": "command", "command": "toolcheck check --claude" } ] }
    ],
    "SessionStart": [
      { "matcher": "compact", "hooks": [ { "type": "command", "command": "toolcheck reset --claude" } ] }
    ]
  }
}
```

With `--claude`, each rule fires at most once per agent when `dedup = true` (the default for warnings and reminders). The main session and each subagent, which Claude Code names by the payload's `agent_id`, keep separate state, because a subagent starts from its own context and has not seen the hints its parent got.

Claude Code adds a hint after the call that triggered it has run. A hint about that call itself, such as how to write a commit message, needs `severity = "error"` with `dedup = true`, which refuses the first matching call so the model reads the hint and retries.

A compaction drops the hints the model was given. `toolcheck reset --claude` reads the `SessionStart` payload and lets every rule fire again, keeping cross-call history. Claude Code 2.1.316 sends the same `SessionStart`, `PreCompact` and `PostCompact` payloads, with no `agent_id`, whether the main session or a subagent compacted, so a reset without an `agent_id` clears the main agent and every subagent of the session. An agent that did not compact sees its hints once more. Error-severity rules default to `dedup = false` so they block every occurrence. A rule with `every_calls = N` instead fires at most once per N tool calls, repeating, and ignores `dedup`. Set `TOOLCHECK_NO_DEDUP=1` to disable dedup and throttling globally, which can be useful while you're developing rules.

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

Input (stdin): `{"tool_name": "read", "tool_input": {…}, "session_id": "…", "context_length": 12345, "cwd": "/home/me/project"}` — `session_id`, `context_length` and `cwd` optional; `context_length` must be an integer and `cwd` a string when present. Output (stdout, always exit 0):

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

Cross-call rules fire every recurrence (not subject to dedup or every_calls) and need session state; only `toolcheck check --claude` and `toolcheck check --pi` evaluate them.

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
tool     = "Bash"          # The tool the model calls, or a list such as ["Bash", "mcp__x__run"]
message  = "`rg ... | wc -l` — use `rg -c` (or `--count-matches`)"
severity = "warning"       # "warning" | "error" | "reminder"
dedup    = true            # optional: fire once per (session, rule), default true for warnings and reminders, false for errors
every_calls = 10           # optional: fire at most once per N session tool calls; supersedes dedup
enabled  = true            # optional: set false to disable without removing, default true
[rule.match]
regex = { field = "command", pattern = '\brg\b[^|]*\|\s*wc\s+-l\b' }
```

An optional `every_calls` throttles a repeating reminder to at most one delivery per N tool calls in the session, measured against a counter of every tool call the hook observes. A rule with `every_calls` fires on the first match, then not again until N further calls have elapsed, and ignores `dedup`. It needs session-scoped state, so it throttles only under `--claude` and `--pi`; plain `check` (no session) fires every match. Like dedup, `TOOLCHECK_NO_DEDUP=1` disables it.

`message_file` can replace `message` with a path to a text file whose contents are the message, resolved relative to the rule's TOML file when it is not absolute. It suits a longer hint, such as a document the model should read before using a tool. A missing file makes the rule a load error.

An optional `sample_field` names the tool-input field echoed as the `> sample` second line of the hint (derived `@`-fields and views work too, a view's values one per line). It defaults by the tool of the call — Bash: `command`; Read/Edit/Write: `file_path`; Grep/Glob: `pattern`; Agent: `description` — and to no sample line for other tools. `reminder` is non-blocking like `warning` but frames the note as context rather than a problem — that framing lives in the `message` text, not in any prefix toolcheck adds.

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
| prior_call | `{ prior_call = { tool = "Skill", field = "skill", value = "ghci" } }` | an earlier call by this agent to the tool had the value in the field (see below) |
| cwd       | `{ cwd = '/work/' }`                                                   | the pattern matches the agent's working directory (see below) |
| ast       | `{ ast = { field = "command", language = "bash", rule = { … } } }`     | the ast-grep rule matches the field parsed as code (see below) |
| command   | `{ command = { field = "command", name = "gh", args = ["pr", "create"] } }` | the field, read as bash, runs the command (see below) |
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

#### Prior calls

`prior_call` holds when an earlier call by the same agent, to the tool it names, had exactly `value` in `field`. Like `context_length` it reads no field of the current call, so it composes freely with `not`/`all`/`any`, and its tool can differ from the rule's. Its main use is refusing a command until the agent has loaded the skill that covers it:

```toml
[[rule]]
name     = "ghci-skill"
tool     = "Bash"
message  = "load the ghci skill before running ghci — then retry this command"
severity = "error"
[rule.match]
all = [
    { regex = { field = "command", pattern = '(^|&&|;|\|)\s*(ghci|cabal repl|stack ghci)\b' } },
    { not = { prior_call = { tool = "Skill", field = "skill", value = "ghci" } } },
]
```

- The hook records a call only when it sees it, so the hook's matcher must include the named tool, here `Skill`.
- `field` is a raw key of the named tool's input; derived `@`-fields are not recorded.
- History is per agent: a subagent starts with none, like its hints. `toolcheck reset` keeps it.
- Plain `check` has no history, so `prior_call` is `False` there and `not { prior_call = … }` holds.

#### Working directory

`cwd` searches the agent's working directory with a regex. Like `context_length` it reads no field of the call, so it composes freely with `not`/`all`/`any`. It catches commands whose text never names the directory they act on, such as `git worktree add ../x` run from inside a checkout:

```toml
[[rule]]
name     = "worktree-in-work"
tool     = "Bash"
message  = "create worktrees here with story-workspace"
severity = "error"
[rule.match]
all = [
    { cwd = '/work/' },
    { regex = { field = "command", pattern = '\bgit\s+worktree\s+add\b' } },
]
```

The directory comes from the payload's `cwd`, which Claude Code's PreToolUse payload and the pi extension both send. A plain `check` reads it from its input too. A search of an unknown directory holds `False`, so `not { cwd = … }` holds when the payload carries none.

#### Syntax trees

`ast` parses a field as source code with [ast-grep](https://ast-grep.github.io/) and holds when its rule matches any node. `rule`, and the optional `constraints` and `utils`, are an [ast-grep rule config](https://ast-grep.github.io/reference/rule.html) written as TOML tables. `language` is any language ast-grep supports, such as `bash`, `typescript`, `python` or `haskell`.

Unlike `regex`, a rule over the syntax tree tells a command from the same text inside a quoted string, a heredoc or a comment. This `rg … | wc -l` rule matches `FOO=1 rg foo | wc -l` and `cd x && rg a |& wc -l`, and skips `printf 'rg a | wc -l'`, `rg a | sort | wc -l` and `wc -c`:

```toml
[[rule]]
name    = "rg-pipe-wc"
tool    = "Bash"
message = "`rg ... | wc -l` — use `rg -c` (or `--count-matches`)"
[rule.match.ast]
field    = "command"
language = "bash"
[rule.match.ast.rule]
kind    = "command"
has     = { kind = "word", regex = '^-l$' }
all     = [{ has = { field = "name", regex = '^wc$' } }]
follows = { regex = '^\|&?$', follows = { kind = "command", has = { field = "name", regex = '^rg$' } } }
```

- `precedes` and `follows` look only at the adjacent node unless `stopBy = "end"`, and the node next to a piped command is the `|` token, so step through it as above.
- Bash patterns such as `rg $$$ | wc -l` miss commands with an environment prefix. Match `kind = "command"` with a `has` on its `name` instead.
- `lint-rules` reports an unknown language or node kind. A `pattern` that does not parse matches nothing, and lint cannot tell.
- The ast-grep playground and the `ast-grep` CLI show the tree a command parses to.

#### Commands

`command` reads a field as a bash script and holds when one of the simple commands it runs matches:

```toml
[rule.match.command]
field       = "command"
name        = "git"              # or a list, such as ["gh", "hub"]; matched against the program's basename
args        = ["commit"]         # optional: these appear in order among the arguments that do not start with "-"
with_arg    = '^(-[a-z]*m|--message|-F|--file)'   # optional: some argument matches
without_arg = '^--no-edit$'      # optional: no argument matches
```

It matches `git commit -am x`, `cd repo && FOO=1 /usr/bin/git -C . commit -m x` and `sudo git commit -m x`, and skips `echo 'git commit -m x'`, a heredoc that contains it, and `git commit --no-edit; echo -m`, because each argument test reads one command's own arguments, with their quotes removed.

- Text inside quotes, heredoc bodies and comments runs nothing. A command inside `$(…)` runs.
- A command that runs another counts as both: `sudo`, `doas`, `env`, `time`, `exec`, `nice`, `nohup`, `command`, `builtin`, `stdbuf`, `timeout` and `xargs` run the command after their options; `reaped` and `headless-sway` the command after `--`; `nix develop` and `nix shell` the command after `-c`. The quoted script of `sh -c`, `bash -c` (or `-lc`) and the remote command of `ssh` are read as bash too, three levels deep.
- An argument that is an expansion, such as `"$msg"` or `$(cat f)`, reads as its source text.

### Regex engine

Patterns use Python's `re` module with `search` semantics. See the [documentation](https://docs.python.org/3/library/re.html) for more info.

### Views

A rule that names several tools usually needs one field that means the same thing in each, such as the shell commands a call runs, which Bash sends as `command` and a code-running MCP tool hides inside the `code` it runs. A `[[view]]` table in any rules file declares such a field for one tool:

```toml
[[view]]
name  = "@shell"
tool  = "Bash"
field = "command"

[[view]]
name     = "@shell"
tool     = "mcp__x__run"
field    = "code"
language = "typescript"
[view.rule]
any = [
  { kind = "template_string", inside = { kind = "call_expression", has = { field = "function", regex = "^sh$" } } },
  { any = [{ kind = "string" }, { kind = "template_string" }], inside = { kind = "arguments", inside = { kind = "call_expression", has = { field = "function", regex = "^exec$" } } } },
]

[[rule]]
name     = "gh-pr-create"
tool     = ["Bash", "mcp__x__run"]
message  = "…"
[rule.match.ast]
field    = "@shell"
language = "bash"
rule     = { kind = "command", has = { field = "name", regex = "^gh$" } }
```

- A view's value is a list of strings. Without `language` and `rule` it holds the field's value. With them, it holds each node the ast-grep rule matches in the field: a string literal as its contents, with each interpolation such as `${x}` read as the word `_`, and any other node as its source text. Above, ``await sh`cd ${d} && gh pr create` `` gives `cd _ && gh pr create`, and a string that only mentions it gives nothing.
- `regex`, `equals`, `in`, `length` and `ast` hold on a view when they hold for one of its values, `present` when it has any, and `absent` when it has none.
- A view's `field` may name another view of the same tool.
- On a tool with no view of that name, the field is absent. `lint-rules` reports a view name that no tool of the rule declares, a view that reads itself, and a query that does not compile.
- Views with the same name and tool replace each other in load order, like rules.

### Builtin derived fields

A field starting with `@` is computed from the tool input, by a view or by one of the derived fields built into `toolcheck`:

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

Cross-call rules fire every recurrence (not subject to dedup or every_calls) and need session state; only `toolcheck check --claude` and `toolcheck check --pi` evaluate them.

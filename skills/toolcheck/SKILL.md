---
name: toolcheck
description: >
  Write, fix, or review toolcheck rules: the TOML `[[rule]]` files under
  `~/.config/toolcheck/rules/` (or `$TOOLCHECK_RULES_DIRS`) that lint
  agent tool calls as a Claude Code PreToolUse hook or a pi extension.
  Use when the user asks to add a hint, warning, reminder, nudge, or
  deny/block rule for a tool call (Bash, Read, Edit, Write, Grep, Glob,
  Agent, `mcp__*`); when a rule fires wrongly, never fires, or needs a
  tighter regex; or on any of `[rule.match]`, `severity`, `sample_field`,
  `dedup`, `regex`, `present`, `absent`, `equals`, `in`, `length`,
  `context_length`, `not`, `all`, `any`, `cross_call`,
  `@last_pipe_command`, `toolcheck check`, `toolcheck lint-rules`,
  `toolcheck list`, or `TOOLCHECK_NO_DEDUP`. Out of scope: changing
  toolcheck's own Python source or the pi extension.
---

# Writing toolcheck rules

A rule names one tool, one message, and one predicate over the call's `tool_input`. When the predicate holds, `warning` and `reminder` rules inject the message into the model's context and `error` rules deny the call. The hook reads the rule directory on every call, so a saved file is live at once. There is no reload step.

## Workflow

1. Find the live directory and what is already there: `toolcheck list --tool Bash`. The default directory is `~/.config/toolcheck/rules/`, and `$TOOLCHECK_RULES_DIRS` overrides it. Files there group rules by tool (`bash.toml`, `read.toml`) or by topic (`config-fish.toml`). Add to the matching file, or start a new topic file.
2. Look up the field names the tool sends in [references/tool-fields.md](references/tool-fields.md). A rule on a field the tool never sends can never fire, and `lint-rules` does not catch it.
3. Write the rule from the template below.
4. Run `toolcheck lint-rules`. A rule that fails to parse is skipped; the hook tells the model which file and why once per session, and `lint-rules` lists it.
5. Run the rule against one input that should fire and one that should not:

   ```sh
   echo '{"tool_name":"Bash","tool_input":{"command":"rg foo | wc -l"}}' | toolcheck check
   ```

   Exit 1 and a `[name] message` line means it fired. `(no rules fired)` and exit 0 means it did not. Add `--json` for structured output. Plain `check` applies no dedup and no session state.

To test a draft without touching the live catalogue, point `TOOLCHECK_RULES_DIRS` at a scratch directory for the `lint-rules` and `check` invocations.

## Template

```toml
[[rule]]
name     = "rg-pipe-wc"
tool     = "Bash"
message  = "`rg ... | wc -l` — use `rg -c` (or `--count-matches`)"
severity = "warning"

[rule.match]
regex = { field = "command", pattern = '\brg\b[^|]*\|\s*wc\s+-l\b' }
```

Optional keys: `dedup` (default `true` for warning and reminder, `false` for error), `enabled` (default `true`), `sample_field` (defaults to `command` for Bash, `file_path` for Read/Edit/Write, `pattern` for Grep/Glob, `description` for Agent, and nothing for other tools).

## Name, severity, message

`name` is kebab-case, `<tool-or-topic>-<what>`, and unique across every rules directory. Two rules with one name do not both run: the last one loaded wins and the first disappears. `lint-rules` reports the duplicate.

Pick `severity` by what should happen to the call:

- `warning`: the call runs and the model sees the message once per session. Use for a better command or tool.
- `reminder`: same delivery as `warning`, for context the model should hold rather than a mistake it made. The framing lives in your message text. toolcheck adds no prefix.
- `error`: the call is denied and the model sees the message as the reason. It fires on every occurrence. Use for things that must not happen (secrets, `--no-verify`, piping a download into a shell), and make the message name the path the model should take instead, or it will try variants until one slips past.

Messages follow one shape: what was seen, an em-dash, the remedy. Lowercase start, no trailing period, under about 120 characters. The hint renders as `[name] message` with the sample field on a second line, so the message should not repeat the command. Read the live catalogue before writing to keep the voice uniform.

## Predicates

| Predicate | TOML | Holds when |
|---|---|---|
| regex | `{ regex = { field = "command", pattern = '…' } }` | `re.search` matches the field |
| present / absent | `{ present = "limit" }` / `{ absent = "limit" }` | the field is set / missing |
| equals / in | `{ in = { field = "@last_pipe_command", values = ["head", "tail"] } }` | the field's string value is listed |
| length | `{ length = { field = "content", op = ">", value = 1500 } }` | characters of a string, or items of a list or object, compare true |
| context_length | `{ context_length = { op = ">", value = 120000 } }` | the session's token count compares true |
| not / all / any | `{ all = [ p, q ] }` | boolean combination |
| cross_call | `{ cross_call = { kind = "duplicate_field_within_session", field = "file_path" } }` | seen earlier this session (see below) |

Ops for `length` and `context_length`: `>`, `>=`, `<`, `<=`, `==`, `!=`. Non-string values are matched as JSON text: `{ equals = { field = "replace_all", value = "true" } }`.

## Regex

Patterns are Python `re` with `search` semantics. Anchor with `^\s*` when you mean "the command starts with". Use TOML literal strings (`'…'`) so backslashes stay single; a pattern that needs a `'` goes in `'''…'''`.

A Bash `command` is usually compound: `cd x && cat f`, `a; b`, `a | b`. A pattern anchored at `^` misses everything after the first statement. The live catalogue uses two shapes for this:

- Statement start: `(^|&&|;|\|)\s*(ghci|ghc)\s`.
- Stay inside one pipe segment with `[^|]*`, or inside one statement with `[^|;&]*`, so `rg foo | sort -u` does not match a `sort -u` three commands later.

`(?i)` at the start makes a pattern case-insensitive.

## Gotchas

These hold in the installed `toolcheck` and will not be reported by `lint-rules`.

- A Bash `command` is the whole string, heredoc bodies and quoted text included. A rule on `curl … | sh` fires when an agent writes a file whose contents mention it. Error rules on Bash deny such writes outright, so keep their patterns narrow, or accept the false positive and tell the user to use the Write tool.
- `regex` on a missing field matches against the empty string. So `{ not = { regex = … } }` holds whenever the field is absent. Wrap it: `all = [ { present = "f" }, { not = { regex = … } } ]`.
- `cross_call` must be the whole `match`. You cannot combine it with a `regex` to say "duplicate Read of a `.hs` file". `lint-rules` rejects nesting.
- `context_length` is unknown under plain `check` and on the first call of a session, and a comparison against unknown is `False`. Under `--claude` it is estimated from the transcript and lags the current turn by about one message.
- `@last_pipe_command` is the basename of the first token of the rightmost `|` segment, with `VAR=x` prefixes stripped. `||` is not a pipe. It is the only derived field; any other `@name` fails lint.
- Under pi, `path` is renamed to `file_path` before evaluation. pi's `edit` sends an `edits` list, not a single string, so toolcheck joins the `newText` values into a synthetic `new_string`; a rule on `new_string` fires under both agents. `old_string` is never synthesised, so rules on it never fire under pi. A `cross_call` on `field = "path"` never fires under pi either, and lint does not flag it. Target `file_path`.
- Dedup makes a warning fire once per session, so a rule you are iterating on in a live session seems to stop working. Set `TOOLCHECK_NO_DEDUP=1` in the hook's environment while developing, or test with plain `check`.

## Cross-call rules

`cross_call` compares the current call's field with earlier calls to the same tool in the same session. `duplicate_field_within_session` fires when the exact value was seen before. `adjacent_near_identical` fires when it is within about 90% similarity of the previous call's value. They fire on every recurrence and ignore `dedup`.

Plain `check` skips them. Test with the hook envelope and a session id, twice:

```sh
for i in 1 2; do
  echo '{"tool_name":"Read","tool_input":{"file_path":"/x"},"session_id":"probe"}' | toolcheck check --claude
done
```

The second run has `additionalContext`. State lives under `~/.local/state/claude-code/toolcheck/<session_id>.json` (`~/.local/state/pi/toolcheck/` for pi). Delete the file to reset.

The README in the toolcheck repository documents the full format, the pi vocabulary mapping, and the hook envelopes.

# Tool input fields

The field names each tool sends in `tool_input`. A predicate on a field the tool never sends tests `None`: `present` fails, `absent` holds, and `regex` matches the empty string.

## Claude Code built-in tools

| Tool | Fields | Default `sample_field` |
|---|---|---|
| `Bash` | `command`, `description`, `timeout`, `run_in_background`, `dangerouslyDisableSandbox` | `command` |
| `Read` | `file_path`, `offset`, `limit`, `pages` | `file_path` |
| `Edit` | `file_path`, `old_string`, `new_string`, `replace_all` | `file_path` |
| `Write` | `file_path`, `content` | `file_path` |
| `Grep` | `pattern`, `path`, `glob`, `type`, `output_mode`, `-i`, `-n`, `-A`, `-B`, `-C`, `head_limit`, `offset`, `multiline` | `pattern` |
| `Glob` | `pattern`, `path` | `pattern` |
| `Agent` | `description`, `prompt`, `subagent_type`, `model`, `isolation` | `description` |
| `Skill` | `skill`, `args` | none |
| `WebFetch` | `url`, `prompt` | none |
| `WebSearch` | `query`, `allowed_domains`, `blocked_domains` | none |
| `NotebookEdit` | `notebook_path`, `cell_id`, `new_source`, `cell_type`, `edit_mode` | none |
| `AskUserQuestion` | `questions`, `answers` | none |

Optional fields are absent from `tool_input` when the model omits them, not present as `null`. `absent = "limit"` on `Read` means the model asked for the whole file.

`Grep` field names that start with `-` need quoting in TOML: `{ absent = "-n" }`.

## MCP tools

MCP tool names are `mcp__<server>__<tool>`, for example `mcp__inquisitor__popup`. Their fields are whatever the server's input schema declares. Set `sample_field` explicitly or the hint has no sample line.

## pi tools

`toolcheck check --pi` renames pi's tools and fields to the Claude vocabulary before rules run. Write rules against the canonical names.

| pi | canonical | fields |
|---|---|---|
| `bash` | `Bash` | `command` unchanged |
| `read` | `Read` | `path` → `file_path`; `offset`, `limit` unchanged |
| `write` | `Write` | `path` → `file_path`; `content` unchanged |
| `edit` | `Edit` | `path` → `file_path`; `edits` (a list) unchanged; no `old_string` or `new_string` |
| `grep` | `Grep` | `pattern` unchanged |
| `find`, `ls`, custom tools | unchanged | unchanged |

A rule on `old_string` runs only under Claude Code. A rule on `file_path` runs under both.

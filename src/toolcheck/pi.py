"""pi → canonical tool-call vocabulary mapping.

pi's tool names and input keys differ from Claude Code's in a few places
(``read.path`` vs ``Read.file_path``). One catalogue serves both agents, so
pi payloads are normalised to Claude-shaped names before evaluation; unknown
tools and keys pass through unchanged, which lets pi-specific rules target
``find``/``ls``/custom tools by their pi names.

Mapping tables are data, not code: normalise is a dictionary lookup per key.
"""

from __future__ import annotations

from collections.abc import Mapping

# pi tool name → canonical (Claude-shaped) tool name.
TOOL_NAMES: dict[str, str] = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "grep": "Grep",
}

# (pi tool, pi field) → canonical field. Keyed on the pi tool name so
# unknown tools pass through completely unchanged.
FIELD_NAMES: dict[tuple[str, str], str] = {
    ("read", "path"): "file_path",
    ("write", "path"): "file_path",
    ("edit", "path"): "file_path",
}


def normalise(tool_name: str, tool_input: Mapping[str, object]) -> tuple[str, dict[str, object]]:
    """Map a pi tool call to canonical names; unmapped keys pass through."""
    canonical = TOOL_NAMES.get(tool_name, tool_name)
    mapped = {
        FIELD_NAMES.get((tool_name, field), field): value
        for field, value in tool_input.items()
    }
    return canonical, mapped

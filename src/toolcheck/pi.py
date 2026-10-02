"""pi → canonical tool-call vocabulary mapping.

pi's tool names and input keys differ from Claude Code's in a few places
(``read.path`` vs ``Read.file_path``). One catalogue serves both agents, so
pi payloads are normalised to Claude-shaped names before evaluation; unknown
tools and keys pass through unchanged, which lets pi-specific rules target
``find``/``ls``/custom tools by their pi names.

Mapping tables are data, not code: normalise is a dictionary lookup per key.
The one computed mapping is pi's ``edit``: its ``edits`` list is joined into a
synthetic ``new_string`` so rules written against Claude's single-string edit
fire under both agents.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

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


def _joined_new_text(edits: object) -> str | None:
    """Concatenated ``newText`` of every edit, or None when there are none."""
    if not isinstance(edits, list):
        return None
    parts: list[str] = []
    for item in cast("list[object]", edits):
        if not isinstance(item, Mapping):
            continue
        new_text: object = cast("Mapping[str, object]", item).get("newText")
        if isinstance(new_text, str):
            parts.append(new_text)
    return "\n".join(parts) if parts else None


def normalise(tool_name: str, tool_input: Mapping[str, object]) -> tuple[str, dict[str, object]]:
    """Map a pi tool call to canonical names; unmapped keys pass through."""
    canonical = TOOL_NAMES.get(tool_name, tool_name)
    mapped = {
        FIELD_NAMES.get((tool_name, field), field): value
        for field, value in tool_input.items()
    }
    if tool_name == "edit" and "new_string" not in mapped:
        new_string = _joined_new_text(mapped.get("edits"))
        if new_string is not None:
            mapped["new_string"] = new_string
    return canonical, mapped

"""Estimate current context length from a Claude Code transcript.

The PreToolUse payload carries no token count, but assistant entries in the
JSONL transcript hold a ``message.usage`` block. The last such block's token
sum approximates current context-window occupancy.

Two caveats, both upstream: the transcript is written asynchronously and may
lag the current turn, and it is empty on the first call. Either yields None —
"unknown" — which a context-length comparison treats as False.

The parsing core (context_length_from_entries) is pure; read_context_length
is the best-effort I/O edge and never raises.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import cast

# Summed for "fullest occupancy": input plus both cache tiers, plus the output
# that becomes part of the context on the next turn.
_TOKEN_KEYS: tuple[str, ...] = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def usage_total(usage: Mapping[str, object]) -> int | None:
    """Sum the token counts in a usage block; None when it carries none."""
    total = 0
    seen = False
    for key in _TOKEN_KEYS:
        v = usage.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            total += v
            seen = True
    return total if seen else None


def _usage_of(entry: object) -> Mapping[str, object] | None:
    if not isinstance(entry, dict):
        return None
    message = cast("dict[str, object]", entry).get("message")
    if not isinstance(message, dict):
        return None
    usage = cast("dict[str, object]", message).get("usage")
    return cast("Mapping[str, object]", usage) if isinstance(usage, dict) else None


def context_length_from_entries(entries: Iterable[object]) -> int | None:
    """The last usage-bearing entry's token total, or None when there is none."""
    result: int | None = None
    for entry in entries:
        usage = _usage_of(entry)
        if usage is not None:
            total = usage_total(usage)
            if total is not None:
                result = total
    return result


def _parse_lines(lines: Iterable[str]) -> Iterable[object]:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def context_length_from_lines(lines: Iterable[str]) -> int | None:
    """Parse JSONL lines (skipping malformed ones) and total the last usage."""
    return context_length_from_entries(_parse_lines(lines))


def read_context_length(path: str) -> int | None:
    """Read a transcript file and estimate context length; None on any failure."""
    try:
        with open(path, encoding="utf-8") as f:
            return context_length_from_lines(f)
    except OSError:
        return None

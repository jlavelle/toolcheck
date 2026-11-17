"""Read context length and context text from a Claude Code transcript.

The PreToolUse payload carries no token count, but assistant entries in the
JSONL transcript hold a ``message.usage`` block. The last such block's token
sum approximates current context-window occupancy.

Two caveats, both upstream: the transcript is written asynchronously and may
lag the current turn, and it is empty on the first call. Either yields None —
"unknown" — which a context-length comparison treats as False.

The text in context is every string in the entries after the last
compaction boundary, an entry of type ``system`` and subtype
``compact_boundary``. The few messages a compaction preserves sit before the
boundary, so they read as gone, which costs a rule at most one extra firing.

A subagent's hook payload names the parent's transcript, so the subagent's own
is found beside it by its ``agent_id``.

The parsing cores are pure; read_entries is the best-effort I/O edge and never
raises.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
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


def _is_compact_boundary(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    e = cast("dict[str, object]", entry)
    return e.get("type") == "system" and e.get("subtype") == "compact_boundary"


def since_compaction(entries: Sequence[object]) -> Sequence[object]:
    """The entries after the last compaction boundary, or all when there is none."""
    for i in range(len(entries) - 1, -1, -1):
        if _is_compact_boundary(entries[i]):
            return entries[i + 1 :]
    return entries


def _strings(value: object) -> Iterator[str]:
    match value:
        case str():
            yield value
        case dict():
            for v in cast("dict[str, object]", value).values():
                yield from _strings(v)
        case list():
            for v in cast("list[object]", value):
                yield from _strings(v)
        case _:
            return


def context_texts(entries: Sequence[object]) -> tuple[str, ...]:
    """Every string in the entries since the last compaction."""
    return tuple(s for e in since_compaction(entries) for s in _strings(e))


def agent_transcript_path(transcript_path: str, agent_id: str | None) -> str:
    """The transcript of the agent a hook payload names.

    The main session's is ``transcript_path``. A subagent's is
    ``subagents/agent-<agent_id>.jsonl`` in the directory named after it.
    """
    if agent_id is None:
        return transcript_path
    main = Path(transcript_path)
    return str(main.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl")


def read_entries(path: str) -> tuple[object, ...] | None:
    """Parse a transcript file's entries; None when it cannot be read."""
    try:
        with open(path, encoding="utf-8") as f:
            return tuple(_parse_lines(f))
    except OSError:
        return None

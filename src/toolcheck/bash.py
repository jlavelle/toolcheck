"""Bash command derivations for the rule catalogue.

Isolated and pure so derivation laws are easy to property-test.
"""

from __future__ import annotations

import re
from typing import Final

_ENV_ASSIGN_PREFIX: Final[re.Pattern[str]] = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*=\S*\s+)+"
)
_FIRST_NON_WS: Final[re.Pattern[str]] = re.compile(r"^\S+")
# Match a single `|` that is not part of `||`. `||` is a statement separator,
# not a pipe; splitting on this pattern keeps it intact.
_REAL_PIPE: Final[re.Pattern[str]] = re.compile(r"(?<!\|)\|(?!\|)")


def first_token(cmd: str) -> str | None:
    """Basename of the first non-env-assignment token in `cmd`.

    Token boundaries are pure whitespace — quoting is ignored.
    `FOO=1 BAR=2 /usr/bin/git status` → `git`.
    """
    stripped = _ENV_ASSIGN_PREFIX.sub("", cmd.lstrip())
    m = _FIRST_NON_WS.match(stripped)
    if not m:
        return None
    return m.group(0).rsplit("/", 1)[-1]


def last_pipe_command(cmd: str) -> str | None:
    """`first_token` of the rightmost real-pipe (`|`, not `||`) segment.

    `rg foo | head -5` → `head`; `test -f x || head` → `test` (`||` is not a
    pipe, so the whole command is one segment).
    """
    return first_token(_REAL_PIPE.split(cmd)[-1].strip())

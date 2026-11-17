"""ast-grep queries over source text, imported only when a rule uses one."""

from __future__ import annotations

import functools
import re
from typing import TYPE_CHECKING, Final, cast

from toolcheck.schema import AstQuery

if TYPE_CHECKING:
    from ast_grep_py import Config, SgNode

PLACEHOLDER: Final = "_"
"""The word an interpolation inside a string literal reads as."""

_FRAGMENT_KINDS: Final = frozenset({"string_fragment", "string_content"})
_DELIMITER_KINDS: Final = frozenset({"string_start", "string_end"})
_ESCAPE: Final = re.compile(r"\\(.)", re.DOTALL)
_ESCAPES: Final = {"n": "\n", "t": "\t", "r": "\r", "0": "\0"}


@functools.lru_cache(maxsize=32)
def parse(text: str, language: str) -> SgNode:
    """The syntax tree of ``text``, shared by every query that reads the same field."""
    from ast_grep_py import SgRoot

    return SgRoot(text, language).root()


def _config(query: AstQuery) -> Config:
    config: dict[str, object] = {"rule": query.rule}
    if query.constraints is not None:
        config["constraints"] = query.constraints
    if query.utils is not None:
        config["utils"] = query.utils
    return cast("Config", config)


def check(query: AstQuery) -> None:
    """Raises ValueError for an unknown language or a rule ast-grep rejects."""
    try:
        parse("", query.language).find(_config(query))
    except BaseException as e:
        # ast-grep raises Exception for a bad rule and panics, which pyo3
        # surfaces as a BaseException, for an unknown language.
        if not isinstance(e, Exception) and type(e).__name__ != "PanicException":
            raise
        raise ValueError(
            f"ast rule for {query.language!r} does not compile: {e}"
        ) from e


def matches(query: AstQuery, text: str) -> bool:
    return parse(text, query.language).find(_config(query)) is not None


def extract(query: AstQuery, text: str) -> tuple[str, ...]:
    """The text of each node the query matches, string literals read as their contents."""
    return tuple(
        _contents(node) for node in parse(text, query.language).find_all(_config(query))
    )


def _contents(node: SgNode) -> str:
    """A string literal's contents, with each interpolation read as one placeholder word.

    Any other node reads as its source text.
    """
    if "string" not in node.kind():
        return node.text()
    parts: list[str] = []
    for child in node.children():
        kind = child.kind()
        if kind in _FRAGMENT_KINDS or kind == "escape_sequence":
            parts.append(_ESCAPE.sub(_unescape, child.text()))
        elif child.is_named() and kind not in _DELIMITER_KINDS:
            parts.append(PLACEHOLDER)
    return "".join(parts)


def _unescape(escape: re.Match[str]) -> str:
    return _ESCAPES.get(escape[1], escape[1])

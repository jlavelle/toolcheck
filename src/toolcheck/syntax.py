"""ast-grep queries over source text, imported only when a rule uses one."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, cast

from toolcheck.schema import AstQuery

if TYPE_CHECKING:
    from ast_grep_py import Config, SgNode


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

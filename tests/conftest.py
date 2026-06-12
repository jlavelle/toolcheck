"""Shared fixtures: rule-catalogue installation with env and cache hygiene."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterator

import pytest

from toolcheck.schema import clear_rule_cache

RulesEnv = Callable[[bytes], Path]


@pytest.fixture
def rules_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RulesEnv]:
    """Install TOML bytes as the active rule catalogue for the test.

    Points TOOLCHECK_RULES_DIR at a fresh directory and invalidates the
    memoised catalogue on both entry and exit; monkeypatch restores the
    prior environment.
    """

    def _install(toml: bytes) -> Path:
        d = tmp_path / "rules"
        d.mkdir(exist_ok=True)
        (d / "test.toml").write_bytes(toml)
        monkeypatch.setenv("TOOLCHECK_RULES_DIR", str(d))
        clear_rule_cache()
        return d

    yield _install
    clear_rule_cache()

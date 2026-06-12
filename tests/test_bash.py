"""Bash derivation laws and pinned examples."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from toolcheck import bash


def test_first_token_strips_env_and_path():
    assert bash.first_token("FOO=1 BAR=2 /usr/bin/git status") == "git"
    assert bash.first_token("  ls -la") == "ls"
    assert bash.first_token("") is None
    assert bash.first_token("   ") is None


def test_last_pipe_command():
    assert bash.last_pipe_command("rg foo | head") == "head"
    assert bash.last_pipe_command("cat x | tail -n 5") == "tail"
    assert bash.last_pipe_command("rg foo | wc -l") == "wc"
    # `||` is a statement separator, not a pipe — whole command is one segment.
    assert bash.last_pipe_command("test -f x || head") == "test"
    assert bash.last_pipe_command("") is None


@given(st.text())
def test_first_token_basename_has_no_slash(cmd: str):
    tok = bash.first_token(cmd)
    assert tok is None or "/" not in tok


@given(st.text())
def test_last_pipe_command_basename_has_no_slash(cmd: str):
    tok = bash.last_pipe_command(cmd)
    assert tok is None or "/" not in tok

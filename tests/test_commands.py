"""Simple commands read through quoting and wrappers, and the command predicate."""

from __future__ import annotations

import shlex

import pytest
from hypothesis import given
from hypothesis import strategies as st

from strategies import make_rule
from toolcheck.commands import SimpleCommand, simple_commands
from toolcheck.compile import Env, compile_rule
from toolcheck.schema import CommandPredicate

C = SimpleCommand


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("echo 'gh pr create' # git push", [C("echo", ("gh pr create",))]),
        ("cat <<EOF\ngh pr create\nEOF", [C("cat", ())]),
        ("FOO=1 /usr/bin/git push", [C("git", ("push",))]),
        (
            """printf %s 'a b' "c \\"d\\" $x" e"f"'g' $'h\\ni\\x41'""",
            [C("printf", ("%s", "a b", 'c "d" $x', "efg", "h\niA"))],
        ),
        ("git log $(rg -l x)", [C("git", ("log", "$(rg -l x)")), C("rg", ("-l", "x"))]),
        (
            "sudo -u root FOO=1 git push",
            [C("sudo", ("-u", "root", "FOO=1", "git", "push")), C("git", ("push",))],
        ),
        ("command -v rm", [C("command", ("-v", "rm"))]),
        (
            "bash -o pipefail -c 'git push'",
            [C("bash", ("-o", "pipefail", "-c", "git push")), C("git", ("push",))],
        ),
        (
            "nix shell nixpkgs#x --command git push",
            [
                C("nix", ("shell", "nixpkgs#x", "--command", "git", "push")),
                C("git", ("push",)),
            ],
        ),
        (
            "nix develop -c bun x",
            [C("nix", ("develop", "-c", "bun", "x")), C("bun", ("x",))],
        ),
        (
            "ssh -p 22 hestia 'cd x && git push'",
            [
                C("ssh", ("-p", "22", "hestia", "cd x && git push")),
                C("cd", ("x",)),
                C("git", ("push",)),
            ],
        ),
    ],
)
def test_simple_commands(script: str, expected: list[SimpleCommand]):
    assert list(simple_commands(script)) == expected


_WORDS = st.sampled_from(["push", "-m", "x y", "it's", '"q"', "$HOME", "a\\b"])
_NAMES = st.sampled_from(["git", "gh", "rg", "curl"])
_WRAPS = [
    "sudo -u root {}",
    "env -u X A=1 {}",
    "time {}",
    "timeout -s KILL 10 {}",
    "xargs -0 -n 1 {}",
    "reaped --timeout 5m -- {}",
    "headless-sway -- {}",
    "nix shell nixpkgs#x -c {}",
]


@given(_NAMES, st.lists(_WORDS, max_size=3), st.sampled_from(_WRAPS))
def test_a_wrapper_runs_the_command_it_wraps(name: str, words: list[str], wrap: str):
    command = shlex.join([name, *words])
    assert simple_commands(wrap.format(command))[1:] == simple_commands(command)


@given(_NAMES, st.lists(_WORDS, max_size=3), st.sampled_from(["sh -c", "bash -lc"]))
def test_a_shell_string_runs_its_commands(name: str, words: list[str], shell: str):
    command = shlex.join([name, *words])
    nested = f"{shell} {shlex.quote(command)}"
    assert simple_commands(nested)[1:] == simple_commands(command)


def test_shell_strings_are_read_three_deep():
    script = "gh pr create"
    for _ in range(4):
        script = f"sh -c {shlex.quote(script)}"
    assert [c.name for c in simple_commands(script)] == ["sh"] * 4


@pytest.mark.parametrize(
    ("pred", "command", "expected"),
    [
        (CommandPredicate("command", ("gh",), ("pr", "create")), "gh pr create", True),
        (
            CommandPredicate("command", ("gh",), ("pr", "create")),
            "gh -R o/r pr --web create",
            True,
        ),
        (CommandPredicate("command", ("gh",), ("pr", "create")), "gh create pr", False),
        (CommandPredicate("command", ("git", "gh"), ()), "cd x && gh", True),
        (
            CommandPredicate("command", ("git",), ("commit",), with_arg="^-[a-z]*m"),
            "git commit -am x",
            True,
        ),
        (
            CommandPredicate("command", ("git",), ("commit",), with_arg="^-[a-z]*m"),
            "git commit --no-edit; echo -m",
            False,
        ),
        (
            CommandPredicate(
                "command", ("git",), ("commit",), without_arg="^--no-edit$"
            ),
            "git commit --amend --no-edit",
            False,
        ),
        (
            CommandPredicate(
                "command", ("git",), ("commit",), without_arg="^--no-edit$"
            ),
            "git commit --amend && echo --no-edit",
            True,
        ),
    ],
)
def test_command_predicate(pred: CommandPredicate, command: str, expected: bool):
    assert compile_rule(make_rule(pred))(Env({"command": command})) is expected

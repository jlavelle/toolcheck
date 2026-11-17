"""The simple commands a bash script runs, read through quoting and wrappers.

Quoted text, heredoc bodies and comments are data, so they yield no
commands. A command that runs another, such as ``sudo git push`` or
``bash -c 'gh pr create'``, yields both itself and the command it runs.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from ast_grep_py import SgNode


@dataclass(frozen=True)
class SimpleCommand:
    name: str
    """The basename of the program, as ``git`` for ``/usr/bin/git``."""
    args: tuple[str, ...]
    """The arguments with their quoting removed."""


MAX_DEPTH: Final = 3
"""How many shell strings deep ``sh -c`` and ``ssh`` commands are read."""


@dataclass(frozen=True)
class _Wrapper:
    """A program that runs the command in its arguments.

    The command starts after ``separator`` when one is given, otherwise at
    the first argument that is neither an option nor the value of an option
    in ``valued``, nor a ``VAR=value`` assignment, skipping ``positional``
    leading operands such as timeout's duration.
    """

    valued: frozenset[str] = frozenset()
    separator: str | None = None
    positional: int = 0


_WRAPPERS: Final[dict[str, _Wrapper]] = {
    "sudo": _Wrapper(frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"})),
    "doas": _Wrapper(frozenset({"-u", "-C"})),
    "env": _Wrapper(frozenset({"-u", "-C", "-S", "--unset", "--chdir"})),
    "time": _Wrapper(frozenset({"-f", "-o"})),
    "exec": _Wrapper(frozenset({"-a"})),
    "nice": _Wrapper(frozenset({"-n"})),
    "nohup": _Wrapper(),
    "command": _Wrapper(),
    "builtin": _Wrapper(),
    "stdbuf": _Wrapper(frozenset({"-i", "-o", "-e"})),
    "timeout": _Wrapper(
        frozenset({"-s", "-k", "--signal", "--kill-after"}), positional=1
    ),
    "xargs": _Wrapper(
        frozenset({"-n", "-I", "-i", "-L", "-l", "-P", "-d", "-s", "-E", "-e", "-a"})
    ),
    "reaped": _Wrapper(separator="--"),
    "headless-sway": _Wrapper(separator="--"),
}

_SHELLS: Final = frozenset({"sh", "bash", "dash", "zsh"})
_SSH_VALUED: Final = frozenset(
    {"-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m"}
    | {"-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w", "-B"}
)
_ASSIGNMENT: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_VALUED: Final = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
_LOOKUPS: Final = frozenset({"-v", "-V"})
_ANSI_C_ESCAPE: Final = re.compile(r"\\(x[0-9a-fA-F]{1,2}|[0-7]{1,3}|.)")
_ANSI_C_LETTERS: Final = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}


@functools.lru_cache(maxsize=32)
def simple_commands(script: str) -> tuple[SimpleCommand, ...]:
    """Every simple command ``script`` runs, in source order, wrappers first."""
    return _script_commands(script, MAX_DEPTH)


def _script_commands(script: str, depth: int) -> tuple[SimpleCommand, ...]:
    from toolcheck.syntax import parse

    return tuple(
        found
        for node in parse(script, "bash").find_all(kind="command")
        if (command := _command(node)) is not None
        for found in _unwrap(command, depth)
    )


def _command(node: SgNode) -> SimpleCommand | None:
    name = node.field("name")
    if name is None:
        return None
    args = tuple(_unquote(child) for child in node.field_children("argument"))
    return SimpleCommand(_basename(_unquote(name)), args)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _unwrap(command: SimpleCommand, depth: int) -> tuple[SimpleCommand, ...]:
    """The command, then whatever it runs."""
    inner = _runs(command, depth)
    return (command, *inner)


def _runs(command: SimpleCommand, depth: int) -> tuple[SimpleCommand, ...]:
    name, args = command.name, command.args
    if name in _SHELLS:
        return _shell_string(args, depth)
    if name == "ssh":
        rest = _after_options(args, _SSH_VALUED)
        return _parse_nested(" ".join(rest[1:]), depth) if len(rest) > 1 else ()
    if name == "nix" and args[:1] in (("develop",), ("shell",)):
        return _from_words(_after(args, "-c") or _after(args, "--command"), depth)
    if name == "command" and args[:1] and args[0] in _LOOKUPS:
        return ()
    wrapper = _WRAPPERS.get(name)
    if wrapper is None:
        return ()
    if wrapper.separator is not None:
        return _from_words(_after(args, wrapper.separator), depth)
    rest = _after_options(args, wrapper.valued)
    return _from_words(rest[wrapper.positional :], depth)


def _shell_string(args: tuple[str, ...], depth: int) -> tuple[SimpleCommand, ...]:
    """The commands of ``sh -c 'script'``, where ``-c`` may share a cluster such as ``-lc``."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _SHELL_VALUED:
            i += 2
        elif arg.startswith("-") and not arg.startswith("--") and "c" in arg:
            return _parse_nested(args[i + 1], depth) if i + 1 < len(args) else ()
        elif arg.startswith(("-", "+")):
            i += 1
        else:
            return ()
    return ()


def _parse_nested(script: str, depth: int) -> tuple[SimpleCommand, ...]:
    return _script_commands(script, depth - 1) if depth > 0 else ()


def _after(args: tuple[str, ...], marker: str) -> tuple[str, ...]:
    return args[args.index(marker) + 1 :] if marker in args else ()


def _after_options(args: tuple[str, ...], valued: frozenset[str]) -> tuple[str, ...]:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return args[i + 1 :]
        if arg in valued:
            i += 2
        elif arg.startswith("-") or _ASSIGNMENT.match(arg):
            i += 1
        else:
            return args[i:]
    return ()


def _from_words(words: tuple[str, ...], depth: int) -> tuple[SimpleCommand, ...]:
    if not words:
        return ()
    return _unwrap(SimpleCommand(_basename(words[0]), words[1:]), depth)


def _unquote(node: SgNode) -> str:
    """A word's value with its quoting removed; expansions keep their source text."""
    text = node.text()
    match node.kind():
        case "raw_string":
            return text[1:-1]
        case "ansi_c_string":
            return _ANSI_C_ESCAPE.sub(_ansi_c_char, text[2:-1])
        case "string":
            return "".join(
                re.sub(r'\\([\\"$`])', r"\1", c.text())
                if c.kind() == "string_content"
                else c.text()
                for c in node.children()
                if c.is_named()
            )
        case "concatenation" | "command_name":
            return "".join(_unquote(c) for c in node.children() if c.is_named())
        case "word":
            return re.sub(r"\\(.)", r"\1", text)
        case _:
            return text


def _ansi_c_char(escape: re.Match[str]) -> str:
    code = escape[1]
    if code[0] == "x" and len(code) > 1:
        return chr(int(code[1:], 16))
    if code[0] in "01234567":
        return chr(int(code, 8))
    return _ANSI_C_LETTERS.get(code, code)

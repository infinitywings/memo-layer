"""Anchors: the files, commands and tools of a project that events touch.

Code derives them from event payloads, never a model, so the same file always
gets the same anchor and no call is spent (docs/GRAPH.md, decision 2). An
anchor is a string key: `file:<project-relative path>`, `command:<program>`
or `command:<program> <subcommand>`, and `tool:<agent tool name>`.

A user message does not touch anything, but it can name anchors: a path-like
token, or a file or command the project has already touched.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import Any

from rka_memory.domain import Event, Kind

FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "read", "edit", "write"})
SHELL_TOOLS = frozenset({"Bash", "bash", "pwsh", "PowerShell"})
# Programs whose first argument picks what they do; the anchor keeps both.
SUBCOMMANDED = frozenset(
    {"git", "uv", "npm", "pnpm", "yarn", "pip", "pip3", "cargo", "go", "docker", "make", "poetry", "gh", "kubectl"}
)
# Prefixes that only run the command after them.
WRAPPERS = frozenset({"sudo", "env", "time", "nohup", "exec", "command"})
SHELL_BUILTINS = frozenset(
    {"cd", "echo", "printf", "true", "false", "export", "set", "test", "[", ":", "source", ".", "exit", "return"}
)
SEPARATORS = re.compile(r"&&|\|\||[;|\n]")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PATH_TOKEN = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|pyi|js|jsx|ts|tsx|go|rs|java|kt|rb|c|h|cc|cpp|hpp|cs|swift|sql|sh"
    r"|md|rst|txt|toml|yaml|yml|json|ini|cfg|html|css|lock))(?![\w/-])"
)


def file_anchor(path: str, project: str | None) -> str | None:
    """`file:<path relative to the project>`, or None for a path outside it."""
    if not path:
        return None
    p = PurePosixPath(path)
    if p.is_absolute():
        if not project:
            return None
        try:
            p = p.relative_to(project)
        except ValueError:
            return None
    parts = [part for part in p.parts if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "file:" + "/".join(parts)


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _program(words: list[str]) -> str | None:
    while words and (ASSIGNMENT.match(words[0]) or words[0] in WRAPPERS):
        words = words[1:]
    if not words:
        return None
    program = PurePosixPath(words[0]).name
    rest = [w for w in words[1:] if not w.startswith("-")]
    if program in ("python", "python3") and words[1:3] and words[1] == "-m" and len(words) > 2:
        return _program(words[2:])
    if program == "uv" and rest[:1] == ["run"]:
        return _program(words[words.index("run") + 1 :])
    if program in SHELL_BUILTINS:
        return None
    if program in SUBCOMMANDED and rest:
        return f"{program} {rest[0]}"
    return program


def command_anchors(command: str) -> set[str]:
    """One `command:` anchor per program a shell command line runs."""
    out = set()
    for segment in SEPARATORS.split(command or ""):
        program = _program(_words(segment.strip()))
        if program:
            out.add(f"command:{program}")
    return out


def touched(ev: Event, project: str | None) -> set[str]:
    """The anchors an event touches."""
    p: Mapping[str, Any] = ev.payload
    if ev.kind in (Kind.FILE_WRITE, Kind.USER_EDIT, Kind.USER_UNDO):
        anchor = file_anchor(str(p.get("path") or ""), project)
        return {anchor} if anchor else set()
    if ev.kind in (Kind.EXIT_CODE, Kind.TEST_RESULT):
        return command_anchors(str(p.get("command") or ""))
    if ev.kind is Kind.TOOL_CALL:
        tool, given = str(p.get("tool") or ""), p.get("input")
        out = {f"tool:{tool}"} if tool else set()
        if tool in SHELL_TOOLS and isinstance(given, str):
            out |= command_anchors(given)
        if tool in FILE_TOOLS and isinstance(given, str):
            anchor = file_anchor(given, project)
            out |= {anchor} if anchor else set()
        return out
    return set()


def named(text: str, project: str | None, known: Iterable[str]) -> set[str]:
    """The anchors a user's text names: path-like tokens inside the project,
    and already-touched files or commands mentioned by path, file name or
    program name."""
    out = {a for a in (file_anchor(m, project) for m in PATH_TOKEN.findall(text or "")) if a}
    words = set(re.findall(r"[\w./-]+", text or ""))
    for anchor in known:
        kind, _, value = anchor.partition(":")
        if kind == "file" and (value in words or PurePosixPath(value).name in words):
            out.add(anchor)
        elif kind == "command" and value.split(" ")[0] in words:
            out.add(anchor)
    return out

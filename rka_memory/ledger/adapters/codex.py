"""Codex hook payloads, mapped onto the Claude Code shape the hook handlers read.

Codex sends the same field names for most events (session_id, cwd,
tool_name, tool_input, tool_response, prompt), with three differences that
matter to the ledger:

- Codex has no PostToolUseFailure event. The Claude Code adapter takes a
  payload with a transcript to mean that a failed command would have come
  through that event, so it logs every other command as exit 0. A Codex
  payload therefore drops its transcript: a command's outcome is then known
  only when its response states an exit code, and otherwise none is logged.
- File edits go through `apply_patch`, whose patch text is in
  `tool_input.command`. Each file the patch names becomes one Edit, so the
  ledger tracks it like any other agent write and a later change by the user
  is told apart from the agent's.
- Stop carries the turn's reply as `last_assistant_message` rather than in a
  transcript, so it is logged the way the dsh plugin logs a reply.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SHELL_TOOLS = frozenset({"Bash", "shell", "local_shell", "exec_command"})
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$", re.MULTILINE)


def patch_paths(patch: str, cwd: str | None) -> list[str]:
    """The files a patch adds, updates, deletes or moves to, absolute, in order, once each."""
    out: list[str] = []
    for name in _PATCH_FILE.findall(patch):
        path = Path(name)
        if not path.is_absolute() and cwd:
            path = Path(cwd) / path
        if str(path) not in out:
            out.append(str(path))
    return out


def _command(tool_input: Mapping[str, Any]) -> str:
    command = tool_input.get("command")
    if isinstance(command, list):
        return shlex.join(str(c) for c in command)
    return command if isinstance(command, str) else ""


def normalize(event: str, payload: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The handler events and payloads one Codex hook event maps to."""
    out = {k: v for k, v in payload.items() if k != "transcript_path"}
    if event == "Stop":
        return [("AssistantMessage", {
            "session_id": out.get("session_id"),
            "cwd": out.get("cwd"),
            "text": out.get("last_assistant_message") or "",
            "turn": out.get("turn_id") or "",
            "step": "stop",
        })]
    tool_input = out.get("tool_input") if isinstance(out.get("tool_input"), Mapping) else {}
    if out.get("tool_name") == "apply_patch":
        use_id = str(out.get("tool_use_id") or "")
        return [
            (event, {**out, "tool_name": "Edit", "tool_use_id": f"{use_id}:{i}", "tool_input": {"file_path": path}})
            for i, path in enumerate(patch_paths(_command(tool_input), out.get("cwd")))
        ]
    if out.get("tool_name") in SHELL_TOOLS:
        out["tool_name"], out["tool_input"] = "Bash", {**tool_input, "command": _command(tool_input)}
    return [(event, out)]

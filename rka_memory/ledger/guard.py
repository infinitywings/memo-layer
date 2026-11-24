"""Keep the agent's tools away from the memory's own state.

The gate shows the agent only what evidence it did not write supports, and
a verdict counts only if the user gave it. Both hold only while the agent
cannot reach around them:

- The ledger directory holds every record, validated or not, the user's
  verdicts, and the config. Reading it shows the agent what the gate holds
  back; writing it forges events or turns this guard off.
- The rka-memory CLI prints unvalidated records (`status`, `review`,
  `explain`). The agent's channel is the MCP server, which labels what it
  returns and only asks the user for verdicts. `doctor` prints health and
  no content, so the agent may run it; any other use of the CLI is denied.
  The CLI's verdicts (`confirm`, `reject`, `rkm`) also refuse to run
  without a terminal, which agents' shell tools do not have.

This is a tripwire, not a boundary: a command can reach the directory in a
way no pattern catches. The boundary is the OS sandbox of the hardened
install (`install-hooks --harden`). The tripwire stops the plain cases, an
agent that follows an instruction it read, or misreads one.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rka_memory import config as config_mod
from rka_memory.config import Config

# The default directory's name, protected wherever the ledger is configured to be.
DEFAULT_NAME = ".rka-memory"
ALLOWED = frozenset({"doctor", "-h", "--help", "--version"})
# A command word: at the start, after a shell separator or opening quote, or as the last part of a path.
_INVOCATION = re.compile(r"(?:^|[\s;&|(`'\"=])(?:[^\s;&|(`'\"=]*/)?rka-memory(?=\s|$|[;&|)`'\"])")
_MODULE = re.compile(r"\brka_memory\b")
_ENV = re.compile(r"\bRKA_MEMORY_(?:DIR|CONFIG)\b")

LEDGER = (
    "rka-memory: the agent's tools may not read or change the memory's directory or config ({dir}). "
    "Use the rka-memory MCP tools (memory_packet, memory_explain), and ask the user for any verdict."
)
CLI = (
    "rka-memory: the agent may run only `rka-memory doctor`; other commands print records the gate has not "
    "validated or act as the user. Use the rka-memory MCP tools; the user runs the CLI in their own terminal."
)


def _spellings(path: Path) -> set[str]:
    out = {str(path), str(path.resolve())}
    home = Path(os.path.expanduser("~"))
    for d in (path, path.resolve()):
        try:
            rel = d.relative_to(home)
        except ValueError:
            continue
        out |= {f"~/{rel}", f"$HOME/{rel}", f"${{HOME}}/{rel}"}
    return {s.rstrip("/") for s in out}


def protected_forms(cfg: Config) -> list[str]:
    """The ways a command or a tool input can spell the ledger directory or the config file.
    The directory's own name counts too, so a relative path or a glob that names it is caught."""
    forms = _spellings(cfg.ledger_dir) | _spellings(config_mod.user_config_path()) | {DEFAULT_NAME}
    if cfg.ledger_dir.name.startswith("."):
        forms.add(cfg.ledger_dir.name)
    return sorted(forms, key=len, reverse=True)


def _names(text: str, form: str) -> bool:
    """`form` appears as itself, not as a prefix of a longer name."""
    at = text.find(form)
    while at >= 0:
        end = at + len(form)
        if end == len(text) or not (text[end].isalnum() or text[end] in "-_"):
            return True
        at = text.find(form, at + 1)
    return False


def _next_word(rest: str) -> str | None:
    """The first argument after `rka-memory`, unquoted; None for no argument."""
    try:
        words = shlex.split(rest, comments=False)
    except ValueError:
        return rest.split()[0] if rest.split() else None
    return words[0] if words else None


def _cli_use(command: str) -> bool:
    """The command runs the rka-memory CLI with anything but `doctor` or help."""
    for match in _INVOCATION.finditer(command):
        rest = re.split(r"[;&|)`\n]", command[match.end():], maxsplit=1)[0]
        word = _next_word(rest)
        if word is not None and word not in ALLOWED:
            return True
    return bool(_MODULE.search(command) or _ENV.search(command))


def blocked(tool: str, tool_input: Mapping[str, Any], cfg: Config) -> str | None:
    """Why the agent's call is denied, or None."""
    if not cfg.hooks_guard:
        return None
    text = json.dumps(tool_input, ensure_ascii=False).replace("\\/", "/")
    for form in protected_forms(cfg):
        if _names(text, form):
            return LEDGER.format(dir=form)
    command = tool_input.get("command") if tool == "Bash" else None
    if isinstance(command, str) and _cli_use(command):
        return CLI
    return None

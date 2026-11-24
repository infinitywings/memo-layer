"""Keep the agent's tools away from the memory's own state.

The gate shows the agent only what evidence it did not write supports, and
a verdict counts only if the user gave it. Both hold only while the agent
cannot reach around them:

- The ledger directory holds every record, validated or not, and the
  user's verdicts. Reading it shows the agent what the gate holds back;
  writing it forges events.
- The rka-memory CLI prints unvalidated records (`status`, `review`,
  `explain`) and gives verdicts (`confirm`, `reject`, `rkm`) as the user.
  The agent's channel is the MCP server, which labels what it returns and
  only asks the user for verdicts. `doctor` prints health and no content,
  so the agent may run it.

This is a tripwire, not a boundary: a command can name the directory in a
way no pattern catches. The boundary is the OS sandbox of the hardened
install (`install-hooks --harden`). The tripwire stops the plain cases, an
agent that follows an instruction it read, or misreads one.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rka_memory.config import Config

SUBCOMMANDS = (
    "hook", "gate", "mine", "export-skills", "metrics", "status", "explain", "graph", "confirm", "reject", "rkm",
    "review", "impact", "packet", "replay", "sync", "pull", "init", "install-hooks", "skills", "jobs", "mcp", "workload",
)
ALLOWED = frozenset({"doctor"})
_CLI = re.compile(r"(?:^|[\s;&|(`'\"=])(?:\S*/)?rka-memory\s+(?:-\S+\s+)*(" + "|".join(map(re.escape, SUBCOMMANDS)) + r")\b")
_MODULE = re.compile(r"-m\s+rka_memory\b|import\s+rka_memory|from\s+rka_memory\b")

LEDGER = (
    "rka-memory: the agent's tools may not read or change the memory ledger ({dir}). "
    "Use the rka-memory MCP tools (memory_packet, memory_explain), and ask the user for any verdict."
)
CLI = (
    "rka-memory: the agent may not run `rka-memory {sub}`: it prints records the gate has not validated or "
    "gives a verdict as the user. Use the rka-memory MCP tools; the user runs the CLI in their own terminal."
)


def ledger_forms(cfg: Config) -> list[str]:
    """The ways a command or a tool input can spell the ledger directory."""
    forms = {str(cfg.ledger_dir), str(cfg.ledger_dir.resolve())}
    home = Path(os.path.expanduser("~"))
    for d in (cfg.ledger_dir, cfg.ledger_dir.resolve()):
        try:
            rel = d.relative_to(home)
        except ValueError:
            continue
        forms |= {f"~/{rel}", f"$HOME/{rel}", f"${{HOME}}/{rel}"}
    return sorted((f.rstrip("/") for f in forms), key=len, reverse=True)


def _names(text: str, form: str) -> bool:
    """`form` appears as the directory itself, not as a prefix of a longer name."""
    at = text.find(form)
    while at >= 0:
        end = at + len(form)
        if end == len(text) or not (text[end].isalnum() or text[end] in "-_."):
            return True
        at = text.find(form, at + 1)
    return False


def blocked(tool: str, tool_input: Mapping[str, Any], cfg: Config) -> str | None:
    """Why the agent's call is denied, or None."""
    if not cfg.hooks_guard:
        return None
    text = json.dumps(tool_input, ensure_ascii=False).replace("\\/", "/")
    for form in ledger_forms(cfg):
        if _names(text, form):
            return LEDGER.format(dir=form)
    command = tool_input.get("command") if tool == "Bash" else None
    if isinstance(command, str):
        for match in _CLI.finditer(command):
            if match.group(1) not in ALLOWED:
                return CLI.format(sub=match.group(1))
        if _MODULE.search(command):
            return CLI.format(sub="python -m rka_memory")
    return None

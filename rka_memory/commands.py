"""The `rkm` commands a user types, in a prompt or at the CLI.

Every command here is the user's act, so only paths the user types into may
call `run`: the UserPromptSubmit hook, which catches a prompt starting with
`rkm` before the model sees it, and `rka-memory rkm`. The agent's tools only
ask the user to type one.

    rkm confirm <id>                         affirm a record
    rkm reject <id> <reason>                 withdraw it and what depends on it
    rkm qualify <id> <text>                  add a condition, exception or limit
    rkm correct <id> <text>                  replace its wording
    rkm replace <id> <text>                  replace it: the old version was wrong
    rkm update <id> <field>=<value> <text>   a new version where the world changed
    rkm close <id> <field>=<value>           end it where the condition holds
    rkm defeat <link-id>                     withdraw one evidence link
    rkm impact <id>                          what rejecting it would do; records nothing
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from rka_memory.domain import SCOPE_FIELDS

if TYPE_CHECKING:
    from rka_memory.service import Memory

COMMAND = re.compile(r"^\s*rkm\s+(\w+)\s+(\S+)(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
CONDITION = re.compile(r"^(\w+)=(\S+)(?:\s+(.*))?$", re.DOTALL)
USAGE = "rka-memory: use `rkm confirm|reject|qualify|correct|replace|update|close|defeat|impact <id> ...`"
WITH_TEXT = ("qualify", "correct", "replace")


class CommandError(ValueError):
    """A command that records nothing; the message says why."""


def run(memory: Memory, prompt: str, session_id: str) -> str:
    match = COMMAND.match(prompt)
    if match is None:
        raise CommandError(USAGE)
    verb, target, rest = match.group(1).lower(), match.group(2), (match.group(3) or "").strip()
    world = memory.world()
    if verb == "defeat":
        owner = next((r.id for r in world.records.values() for link in r.links if link.via == target), None)
        if owner is None:
            raise CommandError(f"rka-memory: no evidence link {target}; nothing recorded")
        memory.amend("defeat", owner, link=target, session_id=session_id)
        memory.gate(session_id=session_id)
        return f"rka-memory recorded defeat of {target}; {owner} now {_rung(memory, owner)}"
    if target not in world.records:
        raise CommandError(f"rka-memory: unknown record {target}; nothing recorded")
    if verb == "impact":
        return _impact_text(memory.impact(target))
    if verb in ("confirm", "reject"):
        memory.verdict(target, confirm=verb == "confirm", session_id=session_id, reason=rest)
        memory.gate(session_id=session_id)
        return f"rka-memory recorded {verb} for {target}; now {_rung(memory, target)}"
    condition, text = None, rest
    if verb in ("update", "close"):
        cond = CONDITION.match(rest)
        if cond is None or cond.group(1) not in SCOPE_FIELDS:
            raise CommandError(f"rka-memory: `rkm {verb}` needs <field>=<value> with a field of {list(SCOPE_FIELDS)}")
        condition, text = {cond.group(1): cond.group(2)}, (cond.group(3) or "").strip()
    elif verb not in WITH_TEXT:
        raise CommandError(USAGE)
    if verb != "close" and not text:
        raise CommandError(f"rka-memory: `rkm {verb}` needs the new text; nothing recorded")
    ev = memory.amend(verb, target, text=text, condition=condition, session_id=session_id)
    memory.gate(session_id=session_id)
    new = ev.payload.get("record_id")
    tail = f"; {new} now {_rung(memory, new)}" if new else ""
    return f"rka-memory recorded {verb} of {target}; {target} now {_rung(memory, target)}{tail}"


def _rung(memory: Memory, record_id: str) -> str:
    rung = memory.world().records[record_id].rung
    return rung.value if rung else "unrated"


def _impact_text(report: dict) -> str:
    parts = [f"rka-memory: rejecting {report['record_id']} would withdraw " + (", ".join(report["withdrawn"]) or "nothing")]
    if report["withheld"]:
        parts.append("withhold " + ", ".join(f"{w['record_id']} ({w['from']} to {w['to']})" for w in report["withheld"]))
    if report["kept"]:
        parts.append("keep " + ", ".join(f"{k['record_id']} ({k['reason']})" for k in report["kept"]))
    if report["files_written_while_in_force"]:
        parts.append("leave for your review the files written while it was in force: " + ", ".join(report["files_written_while_in_force"]))
    if report["skills"]:
        parts.append("remove the skills " + ", ".join(report["skills"]))
    return "; ".join(parts) + ". Nothing was recorded."

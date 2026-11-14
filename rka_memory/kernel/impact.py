"""What retracting a record would do, before the user does it (DESIGN_V2 7.3).

The preview labels the log as it is and as it would be with the user's
reject appended, and reports the difference: the records that would be
withdrawn, the records learned while the root was in force that would lose
their rung, the dependents that would keep it because the user affirmed
them, the files written in sessions where the root or anything withdrawn
with it was in force, and the exported skills that would be removed.
Memory cannot undo code; the files are for the user to review.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rka_memory.config import Config
from rka_memory.domain import Actor, Event, Kind, RecordKind
from rka_memory.kernel.influence import Influence, Reach
from rka_memory.kernel.labels import Labels, label
from rka_memory.skills.gate import skill_name
from rka_memory.world import World, project


def impact(world: World, cfg: Config, now: datetime, record_id: str) -> dict[str, Any]:
    before = label(world, cfg, now)
    reject = Event.new(Kind.USER_REJECT, Actor.USER, "impact-preview", {"record_id": record_id, "reason": "preview"}, ts=now.isoformat())
    after_world = project([*world.events.values(), reject])
    after = label(after_world, cfg, now)
    withdrawn = sorted(after.withdrawn - before.withdrawn, key=lambda r: world.position[world.records[r].proposed.id])
    closure = Reach(world).dependents(frozenset({record_id}))
    return {
        "record_id": record_id,
        "withdrawn": withdrawn,
        "withheld": _withheld(world, before, after, set(withdrawn)),
        "kept": [
            {"record_id": rid, "reason": "the user affirmed it"}
            for rid in sorted(closure - set(withdrawn), key=lambda r: world.position[world.records[r].proposed.id])
            if after.by_id[rid].affirmed
        ],
        "files_written_while_in_force": _files(world, set(withdrawn)),
        "skills": [
            skill_name(rid) for rid in withdrawn
            if world.records[rid].spec.kind is RecordKind.PROCEDURE and before.by_id[rid].deliverable
        ],
    }


def _withheld(world: World, before: Labels, after: Labels, withdrawn: set[str]) -> list[dict[str, Any]]:
    out = []
    for rid in sorted(world.records, key=lambda r: world.position[world.records[r].proposed.id]):
        was, will = before.by_id[rid], after.by_id[rid]
        if rid in withdrawn or will.rung is was.rung or not will.learned_under:
            continue
        out.append({"record_id": rid, "from": was.rung.value, "to": will.rung.value, "learned_under": list(will.learned_under)})
    return out


def _files(world: World, withdrawn: set[str]) -> list[str]:
    """Paths written in sessions where a withdrawn record was in force, in
    the order first written."""
    influence = Influence(world)
    sessions = {sid for rid in withdrawn for sid in influence.sessions_holding.get(rid, ())}
    seen: dict[str, None] = {}
    for ev in world.events.values():
        if ev.kind is Kind.FILE_WRITE and ev.session_id in sessions and ev.payload.get("path"):
            seen.setdefault(str(ev.payload["path"]), None)
    return list(seen)

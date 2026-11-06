"""Dependency propagation over typed `depends_on` links.

A record's grounding is independent of a dependency unless it was observed in
a session where that dependency was delivered to the agent. While a
dependency is invalid (below L2), evidence from those sessions does not count,
so dependents drop when their remaining grounding no longer meets their rung.
"""

from __future__ import annotations

from rka_memory.domain import DELIVERABLE
from rka_memory.world import RecordState, World


def invalid_dependencies(rec: RecordState, world: World) -> list[RecordState]:
    return [
        world.records[d]
        for d in rec.spec.depends_on
        if d in world.records and world.records[d].rung not in DELIVERABLE
    ]


def tainted_sessions(rec: RecordState, world: World) -> set[str]:
    bad = {d.id for d in invalid_dependencies(rec, world)}
    if not bad:
        return set()
    return {d.event.session_id for d in world.deliveries if bad.intersection(d.record_ids)}


def dependency_causes(rec: RecordState, world: World) -> list[str]:
    """The rung-change events that made this record's dependencies invalid."""
    return [d.history[-1].event_id for d in invalid_dependencies(rec, world) if d.history]


def fresh_dependency_causes(rec: RecordState, world: World) -> list[str]:
    """Dependency invalidations this record has not dropped for yet.

    A dependent drops one rung per invalidation, so a cause already cited by
    one of its own moves does not move it again.
    """
    used = {c for move in rec.history for c in move.cause_ids}
    return [c for c in dependency_causes(rec, world) if c not in used]


def dependents(record_id: str, world: World) -> list[RecordState]:
    return [r for r in world.records.values() if record_id in r.spec.depends_on]

"""What could have influenced an observation, and what a record's influence reaches.

`influence(world, ev)` is the set of record ids that were in the agent's
context when `ev` arose (DESIGN_V2 4.3, prompt influence): every record
delivered in the session before `ev`, on any channel, and every decision
made in the session before `ev`. A user edit or undo
reacts to files an earlier session wrote, so it also carries that session's
influence. Artifact influence through files is added in layer 4.

`reach(world, record_id)` is the record's equivalence class and dependency
closure (DESIGN_V2 4.4, 5.1): the record, every version in its supersession
chain, and every record that depends on any of them. Evidence influenced by
anything in the reach is not independent of the record.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property

from rka_memory.domain import Event, Kind, RecordKind
from rka_memory.world import World

CROSS_SESSION_KINDS = frozenset({Kind.USER_EDIT, Kind.USER_UNDO})


@dataclass
class Influence:
    world: World
    # session id -> delivery positions in order, and the records delivered by each
    _deliveries: dict[str, tuple[list[int], list[frozenset[str]]]] = field(init=False)

    def __post_init__(self) -> None:
        by_session: dict[str, list[tuple[int, frozenset[str]]]] = defaultdict(list)
        for d in self.world.deliveries:
            by_session[d.event.session_id].append((self.world.position[d.event.id], frozenset(d.record_ids)))
        for rid, sid, pos in self._decisions():
            by_session[sid].append((pos, frozenset({rid})))
        self._deliveries = {}
        for sid, rows in by_session.items():
            rows.sort()
            self._deliveries[sid] = ([p for p, _ in rows], [ids for _, ids in rows])

    def in_session(self, session_id: str | None, before: int | None = None) -> frozenset[str]:
        """Records delivered in the session, before position `before` if given."""
        if not session_id or session_id not in self._deliveries:
            return frozenset()
        positions, ids = self._deliveries[session_id]
        upto = len(positions) if before is None else bisect_left(positions, before)
        return frozenset().union(*ids[:upto])

    def of(self, ev: Event) -> frozenset[str]:
        own = self.in_session(ev.session_id, self.world.position[ev.id])
        if ev.kind in CROSS_SESSION_KINDS:
            return own | self.in_session(ev.payload.get("write_session_id"))
        return own

    def _decisions(self) -> list[tuple[str, str, int]]:
        """A decision is in force in the session that made it from the moment
        it was made, delivered or not (DESIGN_V2 4.2, 4.3)."""
        return [
            (rec.id, rec.proposed.session_id, self.world.position[rec.proposed.id])
            for rec in self.world.records.values()
            if rec.spec.kind is RecordKind.DECISION
        ]

    @cached_property
    def sessions_holding(self) -> dict[str, frozenset[str]]:
        """Record id -> sessions it was delivered in or, for a decision, made in."""
        out: dict[str, set[str]] = defaultdict(set)
        for d in self.world.deliveries:
            for rid in d.record_ids:
                out[rid].add(d.event.session_id)
        for rid, sid, _ in self._decisions():
            out[rid].add(sid)
        return {rid: frozenset(s) for rid, s in out.items()}


@dataclass
class Reach:
    world: World
    _dependents: dict[str, set[str]] = field(init=False)
    _versions: dict[str, set[str]] = field(init=False)

    def __post_init__(self) -> None:
        self._dependents = defaultdict(set)
        self._versions = defaultdict(set)
        for rec in self.world.records.values():
            for dep in rec.spec.dependencies:
                self._dependents[dep].add(rec.id)
            old = rec.spec.supersedes
            if old:
                self._versions[rec.id].add(old)
                self._versions[old].add(rec.id)

    def version_class(self, record_id: str) -> frozenset[str]:
        seen, todo = {record_id}, [record_id]
        while todo:
            for nxt in self._versions.get(todo.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return frozenset(seen)

    def dependents(self, record_ids: frozenset[str]) -> frozenset[str]:
        """`record_ids` and every record depending on them, transitively."""
        seen, todo = set(record_ids), list(record_ids)
        while todo:
            for nxt in self._dependents.get(todo.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return frozenset(seen)

    def of(self, record_id: str) -> frozenset[str]:
        out: set[str] = set()
        for member in self.dependents(self.version_class(record_id)):
            out |= self.version_class(member)
        return frozenset(out)

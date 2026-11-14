"""What could have influenced an observation, and what a record's influence reaches.

`Influence.of(ev, rec)` is the set of record ids that could have shaped `ev`
(DESIGN_V2 4.3):

- prompt influence: every record delivered in the session before `ev`, on
  any channel, and every decision made in the session before `ev`;
- artifact influence: for every file a step of the session touched up to
  `ev` (a file tool's path, a written file), the prompt influence of the
  session that last wrote it, at the time of that write. One hop. It
  accumulates within the session: once the agent has read a file written
  under a record, what it observes afterwards carries that record;
- for a test or exit result, the last writers before the session of the
  files the session changed and, given `rec`, of the files `rec` is about:
  the tools do not log which files a test run executes, so this
  approximates it;
- a user edit or undo carries the influence of the session that wrote the
  file it reacts to;
- a runner check carries only the artifact influence of the files it read:
  no prompt shaped it.

`Reach.of(record_id)` is the record's identity class and dependency
closure (4.4, 5.1): every version in its supersession chain, every
same-kind paraphrase above the dedup threshold, and every record that
depends on any of them. Evidence influenced by anything in the reach is not
independent of the record.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property

from rka_memory.config import Config
from rka_memory.domain import Event, Kind, RecordKind
from rka_memory.miner.dedup import similarity
from rka_memory.world import RecordState, World

CROSS_SESSION_KINDS = frozenset({Kind.USER_EDIT, Kind.USER_UNDO})
OUTCOME_KINDS = frozenset({Kind.TEST_RESULT, Kind.EXIT_CODE})
FILE_STEP_KINDS = frozenset({Kind.TOOL_CALL, Kind.FILE_WRITE})


@dataclass
class Influence:
    world: World
    # session id -> delivery positions in order, and the records delivered by each
    _deliveries: dict[str, tuple[list[int], list[frozenset[str]]]] = field(init=False)
    # file key (raw path or file anchor) -> agent writes as (position, session) in order
    _writes: dict[str, list[tuple[int, str]]] = field(init=False)
    # session id -> file keys its steps touched, as (position, keys) in order
    _touched: dict[str, list[tuple[int, frozenset[str]]]] = field(init=False)
    _write_positions: set[int] = field(init=False)
    _art: dict[tuple[str, int], frozenset[str]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        w = self.world
        by_session: dict[str, list[tuple[int, frozenset[str]]]] = defaultdict(list)
        for d in w.deliveries:
            by_session[d.event.session_id].append((w.position[d.event.id], frozenset(d.record_ids)))
        for rid, sid, pos in self._decisions():
            by_session[sid].append((pos, frozenset({rid})))
        self._deliveries = {}
        for sid, rows in by_session.items():
            rows.sort()
            self._deliveries[sid] = ([p for p, _ in rows], [ids for _, ids in rows])
        self._writes = defaultdict(list)
        self._touched = defaultdict(list)
        self._write_positions = set()
        for ev in w.events.values():
            keys = self._keys(ev)
            if not keys:
                continue
            pos = w.position[ev.id]
            self._touched[ev.session_id].append((pos, keys))
            if ev.kind is Kind.FILE_WRITE:
                self._write_positions.add(pos)
                for k in keys:
                    self._writes[k].append((pos, ev.session_id))

    def _keys(self, ev: Event) -> frozenset[str]:
        """The files an event's step touched, by anchor and by raw path."""
        if ev.kind not in FILE_STEP_KINDS:
            return frozenset()
        keys = {a for a in self.world.touches.get(ev.id, ()) if a.startswith("file:")}
        raw = ev.payload.get("path") if ev.kind is Kind.FILE_WRITE else ev.payload.get("input")
        if keys and isinstance(raw, str) and raw.strip():
            keys.add(raw.strip())
        return frozenset(keys)

    def _decisions(self) -> list[tuple[str, str, int]]:
        """A decision is in force in the session that made it from the moment
        it was made, delivered or not (DESIGN_V2 4.2, 4.3)."""
        return [
            (rec.id, rec.proposed.session_id, self.world.position[rec.proposed.id])
            for rec in self.world.records.values()
            if rec.spec.kind is RecordKind.DECISION
        ]

    def in_session(self, session_id: str | None, before: int | None = None) -> frozenset[str]:
        """Prompt influence: records delivered (or decisions made) in the
        session, before position `before` if given."""
        if not session_id or session_id not in self._deliveries:
            return frozenset()
        positions, ids = self._deliveries[session_id]
        upto = len(positions) if before is None else bisect_left(positions, before)
        return frozenset().union(*ids[:upto])

    def written_under(self, key: str, before: int) -> frozenset[str]:
        """The prompt influence of the last agent write of `key` before
        `before`, as it stood when the file was written."""
        writes = self._writes.get(key)
        if not writes:
            return frozenset()
        i = bisect_left(writes, (before, ""))
        if i == 0:
            return frozenset()
        pos, sid = writes[i - 1]
        return self.in_session(sid, pos + 1)

    def artifact(self, session_id: str, upto: int) -> frozenset[str]:
        """Artifact influence of every file the session's steps touched up to
        position `upto`."""
        cached = self._art.get((session_id, upto))
        if cached is not None:
            return cached
        out: set[str] = set()
        for pos, keys in self._touched.get(session_id, ()):
            if pos > upto:
                break
            for k in keys:
                out |= self.written_under(k, pos)
        self._art[(session_id, upto)] = frozenset(out)
        return self._art[(session_id, upto)]

    def session(self, session_id: str | None, before: int | None = None) -> frozenset[str]:
        """Everything that could have shaped the session up to `before`."""
        if not session_id:
            return frozenset()
        upto = len(self.world.events) if before is None else before - 1
        return self.in_session(session_id, before) | self.artifact(session_id, upto)

    def of(self, ev: Event, rec: RecordState | None = None) -> frozenset[str]:
        w = self.world
        pos = w.position[ev.id]
        if ev.kind is Kind.CHECK:
            out: set[str] = set()
            for path in ev.payload.get("paths") or ():
                out |= self.written_under(str(path), pos)
            return frozenset(out)
        out = set(self.in_session(ev.session_id, pos)) | self.artifact(ev.session_id, pos)
        if ev.kind in CROSS_SESSION_KINDS:
            out |= self.session(ev.payload.get("write_session_id"))
        if ev.kind in OUTCOME_KINDS:
            sess = w.sessions.get(ev.session_id)
            start = w.position[sess.started.id] if sess is not None and sess.started is not None else pos
            changed = {k for p, keys in self._touched.get(ev.session_id, ()) if p < pos and p in self._write_positions for k in keys}
            about = {a for a in (rec.spec.about if rec is not None else ()) if a.startswith("file:")}
            for k in changed | about:
                out |= self.written_under(k, start)
        return frozenset(out)

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
    cfg: Config
    _dependents: dict[str, set[str]] = field(init=False)
    _versions: dict[str, set[str]] = field(init=False)

    def __post_init__(self) -> None:
        self._dependents = defaultdict(set)
        self._versions = defaultdict(set)
        records = list(self.world.records.values())
        for rec in records:
            for dep in rec.spec.dependencies:
                self._dependents[dep].add(rec.id)
            old = rec.spec.supersedes
            if old:
                self._versions[rec.id].add(old)
                self._versions[old].add(rec.id)
        # Paraphrases: same-kind records worded alike enough that the miner
        # would have merged them (4.4).
        for i, a in enumerate(records):
            for b in records[i + 1:]:
                if a.spec.kind is b.spec.kind and similarity(a.spec.content, b.spec.content) >= self.cfg.dedup_threshold:
                    self._versions[a.id].add(b.id)
                    self._versions[b.id].add(a.id)

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

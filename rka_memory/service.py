"""One facade for hooks, CLI and MCP: open the ledger, project, gate, deliver."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rka_memory import config as config_mod
from rka_memory import relations
from rka_memory.config import Config
from rka_memory.domain import Actor, Event, Kind, State, clock_now
from rka_memory.ledger.jsonl import JsonlLedger
from rka_memory.packet import builder
from rka_memory.validity.engine import ENGINE_SESSION, assess, counted, evaluate, statement
from rka_memory.domain import Polarity
from rka_memory.world import World, project

CLI_SESSION = "cli"


@dataclass
class Memory:
    cfg: Config
    ledger: JsonlLedger

    @classmethod
    def open(cls, cfg: Config | None = None) -> Memory:
        cfg = cfg or config_mod.load()
        return cls(cfg, JsonlLedger(cfg.ledger_dir / "ledger.jsonl"))

    def world(self) -> World:
        return project(self.ledger.read())

    def append(self, *events: Event) -> None:
        self.ledger.append(events)

    def gate(self, *, session_id: str = ENGINE_SESSION, now: datetime | None = None) -> list[Event]:
        """Run the validity engine once over the whole ledger and persist its moves."""
        now = now or clock_now()
        with self.ledger.exclusive():
            started = time.monotonic()
            world = self.world()
            moves = evaluate(world, self.cfg, now)
            cost = Event.new(
                Kind.COST,
                Actor.RUNNER,
                ENGINE_SESSION,
                {
                    "component": "gate",
                    "session_id": session_id,
                    "seconds": round(time.monotonic() - started, 4),
                    "tokens": 0,
                    "moves": len(moves),
                },
            )
            self.ledger.append([*moves, cost])
        return moves

    def deliver(
        self,
        session_id: str,
        state: State,
        *,
        channel: str,
        now: datetime | None = None,
        budget_tokens: int | None = None,
        exclude: frozenset[str] = frozenset(),
        arm: str | None = None,
        world: World | None = None,
    ) -> builder.Packet:
        """Build a packet for `state` and log exactly which records were delivered."""
        now = now or clock_now()
        world = world or self.world()
        anchors = frozenset(world.session(session_id).anchors) if self.cfg.graph_anchors else frozenset()
        missing = builder.missing_files(world, state) if self.cfg.graph_anchors else frozenset()
        if channel == "hints":
            packet = builder.hints(world, self.cfg, state, exclude=exclude, anchors=anchors, missing=missing)
        else:
            arm = arm or builder.arm_for(now.astimezone().date(), self.cfg)
            packet = builder.build(
                world,
                self.cfg,
                state,
                arm=arm,
                channel=channel,
                budget_tokens=budget_tokens,
                exclude=exclude,
                anchors=anchors,
                missing=missing,
            )
        self.append(
            Event.new(
                Kind.PACKET_DELIVERED,
                Actor.RUNNER,
                session_id,
                {
                    "record_ids": packet.record_ids,
                    "arm": packet.arm,
                    "channel": channel,
                    "omitted_count": packet.omitted_count,
                    "ledger_revision": packet.ledger_revision,
                    "budget_tokens": packet.budget_tokens,
                },
                state_hash=state.hash(),
            )
        )
        return packet

    def verdict(self, record_id: str, *, confirm: bool, session_id: str = CLI_SESSION, reason: str = "") -> Event:
        """Record a user verdict. Call only from paths the user types into
        (the CLI, or an `rkm` prompt caught by the UserPromptSubmit hook)."""
        if record_id not in self.world().records:
            raise KeyError(f"unknown record {record_id}")
        kind = Kind.USER_CONFIRM if confirm else Kind.USER_REJECT
        payload: dict[str, Any] = {"record_id": record_id}
        if not confirm:
            payload["reason"] = reason
        ev = Event.new(kind, Actor.USER, session_id, payload)
        self.append(ev)
        return ev

    def session_state(self, session_id: str, world: World | None = None) -> State:
        return (world or self.world()).session(session_id).state

    def explain(self, record_id: str, world: World | None = None) -> dict[str, Any]:
        world = world or self.world()
        rec = world.records[record_id]
        a = assess(rec, world, self.cfg)
        counted_ids = {g.event.id for g in a.support} | {g.event.id for g in a.counters}
        all_counted = {
            g.event.id
            for pol in (Polarity.SUPPORT, Polarity.COUNTER)
            for g in counted(rec, world, self.cfg, pol)
        }
        links = []
        for link in rec.links:
            ev = world.events.get(link.event_id)
            status = (
                "counted"
                if link.event_id in counted_ids
                else "outside scope or superseded"
                if link.event_id in all_counted
                else "not grounding"
            )
            row = {
                "event_id": link.event_id,
                "polarity": link.polarity.value,
                "kind": ev.kind.value if ev else None,
                "actor": ev.actor.value if ev else None,
                "session_id": ev.session_id if ev else None,
                "status": status,
            }
            said = statement(world.events.get(link.via), ev, self.cfg) if ev is not None else None
            if said is not None:
                row["statement"] = {"quote": said[0], "standing": said[1]}
            links.append(row)

        def ref(rid: str) -> dict[str, Any]:
            other = world.records.get(rid)
            return {"record_id": rid, "rung": other.rung.value if other and other.rung else None}

        return {
            "record": rec.spec.to_json(),
            "rung": rec.rung.value if rec.rung else None,
            "scope": rec.scope.describe(),
            "acceptance_check": rec.acceptance_check,
            "grounding": round(sum(g.weight for g in a.support), 3),
            "sessions": sorted({g.session_id for g in a.support}),
            "open_counters": [g.event.id for g in a.counters],
            "history": [
                {
                    "event_id": m.event_id,
                    "ts": m.ts,
                    "from": m.from_rung.value if m.from_rung else None,
                    "to": m.to_rung.value,
                    "reason": m.reason,
                    "cause_ids": list(m.cause_ids),
                }
                for m in rec.history
            ],
            "narrowings": [
                {"field": n.field, "value": n.value, "cause_ids": list(n.cause_ids), "ts": n.ts}
                for n in rec.narrowings
            ],
            "links": links,
            "relations": {
                "depends_on": [ref(d) for d in rec.spec.depends_on],
                "dependents": [ref(r.id) for r in world.records.values() if record_id in r.spec.depends_on],
                "supersedes": ref(rec.spec.supersedes) if rec.spec.supersedes else None,
                "superseded_by": [ref(r.id) for r in world.records.values() if r.spec.supersedes == record_id],
                "refines": [ref(r) for r in rec.spec.refines],
                "refined_by": [ref(r.id) for r in world.records.values() if record_id in r.spec.refines],
                "governed_by": [ref(r) for r in relations.governing(rec, world)],
                "governs": [ref(r.id) for r in world.records.values() if record_id in relations.governing(r, world)],
            },
            "deliveries": [
                {"event_id": d.event.id, "session_id": d.event.session_id, "ts": d.event.ts, "arm": d.arm, "channel": d.channel}
                for d in world.deliveries
                if record_id in d.record_ids
            ],
        }

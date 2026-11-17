"""One facade for hooks, CLI and MCP: open the ledger, project, gate, deliver."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rka_memory import config as config_mod
from rka_memory import relations
from rka_memory.config import Config
from rka_memory.domain import Actor, Event, Kind, Polarity, State, clock_now, derived_id
from rka_memory.kernel import views
from rka_memory.kernel import trials
from rka_memory.kernel.evidence import applicable, counted, inside, statement
from rka_memory.kernel.impact import impact
from rka_memory.kernel.labels import ENGINE_SESSION, Labels, audit_events, label
from rka_memory.ledger.jsonl import JsonlLedger
from rka_memory.world import NODE_OPS, World, project

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

    def labels(self, world: World | None = None, now: datetime | None = None) -> Labels:
        """Every record's label, computed from the log alone."""
        return label(world or self.world(), self.cfg, now or clock_now())

    def gate(self, *, session_id: str = ENGINE_SESSION, now: datetime | None = None) -> list[Event]:
        """Label the ledger and append the audit events that bring the
        projection's cached rungs to the labels."""
        now = now or clock_now()
        with self.ledger.exclusive():
            started = time.monotonic()
            world = self.world()
            moves = audit_events(world, label(world, self.cfg, now), now)
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
    ) -> views.View:
        """Render the packet for `state` and log exactly which records it
        delivered and the hash of the text the agent received. A budget ranks
        records about the project's recent anchors first; a prompt's packet
        also ranks those about what this session has touched or named."""
        now = now or clock_now()
        world = world or self.world()
        arm = arm or views.arm_for(now.astimezone().date(), self.cfg)
        anchors = frozenset()
        if self.cfg.graph_anchors:
            anchors = views.recent_anchors(world, state.project, session_id)
            if channel != "session_start":
                anchors |= world.session(session_id).anchors
        labels = label(world, self.cfg, now)
        treated = self._trial_arms(world, labels, state, session_id) if arm == "gated" else ()
        view = views.packet(
            world,
            labels,
            self.cfg,
            state,
            arm=arm,
            channel=channel,
            budget_tokens=budget_tokens,
            exclude=exclude,
            anchors=anchors,
            treated=treated,
        )
        self.append(
            Event.new(
                Kind.PACKET_DELIVERED,
                Actor.RUNNER,
                session_id,
                {
                    "record_ids": list(view.record_ids),
                    "arm": view.arm,
                    "channel": channel,
                    "omitted_count": len(view.omitted),
                    "ledger_revision": view.ledger_revision,
                    "budget_tokens": view.budget_tokens,
                    "view_hash": view.hash,
                },
                state_hash=state.hash(),
            )
        )
        return view

    def _trial_arms(self, world: World, labels: Labels, state: State, session_id: str) -> tuple[str, ...]:
        """Log a coin for each trial candidate that applies to the session,
        before the packet is built, and return the treated ones. A coin
        already logged in the session is reused, so every packet of one
        session sees the same arms."""
        logged = {
            str(ev.payload.get("record_id")): str(ev.payload.get("arm"))
            for ev in world.trials if ev.session_id == session_id
        }
        new, treated = [], []
        for rid in trials.candidates(world, labels, self.cfg):
            if applicable(world.records[rid], state, world) is not True:
                continue
            arm = logged.get(rid)
            if arm is None:
                draw = trials.coin(session_id, rid)
                arm = "treated" if draw < self.cfg.trials.q else "withheld"
                new.append(Event.new(Kind.TRIAL, Actor.RUNNER, session_id,
                                     {"record_id": rid, "arm": arm, "q": self.cfg.trials.q, "coin": draw}))
            if arm == "treated":
                treated.append(rid)
        if new:
            self.append(*new)
            for ev in new:
                world.apply(ev)
        return tuple(treated)

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

    def amend(
        self,
        op: str,
        target: str,
        *,
        text: str = "",
        condition: dict[str, str] | None = None,
        link: str | None = None,
        session_id: str = CLI_SESSION,
    ) -> Event:
        """Record a user amendment. Call only from paths the user types into
        (`commands.run`). An op that creates a node names it here."""
        if target not in self.world().records:
            raise KeyError(f"unknown record {target}")
        payload: dict[str, Any] = {"op": op, "target": target}
        if text:
            payload["text"] = text
        if condition:
            payload["condition"] = condition
        if link:
            payload["link"] = link
        if op in NODE_OPS:
            payload["record_id"] = derived_id("r", "amend", op, target, text, clock_now().isoformat())
        ev = Event.new(Kind.USER_AMEND, Actor.USER, session_id, payload)
        self.append(ev)
        return ev

    def impact(self, record_id: str, now: datetime | None = None) -> dict[str, Any]:
        """What rejecting `record_id` would do. Records nothing."""
        return impact(self.world(), self.cfg, now or clock_now(), record_id)

    def session_state(self, session_id: str, world: World | None = None) -> State:
        return (world or self.world()).session(session_id).state

    def explain(self, record_id: str, world: World | None = None, now: datetime | None = None) -> dict[str, Any]:
        world = world or self.world()
        labels = self.labels(world, now)
        rec = world.records[record_id]
        lab = labels.by_id[record_id]
        set_aside = dict(lab.set_aside)
        in_scope = {
            g.event.id
            for pol in (Polarity.SUPPORT, Polarity.COUNTER)
            for g in counted(rec, world, self.cfg, pol)
            if inside(rec.scope, g)
        }
        used = set(lab.support) | set(lab.counters)
        links = []
        for link in rec.links:
            ev = world.events.get(link.event_id)
            if link.event_id in used:
                status = "counted"
            elif link.event_id in set_aside:
                status = f"set aside: {set_aside[link.event_id]}"
            elif link.event_id in in_scope:
                status = "before the last affirmation"
            else:
                status = "not grounding"
            row = {
                "event_id": link.event_id,
                "polarity": link.polarity.value,
                "kind": ev.kind.value if ev else None,
                "actor": ev.actor.value if ev else None,
                "session_id": ev.session_id if ev else None,
                "status": status,
            }
            said = statement(world.events.get(link.via), ev, self.cfg, world) if ev is not None else None
            if said is not None:
                row["statement"] = {"quote": said[0], "standing": said[1]}
            links.append(row)

        def ref(rid: str) -> dict[str, Any]:
            other = labels.by_id.get(rid)
            return {"record_id": rid, "rung": other.rung.value if other else None}

        return {
            "record": rec.spec.to_json(),
            "rung": lab.rung.value,
            "reason": lab.reason,
            "since": lab.since,
            "affirmed": lab.affirmed,
            "withdrawn": lab.withdrawn,
            "learned_under": list(lab.learned_under),
            "scope": rec.scope.describe(),
            "acceptance_check": rec.spec.acceptance_check,
            "support": list(lab.support),
            "open_counters": list(lab.counters),
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

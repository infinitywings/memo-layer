"""The ledger as a graph: typed nodes and edges projected from `World`.

Read-only. Rungs and scopes stay with the validity engine; this module only
shows what the projection already holds, so `rka-memory graph`, `explain` and
the workload's provenance check read one structure (docs/GRAPH.md, phase 1).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from rka_memory.config import Config
from rka_memory.domain import Polarity
from rka_memory.validity.engine import counted, statement
from rka_memory.world import RecordState, World


class NodeType(StrEnum):
    RECORD = "record"
    EVENT = "event"


class EdgeType(StrEnum):
    EVIDENCE = "evidence"  # event -> record, with a polarity
    DEPENDS_ON = "depends_on"  # record -> record
    SUPERSEDES = "supersedes"  # record -> record
    DELIVERED = "delivered"  # delivery event -> record


@dataclass(frozen=True)
class Node:
    id: str
    type: NodeType
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type.value, **self.attrs}


@dataclass(frozen=True)
class Edge:
    type: EdgeType
    source: str
    target: str
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type.value, "source": self.source, "target": self.target, **self.attrs}


@dataclass(frozen=True)
class Graph:
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]

    def to_json(self) -> dict[str, Any]:
        return {"nodes": [n.to_json() for n in self.nodes], "edges": [e.to_json() for e in self.edges]}

    def around(self, node_id: str, depth: int = 1) -> Graph:
        """The nodes within `depth` edges of `node_id`, either direction, and the edges among them."""
        near = {node_id}
        frontier = deque([(node_id, 0)])
        while frontier:
            current, d = frontier.popleft()
            if d == depth:
                continue
            for e in self.edges:
                other = e.target if e.source == current else e.source if e.target == current else None
                if other is not None and other not in near:
                    near.add(other)
                    frontier.append((other, d + 1))
        return Graph(
            tuple(n for n in self.nodes if n.id in near),
            tuple(e for e in self.edges if e.source in near and e.target in near),
        )


def _record_node(rec: RecordState) -> Node:
    return Node(
        rec.id,
        NodeType.RECORD,
        {
            "kind": rec.spec.kind.value,
            "rung": rec.rung.value if rec.rung else None,
            "content": rec.spec.content,
            "scope": rec.scope.describe(),
        },
    )


def _evidence_edges(rec: RecordState, world: World, cfg: Config) -> list[Edge]:
    grounding = {
        g.event.id: g for polarity in (Polarity.SUPPORT, Polarity.COUNTER) for g in counted(rec, world, cfg, polarity)
    }
    edges = []
    for link in rec.links:
        g = grounding.get(link.event_id)
        attrs: dict[str, Any] = {"polarity": link.polarity.value, "via": link.via, "grounds": g is not None}
        if g is not None:
            attrs["weight"] = round(g.weight, 3)
            attrs["confirms"] = g.affirms
        ev = world.events.get(link.event_id)
        said = statement(world.events.get(link.via), ev, cfg) if ev is not None else None
        if said is not None:
            attrs["statement"] = {"quote": said[0], "standing": said[1]}
        edges.append(Edge(EdgeType.EVIDENCE, link.event_id, rec.id, attrs))
    return edges


def project(world: World, cfg: Config) -> Graph:
    records = sorted(world.records.values(), key=lambda r: world.position[r.proposed.id])
    edges: list[Edge] = []
    for rec in records:
        edges += _evidence_edges(rec, world, cfg)
        edges += [Edge(EdgeType.DEPENDS_ON, rec.id, d) for d in rec.spec.depends_on if d in world.records]
        if rec.spec.supersedes in world.records:
            edges.append(Edge(EdgeType.SUPERSEDES, rec.id, rec.spec.supersedes))
    for d in world.deliveries:
        attrs = {"session_id": d.event.session_id, "channel": d.channel, "arm": d.arm, "ts": d.event.ts}
        edges += [Edge(EdgeType.DELIVERED, d.event.id, rid, attrs) for rid in d.record_ids if rid in world.records]
    event_ids = {e.source for e in edges if e.source in world.events}
    events = [
        Node(
            ev.id,
            NodeType.EVENT,
            {"kind": ev.kind.value, "actor": ev.actor.value, "session_id": ev.session_id, "ts": ev.ts},
        )
        for ev in sorted((world.events[i] for i in event_ids), key=lambda e: world.position[e.id])
    ]
    return Graph(tuple(_record_node(r) for r in records) + tuple(events), tuple(edges))


@dataclass(frozen=True)
class Trace:
    """One record in one delivery, and the grounding evidence linked to it
    before that delivery. A delivery with no such evidence is a provenance gap."""

    delivery_id: str
    session_id: str
    channel: str
    record_id: str
    evidence: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return self.__dict__ | {"evidence": list(self.evidence)}


def traces(world: World, cfg: Config, session_id: str | None = None) -> list[Trace]:
    out = []
    for d in world.deliveries:
        if session_id is not None and d.event.session_id != session_id:
            continue
        for rid in d.record_ids:
            rec = world.records.get(rid)
            grounds = {g.event.id for g in counted(rec, world, cfg, Polarity.SUPPORT)} if rec else set()
            evidence = tuple(
                link.event_id
                for link in (rec.links if rec else ())
                if link.polarity is Polarity.SUPPORT
                and link.event_id in grounds
                and link.via in world.position
                and world.before(link.via, d.event.id)
            )
            out.append(Trace(d.event.id, d.event.session_id, d.channel, rid, tuple(dict.fromkeys(evidence))))
    return out

"""Projection of the ledger: fold events into sessions, records and links.

`project(events)` is the only way to obtain records. Rungs and scopes change
only when RUNG_CHANGE and SCOPE_NARROWED events are folded in, so replaying
the same ledger always yields the same world. A rung change that does not
start from the record's current rung (a lost race between two gates) is
skipped and listed in `anomalies`, never applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from rka_memory import anchors
from rka_memory.domain import (
    DELIVERABLE,
    FUZZY_FIELDS,
    Actor,
    TRANSITIONS,
    Event,
    Kind,
    Polarity,
    RecordSpec,
    Rung,
    Scope,
    State,
)


@dataclass(frozen=True)
class Link:
    """`event_id` is cited as evidence for `record_id`; `via` created the link."""

    event_id: str
    record_id: str
    polarity: Polarity
    via: str


@dataclass(frozen=True)
class RungMove:
    event_id: str
    ts: str
    from_rung: Rung | None
    to_rung: Rung
    cause_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Narrowing:
    event_id: str
    ts: str
    field: str
    value: str
    cause_ids: tuple[str, ...]


@dataclass(frozen=True)
class Delivery:
    event: Event
    record_ids: tuple[str, ...]
    arm: str
    channel: str


@dataclass
class Session:
    id: str
    state: State = field(default_factory=State)
    events: list[Event] = field(default_factory=list)
    started: Event | None = None
    ended: Event | None = None
    mined: bool = False
    # Anchors this session's events touched or its user messages named.
    anchors: set[str] = field(default_factory=set)


@dataclass
class RecordState:
    spec: RecordSpec
    proposed: Event
    scope: Scope
    rung: Rung | None = None
    links: list[Link] = field(default_factory=list)
    history: list[RungMove] = field(default_factory=list)
    narrowings: list[Narrowing] = field(default_factory=list)
    acceptance_check: str | None = None

    @property
    def id(self) -> str:
        return self.spec.record_id

    def last_move_to(self, rung: Rung) -> RungMove | None:
        for move in reversed(self.history):
            if move.to_rung is rung:
                return move
        return None


@dataclass
class World:
    events: dict[str, Event] = field(default_factory=dict)
    position: dict[str, int] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)
    records: dict[str, RecordState] = field(default_factory=dict)
    deliveries: list[Delivery] = field(default_factory=list)
    judgments: dict[tuple[str, str, str], bool] = field(default_factory=dict)
    anomalies: list[tuple[str, str]] = field(default_factory=list)
    # Event id -> the anchors it touched (or, for a user message, named).
    touches: dict[str, frozenset[str]] = field(default_factory=dict)
    # Project -> every anchor its events have touched so far.
    known: dict[str, set[str]] = field(default_factory=dict)

    @property
    def revision(self) -> int:
        return len(self.events)

    def session(self, session_id: str) -> Session:
        if session_id not in self.sessions:
            self.sessions[session_id] = Session(session_id)
        return self.sessions[session_id]

    def state_of(self, event: Event) -> State:
        return self.session(event.session_id).state

    def before(self, a: str, b: str) -> bool:
        return self.position[a] < self.position[b]

    def delivered_in(self, session_id: str) -> set[str]:
        return {rid for d in self.deliveries if d.event.session_id == session_id for rid in d.record_ids}

    def deliverable(self) -> list[RecordState]:
        return [r for r in self.records.values() if r.rung in DELIVERABLE]

    def apply(self, ev: Event) -> None:
        if ev.id in self.events:
            return
        self.position[ev.id] = len(self.events)
        self.events[ev.id] = ev
        sess = self.session(ev.session_id)
        sess.events.append(ev)
        _FOLD.get(ev.kind, _ignore)(self, ev)
        self._anchor(ev, sess)

    def _anchor(self, ev: Event, sess: Session) -> None:
        project = sess.state.project
        if ev.kind is Kind.USER_MSG:
            found = anchors.named(str(ev.payload.get("text") or ""), project, self.known.get(project or "", ()))
        else:
            found = anchors.touched(ev, project)
            if found and project:
                self.known.setdefault(project, set()).update(found)
        if found:
            self.touches[ev.id] = frozenset(found)
            sess.anchors.update(found)


def _ignore(world: World, ev: Event) -> None:
    pass


def _session_start(world: World, ev: Event) -> None:
    sess = world.session(ev.session_id)
    if sess.started is None:
        sess.started = ev
        sess.state = State.from_json(ev.payload.get("state"))


def _session_end(world: World, ev: Event) -> None:
    world.session(ev.session_id).ended = ev


def _judgment(world: World, ev: Event) -> None:
    """Judgments only touch fuzzy fields. A session judgment changes only its
    own session, and a model one may fill an unknown value but never overwrite
    one, so model text cannot move a session (and the evidence in it) into
    another scope. Record-applicability judgments are accepted only from the
    runner, which logs whichever judge it asked."""
    p = ev.payload
    name = p.get("field")
    if name not in FUZZY_FIELDS:
        return
    if p.get("target") == "session":
        sess = world.session(ev.session_id)
        if ev.actor is Actor.MODEL and sess.state.get(name) is not None:
            return
        sess.state = sess.state.with_field(name, p["value"])
    elif p.get("target") == "record" and ev.actor is Actor.RUNNER and p.get("record_id") in world.records:
        world.judgments[(p["record_id"], name, p["value"])] = bool(p["applies"])


def _record_proposed(world: World, ev: Event) -> None:
    spec = RecordSpec.from_json(ev.payload)
    if spec.record_id in world.records:
        return
    world.records[spec.record_id] = RecordState(spec=spec, proposed=ev, scope=spec.scope)


def _evidence_linked(world: World, ev: Event) -> None:
    rec = world.records.get(ev.payload["record_id"])
    if rec is None:
        return
    rec.links.append(
        Link(
            event_id=ev.payload["event_id"],
            record_id=rec.id,
            polarity=Polarity(ev.payload["polarity"]),
            via=ev.id,
        )
    )


def _user_verdict(world: World, ev: Event) -> None:
    rec = world.records.get(ev.payload.get("record_id", ""))
    if rec is None:
        return
    polarity = Polarity.SUPPORT if ev.kind is Kind.USER_CONFIRM else Polarity.COUNTER
    rec.links.append(Link(event_id=ev.id, record_id=rec.id, polarity=polarity, via=ev.id))


def _rung_change(world: World, ev: Event) -> None:
    p = ev.payload
    rec = world.records[p["record_id"]]
    from_rung = Rung(p["from"]) if p.get("from") else None
    to_rung = Rung(p["to"])
    if from_rung != rec.rung or (from_rung, to_rung) not in TRANSITIONS:
        world.anomalies.append((ev.id, f"{rec.id}: {from_rung} -> {to_rung} while at {rec.rung}"))
        return
    rec.rung = to_rung
    if to_rung is Rung.L3:
        rec.acceptance_check = p["acceptance_check"]
    rec.history.append(
        RungMove(ev.id, ev.ts, from_rung, to_rung, tuple(p.get("cause_ids") or ()), p.get("reason", ""))
    )


def _scope_narrowed(world: World, ev: Event) -> None:
    p = ev.payload
    rec = world.records[p["record_id"]]
    rec.scope = rec.scope.without(p["field"], p["value"])
    rec.narrowings.append(Narrowing(ev.id, ev.ts, p["field"], p["value"], tuple(p.get("cause_ids") or ())))


def _packet_delivered(world: World, ev: Event) -> None:
    p = ev.payload
    world.deliveries.append(
        Delivery(ev, tuple(p.get("record_ids") or ()), p.get("arm", "gated"), p.get("channel", "packet"))
    )


def _cost(world: World, ev: Event) -> None:
    if ev.payload.get("component") == "miner":
        world.session(ev.payload.get("session_id", ev.session_id)).mined = True


_FOLD = {
    Kind.SESSION_START: _session_start,
    Kind.SESSION_END: _session_end,
    Kind.JUDGMENT: _judgment,
    Kind.RECORD_PROPOSED: _record_proposed,
    Kind.EVIDENCE_LINKED: _evidence_linked,
    Kind.USER_CONFIRM: _user_verdict,
    Kind.USER_REJECT: _user_verdict,
    Kind.RUNG_CHANGE: _rung_change,
    Kind.SCOPE_NARROWED: _scope_narrowed,
    Kind.PACKET_DELIVERED: _packet_delivered,
    Kind.COST: _cost,
}


def project(events: Iterable[Event]) -> World:
    world = World()
    for ev in events:
        world.apply(ev)
    return world

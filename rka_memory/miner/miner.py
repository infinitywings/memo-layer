"""Session-end candidate miner.

The model reads one session and proposes candidates; this module decides what
reaches the ledger. The model's output is parsed at the boundary into typed
candidates, then `plan` turns them into the only event kinds the miner may
write: RECORD_PROPOSED, EVIDENCE_LINKED, a session JUDGMENT and one COST.
Rungs stay with the validity engine, which runs right after.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, Any

from rka_memory.config import Config
from rka_memory.domain import (
    GROUNDING,
    SCOPE_FIELDS,
    Actor,
    Event,
    Kind,
    Polarity,
    RecordKind,
    RecordSpec,
    Rung,
    Scope,
    State,
    Step,
    canonical_json,
    new_id,
)
from rka_memory.miner import backends, dedup
from rka_memory.skills.export import export_skills
from rka_memory.world import RecordState, World

if TYPE_CHECKING:
    from rka_memory.service import Memory

PROMPT = Template(Path(__file__).with_name("prompt.md").read_text(encoding="utf-8"))
PAYLOAD_CHARS = 240

# What the model sees of a session: observations, not rka-memory's own bookkeeping.
SHOWN_KINDS = frozenset(
    {
        Kind.SESSION_START,
        Kind.USER_MSG,
        Kind.TOOL_CALL,
        Kind.TOOL_RESULT,
        Kind.FILE_WRITE,
        Kind.EXIT_CODE,
        Kind.TEST_RESULT,
        Kind.USER_EDIT,
        Kind.USER_UNDO,
        Kind.USER_REPEAT,
        Kind.SESSION_END,
    }
)
EDIT_KINDS = (Kind.USER_EDIT, Kind.USER_UNDO)
VERDICT_KINDS = (Kind.USER_CONFIRM, Kind.USER_REJECT)
EXISTING_RUNGS = (Rung.L1, Rung.L2, Rung.L3)


@dataclass(frozen=True)
class RecordRow:
    id: str
    kind: str
    rung: str
    content: str


@dataclass(frozen=True)
class MinerView:
    """Everything the prompt shows about one session."""

    session_id: str
    state: State
    task_classes: tuple[str, ...]
    events: tuple[Event, ...]
    delivered: tuple[RecordRow, ...]
    existing: tuple[RecordRow, ...]


@dataclass(frozen=True)
class Candidate:
    content: str
    kind: RecordKind
    broaden: frozenset[str]
    evidence: tuple[str, ...]
    falsifier: str
    confidence: float
    steps: tuple[Step, ...]
    acceptance_check: str | None
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class CounterLink:
    record_id: str
    event_id: str
    reason: str


@dataclass(frozen=True)
class Drop:
    item: str
    reason: str

    def to_json(self) -> dict[str, str]:
        return {"item": self.item, "reason": self.reason}


@dataclass(frozen=True)
class MinerOutput:
    task_class: str | None
    candidates: tuple[Candidate, ...]
    counter_links: tuple[CounterLink, ...]
    dropped: tuple[Drop, ...]


class MineStatus(StrEnum):
    MINED = "mined"
    ALREADY_MINED = "already_mined"
    UNKNOWN_SESSION = "unknown_session"
    NO_OUTPUT = "no_output"
    BACKEND_ERROR = "backend_error"
    PARSE_ERROR = "parse_error"


@dataclass
class MineResult:
    session_id: str
    status: MineStatus
    detail: str = ""
    task_class: str | None = None
    tokens: int = 0
    seconds: float = 0.0
    proposed: list[dict[str, Any]] = field(default_factory=list)
    merged: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[Drop] = field(default_factory=list)
    ungrounded: list[str] = field(default_factory=list)
    moves: list[dict[str, Any]] = field(default_factory=list)
    appended: list[str] = field(default_factory=list)
    exported: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status.value,
            "detail": self.detail,
            "task_class": self.task_class,
            "tokens": self.tokens,
            "seconds": self.seconds,
            "proposed": self.proposed,
            "merged": self.merged,
            "links": self.links,
            "dropped": [d.to_json() for d in self.dropped],
            "ungrounded": self.ungrounded,
            "moves": self.moves,
            "appended": self.appended,
            "exported": self.exported,
        }


def related_sessions(world: World, session_id: str) -> list[str]:
    """The session plus the sessions whose files the user edited or undid in it."""
    out = [session_id]
    for ev in world.sessions[session_id].events:
        write_session = ev.payload.get("write_session_id") if ev.kind in EDIT_KINDS else None
        if write_session and write_session not in out:
            out.append(write_session)
    return out


def delivered_records(world: World, session_id: str) -> list[RecordState]:
    ids = set().union(*(world.delivered_in(s) for s in related_sessions(world, session_id)))
    recs = [world.records[i] for i in ids if i in world.records]
    return sorted(recs, key=lambda r: world.position[r.proposed.id])


def _row(rec: RecordState) -> RecordRow:
    return RecordRow(rec.id, rec.spec.kind.value, rec.rung.value if rec.rung else "-", rec.spec.content)


def could_apply(scope: Scope, state: State) -> bool:
    """Scope coverage that treats fields the session has not determined yet as open,
    so an unclassified session still sees the records its classification could match."""
    return all(state.get(f) is None or scope.constraint(f).covers(state.get(f)) for f in SCOPE_FIELDS)


def view_of(world: World, session_id: str, cfg: Config) -> MinerView:
    sess = world.sessions[session_id]
    delivered = delivered_records(world, session_id)
    seen = {r.id for r in delivered}
    existing = [
        r
        for r in sorted(world.records.values(), key=lambda r: world.position[r.proposed.id])
        if r.id not in seen and r.rung in EXISTING_RUNGS and could_apply(r.scope, sess.state)
    ]
    return MinerView(
        session_id=session_id,
        state=sess.state,
        task_classes=cfg.task_classes,
        events=tuple(e for e in sess.events if e.kind in SHOWN_KINDS),
        delivered=tuple(_row(r) for r in delivered),
        existing=tuple(_row(r) for r in existing),
    )


def _short(payload: Any) -> str:
    text = canonical_json(payload)
    return text if len(text) <= PAYLOAD_CHARS else text[:PAYLOAD_CHARS] + "..."


def _rows(rows: tuple[RecordRow, ...]) -> str:
    return "\n".join(f"- {r.id} [{r.rung} {r.kind}] {r.content}" for r in rows) or "(none)"


def build_prompt(view: MinerView) -> str:
    return PROMPT.substitute(
        task_classes=", ".join(view.task_classes),
        session_id=view.session_id,
        state=canonical_json(view.state.to_json()),
        delivered=_rows(view.delivered),
        existing=_rows(view.existing),
        events="\n".join(f"{e.id} {e.kind.value} {e.actor.value} {_short(dict(e.payload))}" for e in view.events)
        or "(none)",
    )


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(v) for v in _list(value) if v))


def _confidence(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _candidate(raw: Any, index: int) -> Candidate | Drop:
    if not isinstance(raw, dict):
        return Drop(f"candidate {index}", "not a JSON object")
    content = " ".join(str(raw.get("content") or "").split())
    label = f"candidate {index}: {content[:60]}"
    if not content:
        return Drop(label, "empty content")
    try:
        kind = RecordKind(raw.get("kind"))
    except ValueError:
        return Drop(label, f"unknown kind {raw.get('kind')!r}")
    procedure = kind is RecordKind.PROCEDURE
    steps = tuple(Step.from_json(s) for s in _list(raw.get("steps")) if isinstance(s, dict) and s.get("tool"))
    check = str(raw.get("acceptance_check") or "").strip() or None
    return Candidate(
        content=content,
        kind=kind,
        broaden=frozenset(_strings(raw.get("broaden"))) & frozenset(SCOPE_FIELDS),
        evidence=_strings(raw.get("evidence")),
        falsifier=str(raw.get("falsifier") or ""),
        confidence=_confidence(raw.get("confidence", 0.5)),
        steps=steps if procedure else (),
        acceptance_check=check if procedure else None,
        depends_on=_strings(raw.get("depends_on")),
    )


def _counter_link(raw: Any, index: int) -> CounterLink | Drop:
    if not isinstance(raw, dict) or not raw.get("record_id") or not raw.get("event_id"):
        return Drop(f"counter_link {index}", "needs record_id and event_id")
    return CounterLink(str(raw["record_id"]), str(raw["event_id"]), str(raw.get("reason") or ""))


def parse_output(text: str) -> MinerOutput:
    """Parse the model's reply. Raises ValueError when it holds no JSON object."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("miner output holds no JSON object")
    try:
        doc = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"miner output is not valid JSON: {exc}") from exc
    candidates: list[Candidate] = []
    links: list[CounterLink] = []
    dropped: list[Drop] = []
    for i, raw in enumerate(_list(doc.get("candidates"))):
        parsed = _candidate(raw, i)
        (dropped if isinstance(parsed, Drop) else candidates).append(parsed)
    for i, raw in enumerate(_list(doc.get("counter_links"))):
        parsed = _counter_link(raw, i)
        (dropped if isinstance(parsed, Drop) else links).append(parsed)
    task_class = doc.get("task_class")
    return MinerOutput(
        task_class=str(task_class) if task_class else None,
        candidates=tuple(candidates),
        counter_links=tuple(links),
        dropped=tuple(dropped),
    )


def citation_problem(event_id: str, world: World, session_id: str, polarity: Polarity) -> str | None:
    """Why `event_id` cannot be cited with `polarity` by the miner of `session_id`, or None."""
    ev = world.events.get(event_id)
    if ev is None:
        return "no such event in the ledger"
    if ev.session_id != session_id:
        return "evidence outside mined session"
    if ev.actor is Actor.MODEL:
        return "model-authored event never grounds"
    if ev.kind in VERDICT_KINDS:
        return "user verdicts count only for the record they name"
    allowed = GROUNDING.get(ev.kind)
    if allowed is None:
        return f"{ev.kind.value} never grounds"
    if polarity not in allowed(ev.payload):
        return f"{ev.kind.value} cannot carry {polarity.value}"
    return None


class _Planner:
    """Turns parsed miner output into events, folding each into `world` as it goes
    so later candidates in the same batch see (and merge into) earlier ones."""

    def __init__(self, world: World, session_id: str, cfg: Config, result: MineResult) -> None:
        self.world = world
        self.session_id = session_id
        self.cfg = cfg
        self.result = result
        self.events: list[Event] = []

    def emit(self, kind: Kind, payload: dict[str, Any]) -> Event:
        state = self.world.session(self.session_id).state
        ev = Event.new(kind, Actor.MODEL, self.session_id, payload, state_hash=state.hash())
        self.world.apply(ev)
        self.events.append(ev)
        return ev

    def judge_task_class(self, value: str | None) -> None:
        if value is None:
            return
        current = self.world.session(self.session_id).state.task_class
        if value not in self.cfg.task_classes:
            self.result.dropped.append(Drop(f"task_class {value}", "not one of the configured task classes"))
            return
        if current is not None:
            if current != value:
                self.result.dropped.append(Drop(f"task_class {value}", f"session already judged {current}"))
            return
        self.emit(
            Kind.JUDGMENT,
            {
                "target": "session",
                "session_id": self.session_id,
                "field": "task_class",
                "value": value,
                "judge": "miner",
            },
        )
        self.result.task_class = value

    def cited(self, label: str, event_ids: tuple[str, ...], polarity: Polarity) -> list[str]:
        kept = []
        for eid in event_ids:
            problem = citation_problem(eid, self.world, self.session_id, polarity)
            if problem:
                self.result.dropped.append(Drop(f"{label} evidence {eid}", problem))
            else:
                kept.append(eid)
        return kept

    def link(self, rec: RecordState, event_id: str, polarity: Polarity, reason: str) -> bool:
        if any(old.event_id == event_id and old.polarity is polarity for old in rec.links):
            return False
        self.emit(
            Kind.EVIDENCE_LINKED,
            {"record_id": rec.id, "event_id": event_id, "polarity": polarity.value, "reason": reason},
        )
        return True

    def candidate(self, index: int, c: Candidate) -> None:
        label = f"candidate {index}"
        evidence = self.cited(label, c.evidence, Polarity.SUPPORT)
        state = self.world.session(self.session_id).state
        dup = dedup.find_duplicate(c.content, c.kind, self.world, state, self.cfg.dedup_threshold)
        if dup is not None:
            linked = [e for e in evidence if self.link(dup, e, Polarity.SUPPORT, "duplicate candidate cited it")]
            self.result.merged.append({"record_id": dup.id, "content": c.content, "evidence": linked})
            return
        depends_on = []
        for d in c.depends_on:
            if d in self.world.records:
                depends_on.append(d)
            else:
                self.result.dropped.append(Drop(f"{label} depends_on {d}", "no such record"))
        spec = RecordSpec(
            record_id=new_id("r"),
            kind=c.kind,
            content=c.content,
            scope=Scope.exact(state, broaden=c.broaden),
            falsifier=c.falsifier,
            confidence=c.confidence,
            steps=c.steps,
            acceptance_check=c.acceptance_check,
            depends_on=tuple(depends_on),
        )
        self.emit(Kind.RECORD_PROPOSED, {**spec.to_json(), "mined_from_session": self.session_id})
        rec = self.world.records[spec.record_id]
        for e in evidence:
            self.link(rec, e, Polarity.SUPPORT, "cited by miner")
        self.result.proposed.append(
            {"record_id": spec.record_id, "kind": c.kind.value, "content": c.content, "evidence": evidence}
        )
        if not evidence:
            self.result.ungrounded.append(spec.record_id)

    def counter(self, index: int, cl: CounterLink, delivered: set[str]) -> None:
        label = f"counter_link {index}"
        if cl.record_id not in delivered:
            self.result.dropped.append(
                Drop(f"{label} {cl.record_id}", "record was not delivered in this session or a session it edited")
            )
            return
        if not self.cited(label, (cl.event_id,), Polarity.COUNTER):
            return
        if self.link(self.world.records[cl.record_id], cl.event_id, Polarity.COUNTER, cl.reason):
            self.result.links.append(
                {"record_id": cl.record_id, "event_id": cl.event_id, "polarity": "counter", "reason": cl.reason}
            )


def plan(out: MinerOutput, world: World, session_id: str, cfg: Config, result: MineResult) -> list[Event]:
    """Validate parsed output against the ledger and return the events to append.

    Mutates `world` (the events are folded in) and fills `result`.
    """
    planner = _Planner(world, session_id, cfg, result)
    result.dropped.extend(out.dropped)
    planner.judge_task_class(out.task_class)
    delivered = {r.id for r in delivered_records(world, session_id)}
    for i, c in enumerate(out.candidates):
        planner.candidate(i, c)
    for i, cl in enumerate(out.counter_links):
        planner.counter(i, cl, delivered)
    return planner.events


def _cost(session_id: str, completion: backends.Completion) -> Event:
    payload = {
        "component": "miner",
        "session_id": session_id,
        "tokens": completion.tokens,
        "seconds": completion.seconds,
    }
    return Event.new(Kind.COST, Actor.RUNNER, session_id, payload)


def mine_session(
    memory: Memory, session_id: str, force: bool = False, backend: backends.Backend | None = None
) -> MineResult:
    """Mine one session, append what survives validation, then gate and export skills."""
    backend = backend or backends.backend_for(memory.cfg)
    with memory.ledger.exclusive("miner"):
        world = memory.world()
        sess = world.sessions.get(session_id)
        if sess is None:
            return MineResult(session_id, MineStatus.UNKNOWN_SESSION, "no events for this session")
        if sess.mined and not force:
            return MineResult(session_id, MineStatus.ALREADY_MINED, "session was mined before; use --force")
        prompt = build_prompt(view_of(world, session_id, memory.cfg))
        try:
            completion = backend(prompt)
        except backends.BackendError as exc:
            return MineResult(session_id, MineStatus.BACKEND_ERROR, str(exc))
        if not completion.text.strip():
            return MineResult(session_id, MineStatus.NO_OUTPUT, "backend returned no output")
        result = MineResult(session_id, MineStatus.MINED, tokens=completion.tokens, seconds=completion.seconds)
        cost = _cost(session_id, completion)
        try:
            out = parse_output(completion.text)
        except ValueError as exc:
            memory.append(cost)
            result.status, result.detail, result.appended = MineStatus.PARSE_ERROR, str(exc), [cost.id]
            return result
        events = plan(out, memory.world(), session_id, memory.cfg, result)
        memory.append(*events, cost)
        result.appended = [e.id for e in (*events, cost)]
    moves = memory.gate(session_id=session_id)
    result.moves = [
        {
            "record_id": m.payload["record_id"],
            "from": m.payload["from"],
            "to": m.payload["to"],
            "reason": m.payload["reason"],
        }
        for m in moves
        if m.kind is Kind.RUNG_CHANGE
    ]
    result.exported = export_skills(memory).to_json()
    return result

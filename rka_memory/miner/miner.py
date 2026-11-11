"""Session-end candidate miner.

The model reads one session and proposes candidates; this module decides what
reaches the ledger. First a narrow call lists the rules the user stated, each
as a quote that code finds in a user message (see `extract`); then the full
miner call proposes lessons in its own words. Both outputs are parsed at the
boundary into typed values, then `plan` turns them into the only event kinds
the miner may write: RECORD_PROPOSED, EVIDENCE_LINKED, a session JUDGMENT and
one COST.
Rungs stay with the validity engine, which runs right after.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, Any

from rka_memory.config import Config
from rka_memory.domain import (
    GROUNDING,
    REFUTING_KINDS,
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
from rka_memory.miner import backends, dedup, extract
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
VERDICT_KINDS = (Kind.USER_CONFIRM, Kind.USER_REJECT)
EXISTING_RUNGS = (Rung.L1, Rung.L2, Rung.L3)
RULE_KINDS = frozenset(k.value for k in extract.RULE_KINDS)
NOT_COUNTERABLE = "record was neither delivered in this session or a session it edited nor an unverified claim"


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
    anchors: tuple[str, ...] = ()
    # L1 records that apply and were not delivered: claims nothing has yet
    # shown to hold, listed apart so the miner does not build on them.
    unverified: tuple[RecordRow, ...] = ()

    @property
    def listed(self) -> tuple[RecordRow, ...]:
        return (*self.delivered, *self.existing, *self.unverified)


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
    about: tuple[str, ...] = ()
    supersedes: str | None = None
    refines: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecordLink:
    """A miner judgment that an event of the mined session supports or
    contradicts a record the prompt listed."""

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
    counter_links: tuple[RecordLink, ...]
    support_links: tuple[RecordLink, ...]
    dropped: tuple[Drop, ...]


EMPTY = MinerOutput(task_class=None, candidates=(), counter_links=(), support_links=(), dropped=())


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


def delivered_records(world: World, session_id: str) -> list[RecordState]:
    recs = [world.records[i] for i in world.received_in(session_id) if i in world.records]
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
    applicable = [
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
        existing=tuple(_row(r) for r in applicable if r.rung is not Rung.L1),
        anchors=tuple(sorted(sess.anchors)) if cfg.graph_anchors else (),
        unverified=tuple(_row(r) for r in applicable if r.rung is Rung.L1),
    )


def _short(payload: Any) -> str:
    text = canonical_json(payload)
    return text if len(text) <= PAYLOAD_CHARS else text[:PAYLOAD_CHARS] + "..."


def _rows(rows: tuple[RecordRow, ...]) -> str:
    def tag(r: RecordRow) -> str:
        return f"{r.rung} {r.kind}, unverified" if r.rung == Rung.L1.value else f"{r.rung} {r.kind}"

    return "\n".join(f"- {r.id} [{tag(r)}] {r.content}" for r in rows) or "(none)"


def build_prompt(view: MinerView, stated: tuple[str, ...] = ()) -> str:
    return PROMPT.substitute(
        task_classes=", ".join(view.task_classes),
        session_id=view.session_id,
        state=canonical_json(view.state.to_json()),
        delivered=_rows(view.delivered),
        existing=_rows(view.existing),
        unverified=_rows(view.unverified),
        stated="\n".join(f"- stated-{n}: {q}" for n, q in enumerate(stated)) or "(none)",
        anchors=", ".join(view.anchors) or "(none)",
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
        about=_strings(raw.get("about")),
        supersedes=str(raw["supersedes"]) if raw.get("supersedes") else None,
        refines=_strings(raw.get("refines")),
    )


def _record_link(raw: Any, label: str) -> RecordLink | Drop:
    if not isinstance(raw, dict) or not raw.get("record_id") or not raw.get("event_id"):
        return Drop(label, "needs record_id and event_id")
    return RecordLink(str(raw["record_id"]), str(raw["event_id"]), str(raw.get("reason") or ""))


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
    links: dict[str, list[RecordLink]] = {"counter_links": [], "support_links": []}
    dropped: list[Drop] = []
    for i, raw in enumerate(_list(doc.get("candidates"))):
        parsed = _candidate(raw, i)
        (dropped if isinstance(parsed, Drop) else candidates).append(parsed)
    for key, kept in links.items():
        for i, raw in enumerate(_list(doc.get(key))):
            parsed = _record_link(raw, f"{key[:-1]} {i}")
            (dropped if isinstance(parsed, Drop) else kept).append(parsed)
    task_class = doc.get("task_class")
    return MinerOutput(
        task_class=str(task_class) if task_class else None,
        candidates=tuple(candidates),
        counter_links=tuple(links["counter_links"]),
        support_links=tuple(links["support_links"]),
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
        # Records the prompts listed: the only ones a new record may supersede.
        self.listed: set[str] = set()
        self.rule_records: set[str] = set()
        # `stated-<n>` in the miner prompt -> the record stated rule n became.
        self.stated: dict[str, str] = {}

    def refined(self, label: str, c: Candidate) -> list[str]:
        """The listed records `c` qualifies; it may refine nothing else."""
        kept = []
        for target in c.refines:
            if target in self.listed:
                kept.append(target)
            else:
                self.result.dropped.append(Drop(f"{label} refines {target}", "refines a record that was not listed"))
        return kept

    def supersedes(self, label: str, c: Candidate, by_user: bool) -> str | None:
        """The listed record `c` replaces, or None. The miner may supersede only
        a procedure with a procedure; a user's rule is replaced only through
        the user's own words (docs/GRAPH.md, decision 1)."""
        old = c.supersedes
        if old is None:
            return None
        if by_user:
            ok, why = old in self.rule_records, "replaces a record that was not listed as a rule"
        else:
            rec = self.world.records.get(old)
            ok = old in self.listed and rec is not None and rec.spec.kind is c.kind is RecordKind.PROCEDURE
            why = "only a procedure supersedes a listed procedure"
        if not ok:
            self.result.dropped.append(Drop(f"{label} supersedes {old}", why))
            return None
        return old

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

    def link(
        self, rec: RecordState, event_id: str, polarity: Polarity, reason: str, statement: dict[str, str] | None = None
    ) -> bool:
        """`statement` carries the user's words this link quotes from the cited
        message; the validity engine checks them against the message itself."""
        if any(old.event_id == event_id and old.polarity is polarity for old in rec.links):
            return False
        payload = {"record_id": rec.id, "event_id": event_id, "polarity": polarity.value, "reason": reason}
        self.emit(Kind.EVIDENCE_LINKED, {**payload, "statement": statement} if statement else payload)
        return True

    def candidate(
        self, label: str, c: Candidate, statement: dict[str, str] | None = None, by_user: bool = False
    ) -> str | None:
        """Plan one candidate; return the record it became or merged into."""
        c = self.resolve_stated(c)
        evidence = self.cited(label, c.evidence, Polarity.SUPPORT)
        state = self.world.session(self.session_id).state
        replaces = self.supersedes(label, c, by_user)
        # A replacement differs from what it replaces by definition, however
        # close the wording ("starts with `TL;DR:`" for "starts with `Summary:`").
        dup = None if replaces else dedup.find_duplicate(c.content, c.kind, self.world, state, self.cfg.dedup_threshold)
        if dup is not None:
            linked = [e for e in evidence if self.link(dup, e, Polarity.SUPPORT, "duplicate candidate cited it", statement)]
            self.result.merged.append({"record_id": dup.id, "content": c.content, "evidence": linked})
            return dup.id
        depends_on = []
        for d in c.depends_on:
            if d in self.world.records:
                depends_on.append(d)
            else:
                self.result.dropped.append(Drop(f"{label} depends_on {d}", "no such record"))
        # A record may be about only what this session touched or its user named.
        # File writes and tool calls never ground, so the cited evidence itself
        # rarely touches the file a fact describes.
        reached = self.world.session(self.session_id).anchors
        about = []
        for a in c.about if self.cfg.graph_anchors else ():
            if a in reached:
                about.append(a)
            else:
                self.result.dropped.append(Drop(f"{label} about {a}", "the session did not touch or name it"))
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
            about=tuple(about),
            supersedes=replaces,
            refines=tuple(self.refined(label, c)),
        )
        self.emit(Kind.RECORD_PROPOSED, {**spec.to_json(), "mined_from_session": self.session_id})
        rec = self.world.records[spec.record_id]
        for e in evidence:
            self.link(rec, e, Polarity.SUPPORT, "cited by miner", statement)
        self.result.proposed.append(
            {"record_id": spec.record_id, "kind": c.kind.value, "content": c.content, "evidence": evidence}
        )
        if not evidence:
            self.result.ungrounded.append(spec.record_id)
        return spec.record_id

    def record_link(
        self,
        label: str,
        rl: RecordLink,
        polarity: Polarity,
        allowed: set[str],
        why_not: str,
        statement: dict[str, str] | None = None,
    ) -> None:
        """A counter link may name a delivered record or an unverified claim, the
        L1 records the prompt lists apart. A validated record that was not
        delivered was not tested here, so a counter link may not name it. A
        support link may name any listed record: support has to reach L1
        records, which are never delivered, or nothing accumulates evidence
        across sessions. Either way the event obeys the candidate citation rules."""
        if rl.record_id not in allowed:
            self.result.dropped.append(Drop(f"{label} {rl.record_id}", why_not))
            return
        if not self.cited(label, (rl.event_id,), polarity):
            return
        if self.link(self.world.records[rl.record_id], rl.event_id, polarity, rl.reason, statement):
            self.result.links.append(
                {"record_id": rl.record_id, "event_id": rl.event_id, "polarity": polarity.value, "reason": rl.reason}
            )

    def resolve_stated(self, c: Candidate) -> Candidate:
        """The miner prompt names this session's stated rules `stated-<n>`;
        point those references at the records the rules became."""
        if not self.stated:
            return c

        def one(ref: str) -> str:
            return self.stated.get(ref, ref)

        def each(refs: tuple[str, ...]) -> tuple[str, ...]:
            # The miner may name a restated rule both ways; keep it once.
            return tuple(dict.fromkeys(one(r) for r in refs))

        return replace(
            c,
            depends_on=each(c.depends_on),
            refines=each(c.refines),
            supersedes=one(c.supersedes) if c.supersedes else None,
        )

    def rule(self, found: extract.Located, listed: set[str]) -> str | None:
        """A rule in the user's own words: evidence for the record it restates,
        or a new record citing the message that holds it. Returns that record."""
        label, rule, event_id = f"rule {found.rule.index}", found.rule, found.message.event_id
        statement = found.statement()
        if rule.restates:
            link = RecordLink(rule.restates, event_id, "the user stated this rule again")
            self.record_link(label, link, Polarity.SUPPORT, listed, "restates a record that was not listed", statement)
            return rule.restates if rule.restates in listed else None
        return self.candidate(
            label,
            Candidate(
                content=rule.quote,
                kind=rule.kind,
                broaden=extract.BROADEN,
                evidence=(event_id,),
                falsifier="the user asks for something else",
                confidence=1.0,
                steps=(),
                acceptance_check=None,
                depends_on=(),
                supersedes=rule.replaces,
            ),
            statement,
            by_user=True,
        )

    def withdraw(self, gone: extract.Withdrawn, listed: set[str]) -> None:
        """The user's words withdrawing a listed rule: a counter link that
        quotes them, which the validity engine applies wherever the rule does."""
        w = gone.withdrawal
        link = RecordLink(w.record_id, gone.message.event_id, "the user withdrew this rule")
        self.record_link(
            f"withdrawal {w.index}",
            link,
            Polarity.COUNTER,
            listed,
            "withdraws a record that was not listed",
            {"quote": w.quote},
        )


def plan(
    out: MinerOutput,
    world: World,
    session_id: str,
    cfg: Config,
    result: MineResult,
    extraction: extract.Extraction | None = None,
) -> list[Event]:
    """Validate parsed output against the ledger and return the events to append.
    Stated rules go first, so a miner candidate that repeats one merges into it.

    Mutates `world` (the events are folded in) and fills `result`.
    """
    planner = _Planner(world, session_id, cfg, result)
    result.dropped.extend(out.dropped)
    view = view_of(world, session_id, cfg)
    counterable = {r.id for r in (*view.delivered, *view.unverified)}
    listed = {r.id for r in view.listed}
    planner.judge_task_class(out.task_class)
    rule_records = {r.id for r in view.listed if r.kind in RULE_KINDS}
    planner.listed, planner.rule_records = listed, rule_records
    for n, found in enumerate(extraction.rules if extraction else ()):
        rid = planner.rule(found, rule_records)
        if rid is not None:
            planner.stated[f"stated-{n}"] = rid
            planner.listed.add(rid)
    for gone in extraction.withdrawn if extraction else ():
        planner.withdraw(gone, rule_records)
    for i, c in enumerate(out.candidates):
        planner.candidate(f"candidate {i}", c)
    unverified = {r.id for r in view.unverified}
    for i, rl in enumerate(out.counter_links):
        ev = world.events.get(rl.event_id)
        if rl.record_id in unverified and ev is not None and ev.kind not in REFUTING_KINDS:
            # The engine would not count it: see engine.counted.
            why = f"{ev.kind.value} cannot refute a claim the session never received"
            result.dropped.append(Drop(f"counter_link {i} evidence {rl.event_id}", why))
            continue
        planner.record_link(f"counter_link {i}", rl, Polarity.COUNTER, counterable, NOT_COUNTERABLE)
    for i, rl in enumerate(out.support_links):
        planner.record_link(f"support_link {i}", rl, Polarity.SUPPORT, listed, "record was not listed in the prompt")
    return planner.events


def _cost(session_id: str, tokens: int, seconds: float) -> Event:
    payload = {"component": "miner", "session_id": session_id, "tokens": tokens, "seconds": seconds}
    return Event.new(Kind.COST, Actor.RUNNER, session_id, payload)


def stated_rules(
    world: World, view: MinerView, cfg: Config, backend: backends.Backend, result: MineResult
) -> tuple[extract.Extraction | None, backends.Completion | None]:
    """Ask which rules the user stated or withdrew in the session and keep
    what quotes a user message. Returns that with the completion to bill.
    The rule records the extractor sees are the ones the miner prompt lists."""
    messages = extract.user_messages(world, view.session_id)
    if not messages:
        return None, None
    records = [(r.id, r.content) for r in view.listed if r.kind in RULE_KINDS]
    completion = backend(extract.build_prompt(messages, records))
    if not completion.text.strip():
        return None, completion
    try:
        rules, withdrawals, dropped = extract.parse_output(completion.text)
    except ValueError as exc:
        result.dropped.append(Drop("rule extractor", str(exc)))
        return None, completion
    extraction = extract.locate(rules, withdrawals, messages, cfg.gate.standing_phrases)
    result.dropped.extend(Drop(item, reason) for item, reason in (*dropped, *extraction.dropped))
    return extraction, completion


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
        view = view_of(world, session_id, memory.cfg)
        result = MineResult(session_id, MineStatus.MINED)
        extraction: extract.Extraction | None = None
        spent: list[backends.Completion] = []
        try:
            if memory.cfg.miner_extract_rules:
                extraction, asked = stated_rules(world, view, memory.cfg, backend, result)
                if asked is not None:
                    spent.append(asked)
            stated = tuple(f.rule.quote for f in extraction.rules) if extraction else ()
            completion = backend(build_prompt(view, stated))
        except backends.BackendError as exc:
            return MineResult(session_id, MineStatus.BACKEND_ERROR, str(exc))
        if not completion.text.strip():
            return MineResult(session_id, MineStatus.NO_OUTPUT, "backend returned no output")
        spent.append(completion)
        result.tokens = sum(c.tokens for c in spent)
        result.seconds = sum(c.seconds for c in spent)
        try:
            out = parse_output(completion.text)
        except ValueError as exc:
            out = EMPTY
            result.status, result.detail = MineStatus.PARSE_ERROR, str(exc)
        events = plan(out, world, session_id, memory.cfg, result, extraction)
        cost = _cost(session_id, result.tokens, result.seconds)
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

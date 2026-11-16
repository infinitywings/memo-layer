"""Core types shared by every component: events, state, scope, records, rungs.

The ledger is the only source of truth. Records, rungs and scopes are a
projection of it (see world.py), so every type here is immutable and
serializable to the JSON that the ledger stores.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from enum import StrEnum
from typing import Any, Callable, Mapping, Sequence


class Actor(StrEnum):
    USER = "user"
    RUNNER = "runner"
    TOOL = "tool"
    ENV = "env"
    MODEL = "model"


class Kind(StrEnum):
    SESSION_START = "session_start"
    USER_MSG = "user_msg"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FILE_WRITE = "file_write"
    EXIT_CODE = "exit_code"
    TEST_RESULT = "test_result"
    USER_EDIT = "user_edit"
    USER_UNDO = "user_undo"
    USER_REPEAT = "user_repeat"
    USER_CONFIRM = "user_confirm"
    USER_REJECT = "user_reject"
    SESSION_END = "session_end"
    PACKET_DELIVERED = "packet_delivered"
    RECORD_PROPOSED = "record_proposed"
    EVIDENCE_LINKED = "evidence_linked"
    RUNG_CHANGE = "rung_change"
    SCOPE_NARROWED = "scope_narrowed"
    JUDGMENT = "judgment"
    COST = "cost"
    # A test harness removed a path between sessions: housekeeping that must
    # not read as the user undoing or editing what the agent wrote there.
    WORKSPACE_RESET = "workspace_reset"
    # An amendment the user typed: qualify, correct, replace, update, close,
    # or defeat an evidence link (DESIGN_V2 6.1).
    USER_AMEND = "user_amend"
    # A check the runner ran, logged before anything reads it (DESIGN_V2
    # 4.1, 8): a citation, a falsifier, or whether a session changed its tests.
    CHECK = "check"
    # The runner's coin for one trial candidate in one session (DESIGN_V2 5.3).
    TRIAL = "trial"


# Which actor may author each kind. The adapter sets the actor, never the
# model, so this table is what stops model text from posing as a user signal.
KIND_ACTORS: dict[Kind, frozenset[Actor]] = {
    Kind.SESSION_START: frozenset({Actor.RUNNER}),
    Kind.USER_MSG: frozenset({Actor.USER}),
    Kind.TOOL_CALL: frozenset({Actor.MODEL}),
    Kind.TOOL_RESULT: frozenset({Actor.TOOL, Actor.ENV}),
    Kind.FILE_WRITE: frozenset({Actor.TOOL}),
    Kind.EXIT_CODE: frozenset({Actor.TOOL, Actor.RUNNER}),
    Kind.TEST_RESULT: frozenset({Actor.TOOL, Actor.RUNNER}),
    Kind.USER_EDIT: frozenset({Actor.USER}),
    Kind.USER_UNDO: frozenset({Actor.USER}),
    Kind.USER_REPEAT: frozenset({Actor.USER}),
    Kind.USER_CONFIRM: frozenset({Actor.USER}),
    Kind.USER_REJECT: frozenset({Actor.USER}),
    Kind.SESSION_END: frozenset({Actor.RUNNER}),
    Kind.PACKET_DELIVERED: frozenset({Actor.RUNNER}),
    Kind.RECORD_PROPOSED: frozenset({Actor.MODEL}),
    Kind.EVIDENCE_LINKED: frozenset({Actor.MODEL, Actor.RUNNER}),
    Kind.RUNG_CHANGE: frozenset({Actor.RUNNER}),
    Kind.SCOPE_NARROWED: frozenset({Actor.RUNNER}),
    Kind.JUDGMENT: frozenset({Actor.MODEL, Actor.RUNNER}),
    Kind.COST: frozenset({Actor.RUNNER}),
    Kind.WORKSPACE_RESET: frozenset({Actor.RUNNER}),
    Kind.USER_AMEND: frozenset({Actor.USER}),
    Kind.CHECK: frozenset({Actor.RUNNER}),
    Kind.TRIAL: frozenset({Actor.RUNNER}),
}


class Polarity(StrEnum):
    SUPPORT = "support"
    COUNTER = "counter"


BOTH = frozenset({Polarity.SUPPORT, Polarity.COUNTER})
SUPPORT_ONLY = frozenset({Polarity.SUPPORT})
COUNTER_ONLY = frozenset({Polarity.COUNTER})


def _outcome(ok: bool) -> frozenset[Polarity]:
    return SUPPORT_ONLY if ok else COUNTER_ONLY


# The tool name under which the runner logs what the user's outcome check printed.
OUTCOME_CHECK = "outcome check"


def passing_outcome_check(ev: Event) -> bool:
    """What the user's outcome check printed when it passed (DESIGN_V2 5.3)."""
    return ev.actor is Actor.ENV and ev.payload.get("tool") == OUTCOME_CHECK and ev.payload.get("exit_code") == 0


# Event kinds that can ground a record, and the polarity each event can carry.
# An outcome event's polarity comes from the outcome itself, so a failing
# test can never be cited as support. Kinds absent here never ground.
GROUNDING: dict[Kind, Callable[[Event], frozenset[Polarity]]] = {
    Kind.EXIT_CODE: lambda e: _outcome(e.payload.get("code") == 0),
    Kind.TEST_RESULT: lambda e: _outcome(bool(e.payload.get("passed"))),
    Kind.USER_CONFIRM: lambda e: SUPPORT_ONLY,
    Kind.USER_REJECT: lambda e: COUNTER_ONLY,
    Kind.USER_AMEND: lambda e: SUPPORT_ONLY,
    # A passing citation or falsifier shows only that the words the model
    # chose are (or are not) in the file, not that the record follows from
    # them, so it grounds nothing; a failing one counts against the record.
    Kind.CHECK: lambda e: frozenset() if e.payload.get("passed") else COUNTER_ONLY,
    Kind.USER_EDIT: lambda e: BOTH,
    Kind.USER_UNDO: lambda e: COUNTER_ONLY,
    Kind.USER_REPEAT: lambda e: SUPPORT_ONLY,
    Kind.USER_MSG: lambda e: BOTH,
    # A passing outcome check shows that the user's requirement held, so it
    # never counts against a record. A failing one's output may also state
    # the requirement, so it keeps both.
    Kind.TOOL_RESULT: lambda e: SUPPORT_ONLY if passing_outcome_check(e) else BOTH,
}

# What a session observed, as opposed to an outcome of the agent's own work: the
# only events that can count against a claim the session never received.
REFUTING_KINDS: frozenset[Kind] = frozenset({Kind.TOOL_RESULT, Kind.USER_MSG})

# What a miner prompt shows of a session: observations, not rka-memory's own bookkeeping.
SHOWN_KINDS: frozenset[Kind] = frozenset(
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
# Shown for context, but nothing to mine on their own.
SESSION_MARKERS: frozenset[Kind] = frozenset({Kind.SESSION_START, Kind.SESSION_END})


def clock_now() -> datetime:
    """The current time, or a simulated one for workload runs.

    RKA_MEMORY_NOW pins a fixed instant. RKA_MEMORY_CLOCK_FILE names a file
    holding an ISO instant; time then runs on from it at real speed since the
    file was last written, so a workload can jump days between sessions while
    events inside a session keep their order.
    """
    fixed = os.environ.get("RKA_MEMORY_NOW")
    if fixed:
        return _aware(datetime.fromisoformat(fixed))
    path = os.environ.get("RKA_MEMORY_CLOCK_FILE")
    if path:
        try:
            return clock_file_now(Path(path))
        except (OSError, ValueError):
            pass
    return datetime.now(UTC)


def clock_file_now(path: Path) -> datetime:
    """The instant in a clock file plus the real time since it was written."""
    base = _aware(datetime.fromisoformat(path.read_text().strip()))
    return base + timedelta(seconds=max(0.0, time.time() - os.stat(path).st_mtime))


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def now_iso() -> str:
    return clock_now().isoformat(timespec="microseconds")


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def new_id(prefix: str) -> str:
    """Time-sortable random id: prefix, 13 hex digits of ms, 8 random hex."""
    return f"{prefix}_{int(time.time() * 1000):013x}{secrets.token_hex(4)}"


def derived_id(prefix: str, *parts: str) -> str:
    """Deterministic id, so re-detecting the same fact appends nothing new."""
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:21]
    return f"{prefix}_{digest}"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def collapse(text: str) -> str:
    """Whitespace runs collapsed to one space: how quotes are compared."""
    return " ".join(str(text).split())


# The one check of a statement quoted from a user message, shared by the rule
# extractor and the validity engine so the two cannot disagree.
QUOTE_CHARS = (12, 400)
MAX_STANDING_CHARS = 80
STANDING_WINDOW = 200


def quoted(text: str, quote: str) -> bool:
    """The user wrote `quote`: a run of `text`, whitespace aside, of rule length."""
    q = collapse(quote)
    return QUOTE_CHARS[0] <= len(q) <= QUOTE_CHARS[1] and q in collapse(text)


def standing(text: str, quote: str, words: str, phrases: Sequence[str]) -> bool:
    """`words` mark the quoted rule as standing: a short run of `text` that
    holds one of `phrases` (case aside) and lies within STANDING_WINDOW
    characters of the quote. Which words govern which rule stays a judgment;
    this bounds it to a known phrase next to the rule."""
    t, q, w = collapse(text), collapse(quote), collapse(words)
    if not w or len(w) > MAX_STANDING_CHARS or not any(p.lower() in w.lower() for p in phrases):
        return False
    q_at = t.find(q)
    if q_at < 0:
        return False
    at = t.find(w)
    while at >= 0:
        if max(at - (q_at + len(q)), q_at - (at + len(w)), 0) <= STANDING_WINDOW:
            return True
        at = t.find(w, at + 1)
    return False


@dataclass(frozen=True)
class Event:
    id: str
    session_id: str
    ts: str
    kind: Kind
    actor: Actor
    payload: Mapping[str, Any]
    state_hash: str = ""

    def __post_init__(self) -> None:
        if self.actor not in KIND_ACTORS[self.kind]:
            raise ValueError(f"actor {self.actor} may not author {self.kind}")

    @classmethod
    def new(
        cls,
        kind: Kind,
        actor: Actor,
        session_id: str,
        payload: Mapping[str, Any],
        *,
        state_hash: str = "",
        id: str | None = None,
        ts: str | None = None,
    ) -> Event:
        return cls(
            id=id or new_id("ev"),
            session_id=session_id,
            ts=ts or now_iso(),
            kind=kind,
            actor=actor,
            payload=dict(payload),
            state_hash=state_hash,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "ts": self.ts,
            "kind": self.kind.value,
            "actor": self.actor.value,
            "payload": dict(self.payload),
            "state_hash": self.state_hash,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Event:
        return cls(
            id=d["id"],
            session_id=d["session_id"],
            ts=d["ts"],
            kind=Kind(d["kind"]),
            actor=Actor(d["actor"]),
            payload=d.get("payload") or {},
            state_hash=d.get("state_hash", ""),
        )


SCOPE_FIELDS: tuple[str, ...] = ("task_class", "project", "toolchain", "model_id", "user_context")
FUZZY_FIELDS: frozenset[str] = frozenset({"task_class", "user_context"})


@dataclass(frozen=True)
class State:
    """What the adapter knows about the situation a session runs in."""

    task_class: str | None = None
    project: str | None = None
    toolchain: str | None = None
    model_id: str | None = None
    user_context: str | None = None

    def get(self, name: str) -> str | None:
        return getattr(self, name)

    def with_field(self, name: str, value: str | None) -> State:
        return replace(self, **{name: value})

    def to_json(self) -> dict[str, str | None]:
        return {f: self.get(f) for f in SCOPE_FIELDS}

    @classmethod
    def from_json(cls, d: Mapping[str, Any] | None) -> State:
        d = d or {}
        return cls(**{f: d.get(f) for f in SCOPE_FIELDS})

    def hash(self) -> str:
        return hashlib.sha256(canonical_json(self.to_json()).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class FieldConstraint:
    """Allowed values for one state field. include=None means any value."""

    include: frozenset[str] | None = None
    exclude: frozenset[str] = frozenset()

    def covers(self, value: str | None) -> bool:
        if value is None:
            return self.include is None
        if value in self.exclude:
            return False
        return self.include is None or value in self.include

    def without(self, value: str) -> FieldConstraint:
        if self.include is not None:
            return FieldConstraint(self.include - {value}, self.exclude)
        return FieldConstraint(None, self.exclude | {value})

    def to_json(self) -> dict[str, Any]:
        return {
            "include": sorted(self.include) if self.include is not None else None,
            "exclude": sorted(self.exclude),
        }

    @classmethod
    def from_json(cls, d: Any) -> FieldConstraint:
        if d is None or d == "*":
            return cls()
        if isinstance(d, str):
            return cls(frozenset({d}))
        inc = d.get("include")
        return cls(frozenset(inc) if inc is not None else None, frozenset(d.get("exclude") or ()))

    def describe(self) -> str:
        if self.include is None and not self.exclude:
            return "*"
        parts = []
        if self.include is not None:
            parts.append("|".join(sorted(self.include)) or "(none)")
        if self.exclude:
            parts.append("not " + "|".join(sorted(self.exclude)))
        return " ".join(parts)


@dataclass(frozen=True)
class Scope:
    task_class: FieldConstraint = field(default_factory=FieldConstraint)
    project: FieldConstraint = field(default_factory=FieldConstraint)
    toolchain: FieldConstraint = field(default_factory=FieldConstraint)
    model_id: FieldConstraint = field(default_factory=FieldConstraint)
    user_context: FieldConstraint = field(default_factory=FieldConstraint)

    def constraint(self, name: str) -> FieldConstraint:
        return getattr(self, name)

    def uncovered_fields(self, state: State) -> list[str]:
        return [f for f in SCOPE_FIELDS if not self.constraint(f).covers(state.get(f))]

    def covers(self, state: State) -> bool:
        return not self.uncovered_fields(state)

    def without(self, name: str, value: str) -> Scope:
        return replace(self, **{name: self.constraint(name).without(value)})

    def only(self, name: str, value: str) -> Scope:
        """This scope restricted to states where `name` is `value`."""
        c = self.constraint(name)
        include = frozenset({value}) if c.include is None else c.include & {value}
        return replace(self, **{name: FieldConstraint(include, c.exclude)})

    @classmethod
    def exact(cls, state: State, broaden: frozenset[str] = frozenset()) -> Scope:
        """The narrowest scope that covers `state`, with `broaden` fields left open."""
        kwargs = {}
        for f in SCOPE_FIELDS:
            value = state.get(f)
            if f in broaden or value is None:
                kwargs[f] = FieldConstraint()
            else:
                kwargs[f] = FieldConstraint(frozenset({value}))
        return cls(**kwargs)

    def to_json(self) -> dict[str, Any]:
        return {f: self.constraint(f).to_json() for f in SCOPE_FIELDS}

    @classmethod
    def from_json(cls, d: Mapping[str, Any] | None) -> Scope:
        d = d or {}
        return cls(**{f: FieldConstraint.from_json(d.get(f)) for f in SCOPE_FIELDS})

    def describe(self) -> str:
        parts = [f"{f}={self.constraint(f).describe()}" for f in SCOPE_FIELDS]
        return ", ".join(p for p in parts if not p.endswith("=*")) or "any state"


class Rung(StrEnum):
    L0D = "L0D"  # derived from model text only; stored, never shown
    L1 = "L1"  # grounded claim; hint only
    L2 = "L2"  # validated claim; may influence behavior
    L3 = "L3"  # skill; auto-loaded
    L4 = "L4"  # retired; kept to block re-promotion

    @property
    def order(self) -> int:
        return _RUNG_ORDER[self]


_RUNG_ORDER = {Rung.L0D: 0, Rung.L1: 1, Rung.L2: 2, Rung.L3: 3, Rung.L4: -1}

DELIVERABLE: frozenset[Rung] = frozenset({Rung.L2, Rung.L3})


@dataclass(frozen=True)
class Citation:
    path: str
    quote: str
    absent: bool = False

    @property
    def check(self) -> str:
        return "falsifier" if self.absent else "citation"

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "quote": self.quote}
        if self.absent:
            out["absent"] = True
        return out

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Citation:
        return cls(str(d["path"]), str(d["quote"]), bool(d.get("absent")))


class RecordKind(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    PROCEDURE = "procedure"
    CONSTRAINT = "constraint"
    # A chosen approach: in force from when it is made, deliverable only once
    # the user affirms it (DESIGN_V2 4.2).
    DECISION = "decision"


@dataclass(frozen=True)
class Step:
    """One procedure step: a tool call whose input contains `match`."""

    tool: str
    match: str

    def to_json(self) -> dict[str, str]:
        return {"tool": self.tool, "match": self.match}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Step:
        return cls(tool=str(d["tool"]), match=str(d.get("match", "")))


@dataclass(frozen=True)
class RecordSpec:
    """What the miner proposed. Immutable; rung and scope changes live in events."""

    record_id: str
    kind: RecordKind
    content: str
    scope: Scope
    falsifier: str = ""
    confidence: float = 0.5
    steps: tuple[Step, ...] = ()
    acceptance_check: str | None = None
    depends_on: tuple[str, ...] = ()
    supersedes: str | None = None
    # Anchor keys (rka_memory/anchors.py) the record is about.
    about: tuple[str, ...] = ()
    # Records this one qualifies or narrows; views keep them together.
    refines: tuple[str, ...] = ()
    # Decisions this record carries out: a validity dependency, like depends_on.
    implements: tuple[str, ...] = ()
    # For a decision: the options not chosen, and why this one.
    alternatives: tuple[str, ...] = ()
    reason: str = ""
    # Files and the words in them the record rests on; the runner checks each
    # (DESIGN_V2 8.2). With `absent`, the words must not be there: a falsifier.
    cites: tuple[Citation, ...] = ()

    @property
    def dependencies(self) -> tuple[str, ...]:
        """Every record this one's validity rests on."""
        return (*self.depends_on, *self.implements)

    def to_json(self) -> dict[str, Any]:
        out = {
            "record_id": self.record_id,
            "kind": self.kind.value,
            "content": self.content,
            "scope": self.scope.to_json(),
            "falsifier": self.falsifier,
            "confidence": self.confidence,
            "steps": [s.to_json() for s in self.steps],
            "acceptance_check": self.acceptance_check,
            "depends_on": list(self.depends_on),
            "supersedes": self.supersedes,
        }
        if self.about:
            out["about"] = list(self.about)
        if self.refines:
            out["refines"] = list(self.refines)
        if self.implements:
            out["implements"] = list(self.implements)
        if self.alternatives:
            out["alternatives"] = list(self.alternatives)
        if self.reason:
            out["reason"] = self.reason
        if self.cites:
            out["cites"] = [c.to_json() for c in self.cites]
        return out

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> RecordSpec:
        return cls(
            record_id=d["record_id"],
            kind=RecordKind(d["kind"]),
            content=d["content"],
            scope=Scope.from_json(d.get("scope")),
            falsifier=d.get("falsifier") or "",
            confidence=float(d.get("confidence", 0.5)),
            steps=tuple(Step.from_json(s) for s in d.get("steps") or ()),
            acceptance_check=d.get("acceptance_check"),
            depends_on=tuple(d.get("depends_on") or ()),
            supersedes=d.get("supersedes"),
            about=tuple(str(a) for a in d.get("about") or ()),
            refines=tuple(str(r) for r in d.get("refines") or ()),
            implements=tuple(str(r) for r in d.get("implements") or ()),
            alternatives=tuple(str(a) for a in d.get("alternatives") or ()),
            reason=str(d.get("reason") or ""),
            cites=tuple(Citation.from_json(c) for c in d.get("cites") or () if isinstance(c, Mapping) and c.get("path") and c.get("quote")),
        )

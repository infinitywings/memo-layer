"""Build fixture ledgers with deterministic ids and timestamps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from rka_memory import config as config_mod
from rka_memory.domain import (
    Actor,
    Event,
    Kind,
    RecordKind,
    RecordSpec,
    Scope,
    State,
    Step,
)
from rka_memory.kernel.labels import audit_events, label
from rka_memory.world import World, project

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def cfg(**overrides: Any):
    return config_mod.load(path=config_mod.DEFAULT_PATH, overrides=overrides)


class Ledger:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.clock = T0
        self.n = 0

    def _ev(self, kind: Kind, actor: Actor, session: str, payload: dict[str, Any]) -> Event:
        self.n += 1
        self.clock += timedelta(minutes=1)
        ev = Event.new(kind, actor, session, payload, id=f"ev_{self.n:04d}", ts=self.clock.isoformat())
        self.events.append(ev)
        return ev

    def advance(self, **delta: float) -> None:
        self.clock += timedelta(**delta)

    def start(self, session: str, **state: str) -> Event:
        return self._ev(Kind.SESSION_START, Actor.RUNNER, session, {"state": State(**state).to_json()})

    def end(self, session: str) -> Event:
        return self._ev(Kind.SESSION_END, Actor.RUNNER, session, {})

    def exit(self, session: str, code: int, command: str = "make") -> Event:
        return self._ev(Kind.EXIT_CODE, Actor.TOOL, session, {"code": code, "command": command})

    def test(self, session: str, passed: bool, command: str = "uv run pytest") -> Event:
        return self._ev(Kind.TEST_RESULT, Actor.TOOL, session, {"passed": passed, "command": command})

    def user_msg(self, session: str, text: str) -> Event:
        return self._ev(Kind.USER_MSG, Actor.USER, session, {"text": text})

    def tool_call(self, session: str, tool: str, input: str) -> Event:
        return self._ev(Kind.TOOL_CALL, Actor.MODEL, session, {"tool": tool, "input": input})

    def undo(self, session: str, write_session: str) -> Event:
        return self._ev(Kind.USER_UNDO, Actor.USER, session, {"path": "x.py", "write_session_id": write_session})

    def confirm(self, session: str, record_id: str) -> Event:
        return self._ev(Kind.USER_CONFIRM, Actor.USER, session, {"record_id": record_id})

    def reject(self, session: str, record_id: str, reason: str = "wrong") -> Event:
        return self._ev(Kind.USER_REJECT, Actor.USER, session, {"record_id": record_id, "reason": reason})

    def propose(
        self,
        session: str,
        record_id: str,
        scope: Scope | None = None,
        kind: RecordKind = RecordKind.FACT,
        **spec: Any,
    ) -> Event:
        """`mode` is a field a model might add that no rule reads; it lands in the payload."""
        steps = tuple(Step(*s) for s in spec.pop("steps", ()))
        extra = {k: spec.pop(k) for k in ("mode",) if k in spec}
        rs = RecordSpec(record_id, kind, spec.pop("content", f"content of {record_id}"), scope or Scope(), steps=steps, **spec)
        return self._ev(Kind.RECORD_PROPOSED, Actor.MODEL, session, {**rs.to_json(), **extra})

    def decide(self, session: str, record_id: str, choice: str, alternatives: tuple[str, ...] = (), reason: str = "") -> Event:
        """The agent's `decide` tool call, as the hook logs it."""
        rs = RecordSpec(record_id, RecordKind.DECISION, choice, Scope(), alternatives=alternatives, reason=reason)
        return self._ev(Kind.RECORD_PROPOSED, Actor.MODEL, session, {**rs.to_json(), "origin": "decide"})

    def amend(self, session: str, op: str, target: str, text: str = "", new_id: str | None = None,
              condition: dict[str, str] | None = None, link: str | None = None) -> Event:
        payload: dict[str, Any] = {"op": op, "target": target}
        if new_id:
            payload["record_id"] = new_id
        if text:
            payload["text"] = text
        if condition:
            payload["condition"] = condition
        if link:
            payload["link"] = link
        return self._ev(Kind.USER_AMEND, Actor.USER, session, payload)

    def read(self, session: str, path: str) -> Event:
        """A file tool read of `path`; returns its result."""
        tid = f"rd{self.n}"
        self._ev(Kind.TOOL_CALL, Actor.MODEL, session, {"tool": "Read", "input": path, "tool_use_id": tid})
        return self._ev(Kind.TOOL_RESULT, Actor.TOOL, session, {"tool": "Read", "tool_use_id": tid, "ok": True})

    def check(self, session: str, check: str, passed: bool, record_id: str | None = None, paths=()) -> Event:
        payload: dict[str, Any] = {"check": check, "passed": passed, "paths": list(paths)}
        if record_id:
            payload["record_id"] = record_id
        return self._ev(Kind.CHECK, Actor.RUNNER, session, payload)

    def assign(self, session: str, record_id: str, arm: str) -> Event:
        return self._ev(Kind.TRIAL, Actor.RUNNER, session, {"record_id": record_id, "arm": arm, "q": 0.5})

    def write(self, session: str, path: str) -> Event:
        return self._ev(Kind.FILE_WRITE, Actor.TOOL, session, {"tool": "write", "tool_use_id": f"w{self.n}", "path": path})

    def link(self, record_id: str, event: Event, polarity: str = "support", statement: dict | None = None) -> Event:
        payload = {"record_id": record_id, "event_id": event.id, "polarity": polarity}
        if statement is not None:
            payload["statement"] = statement
        return self._ev(Kind.EVIDENCE_LINKED, Actor.MODEL, event.session_id, payload)

    def deliver(self, session: str, *record_ids: str) -> Event:
        return self._ev(Kind.PACKET_DELIVERED, Actor.RUNNER, session, {"record_ids": list(record_ids), "arm": "gated"})

    def gate(self, config=None) -> World:
        """Label at the current clock and keep the derived audit events, as
        `Memory.gate` does; return the projection with their cached rungs."""
        new = audit_events(project(self.events), label(project(self.events), config or cfg(), self.clock), self.clock)
        self.events.extend(new)
        return project(self.events)


def rung(world: World, record_id: str) -> str | None:
    r = world.records[record_id].rung
    return r.value if r else None


def moves(world: World, record_id: str) -> list[tuple[str | None, str, str]]:
    return [
        (m.from_rung.value if m.from_rung else None, m.to_rung.value, m.reason)
        for m in world.records[record_id].history
    ]

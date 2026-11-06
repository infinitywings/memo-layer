"""rka-memory MCP server (stdio).

Each tool is a thin wrapper over a plain function that takes a `Memory`, so the
behavior is testable without the MCP runtime. The model can call every tool
here, so none of them may write a user signal: confirm and reject only log a
request for the user to give the verdict themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from rka_memory.domain import Actor, Event, Kind, State
from rka_memory.service import Memory

MCP_SESSION = "mcp"

Detector = Callable[[Memory], State]


def detect_cwd(memory: Memory) -> State:
    from rka_memory.ledger.state import detect

    return detect(Path.cwd(), memory.cfg)


def bound_session(memory: Memory, session_id: str | None) -> str:
    """The model names the session, so accept only one that is running now;
    anything else is logged under the shared MCP session."""
    if session_id:
        sess = memory.world().sessions.get(session_id)
        if sess is not None and sess.started is not None and sess.ended is None:
            return session_id
    return MCP_SESSION


def _state(memory: Memory, state: dict[str, Any] | None, detect: Detector) -> State:
    return State.from_json(state) if state is not None else detect(memory)


def packet(
    memory: Memory,
    state: dict[str, Any] | None = None,
    budget_tokens: int | None = None,
    session_id: str | None = None,
    *,
    detect: Detector = detect_cwd,
) -> dict[str, Any]:
    pkt = memory.deliver(
        bound_session(memory, session_id), _state(memory, state, detect), channel="mcp", budget_tokens=budget_tokens
    )
    return {
        "records": [i.to_json() for i in pkt.items],
        "omitted_count": pkt.omitted_count,
        "ledger_revision": pkt.ledger_revision,
        "arm": pkt.arm,
        "text": pkt.render(),
    }


def hints(
    memory: Memory,
    state: dict[str, Any] | None = None,
    session_id: str | None = None,
    *,
    detect: Detector = detect_cwd,
) -> dict[str, Any]:
    pkt = memory.deliver(bound_session(memory, session_id), _state(memory, state, detect), channel="hints")
    return {
        "records": [{**i.to_json(), "tag": "unvalidated"} for i in pkt.items],
        "omitted_count": pkt.omitted_count,
        "ledger_revision": pkt.ledger_revision,
        "text": pkt.render(),
    }


def request_verdict(memory: Memory, record_id: str, verdict: str, reason: str = "") -> dict[str, Any]:
    """Log the agent's request as a model JUDGMENT and hand the verdict to the user."""
    if record_id not in memory.world().records:
        return {"error": f"unknown record {record_id}"}
    ev = Event.new(
        Kind.JUDGMENT,
        Actor.MODEL,
        MCP_SESSION,
        {"target": "verdict_request", "record_id": record_id, "verdict": verdict, "reason": reason, "judge": "model"},
    )
    memory.append(ev)
    command = f"rkm {verdict} {record_id}" + (f" {reason}" if reason else "")
    return {
        "requested": verdict,
        "record_id": record_id,
        "event_id": ev.id,
        "message": (
            f"rka-memory does not take verdicts from the agent. Ask the user to type `{command}` "
            "as their next prompt if they agree; only that counts."
        ),
    }


def explain(memory: Memory, record_id: str) -> dict[str, Any]:
    if record_id not in memory.world().records:
        return {"error": f"unknown record {record_id}"}
    return memory.explain(record_id)


def state_report(memory: Memory, session_id: str | None = None, *, detect: Detector = detect_cwd) -> dict[str, Any]:
    out: dict[str, Any] = {"detected": detect(memory).to_json()}
    if session_id is not None:
        sess = memory.world().sessions.get(session_id)
        out["session"] = sess.state.to_json() if sess else None
    return out


def build_server(open_memory: Callable[[], Memory] = Memory.open) -> FastMCP:
    server = FastMCP("rka-memory")

    @server.tool()
    def memory_packet(
        state: dict[str, Any] | None = None, budget_tokens: int | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        """Validated memory (L2 claims and L3 skills) that applies to the current state.

        `state` holds task_class, project, toolchain, model_id, user_context; omit it
        to use the state detected from the working directory. Fields left out of a
        given state count as unknown. The delivery is logged.
        """
        return packet(open_memory(), state, budget_tokens, session_id)

    @server.tool()
    def memory_hints(state: dict[str, Any] | None = None, session_id: str | None = None) -> dict[str, Any]:
        """Unvalidated L1 hints for the current state.

        Each hint is grounded but not validated, so check it before relying on it.
        """
        return hints(open_memory(), state, session_id)

    @server.tool()
    def memory_confirm(record_id: str) -> dict[str, Any]:
        """Ask the user to confirm a record. The agent cannot confirm records itself."""
        return request_verdict(open_memory(), record_id, "confirm")

    @server.tool()
    def memory_reject(record_id: str, reason: str) -> dict[str, Any]:
        """Ask the user to reject a record that looks wrong. The agent cannot reject records itself."""
        return request_verdict(open_memory(), record_id, "reject", reason)

    @server.tool()
    def memory_explain(record_id: str) -> dict[str, Any]:
        """Rung history, scope, evidence and deliveries of one record."""
        return explain(open_memory(), record_id)

    @server.tool()
    def memory_state(session_id: str | None = None) -> dict[str, Any]:
        """The state detected for the working directory and, given a session id, the state recorded for that session."""
        return state_report(open_memory(), session_id)

    return server


def main() -> None:
    build_server().run()

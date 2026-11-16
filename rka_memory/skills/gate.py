"""What a session did with a procedure: helpers for the kernel's skill rule.

A procedure is followed when a session's tool calls contain its steps in
order. Its acceptance check is matched against commands the agent already
ran; rka-memory never executes it on its own, so a model-proposed command
cannot run unattended. The rule itself is in `kernel.labels._skill`.
"""

from __future__ import annotations

from rka_memory.domain import GROUNDING, SUPPORT_ONLY, Event, Kind, Step
from rka_memory.world import RecordState, Session

OUTCOME_KINDS = (Kind.EXIT_CODE, Kind.TEST_RESULT)


def skill_name(record_id: str) -> str:
    return "rka-" + record_id.replace("_", "-").lower()


def normalize_command(cmd: str) -> str:
    return " ".join(cmd.split())


def outcome_ok(ev: Event) -> bool:
    return GROUNDING[ev.kind](ev) == SUPPORT_ONLY


def _step_matches(step: Step, ev: Event) -> bool:
    return (
        str(ev.payload.get("tool", "")).lower() == step.tool.lower()
        and step.match.lower() in str(ev.payload.get("input", "")).lower()
    )


def follows(session: Session, steps: tuple[Step, ...]) -> bool:
    """True when the session's tool calls contain `steps` as an ordered subsequence."""
    if not steps:
        return False
    i = 0
    for ev in session.events:
        if ev.kind is Kind.TOOL_CALL and i < len(steps) and _step_matches(steps[i], ev):
            i += 1
    return i == len(steps)


def final_outcome(session: Session) -> Event | None:
    outcomes = [e for e in session.events if e.kind in OUTCOME_KINDS]
    return outcomes[-1] if outcomes else None


def _used_skill(sess: Session, rec: RecordState) -> int | None:
    """Index into sess.events from which the skill was in use, or None."""
    name = skill_name(rec.id)
    for i, ev in enumerate(sess.events):
        if ev.kind is Kind.TOOL_CALL and str(ev.payload.get("tool", "")).lower() == "skill":
            if name in str(ev.payload.get("input", "")).lower():
                return i
    return 0 if follows(sess, rec.spec.steps) else None

"""Skill rung rules as pure functions over the projection.

L2 -> L3 needs later sessions inside scope that followed the procedure's steps
and ended on a successful strong outcome. L3 -> L4 happens when the frozen
acceptance check fails in a session that used the skill, or when the user
undoes a file written in such a session. The acceptance check is matched
against commands the agent already ran; rka-memory never executes it on its
own, so a model-proposed command cannot run unattended.
"""

from __future__ import annotations

from rka_memory.domain import GROUNDING, SUPPORT_ONLY, Actor, Event, Kind, Step
from rka_memory.world import RecordState, Session, World

OUTCOME_KINDS = (Kind.EXIT_CODE, Kind.TEST_RESULT)


def skill_name(record_id: str) -> str:
    return "rka-" + record_id.replace("_", "-").lower()


def normalize_command(cmd: str) -> str:
    return " ".join(cmd.split())


def outcome_ok(ev: Event) -> bool:
    return GROUNDING[ev.kind](ev.payload) == SUPPORT_ONLY


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


def _sessions_after(world: World, event_id: str):
    for sess in world.sessions.values():
        if sess.started is not None and sess.ended is not None and world.before(event_id, sess.started.id):
            yield sess


def sessions_following(rec: RecordState, world: World) -> list[tuple[Session, Event]]:
    """Ended sessions after the latest L2 promotion that followed the steps and succeeded."""
    promoted = rec.last_move_to(rec.rung) if rec.rung else None
    if promoted is None:
        return []
    found = []
    for sess in _sessions_after(world, promoted.event_id):
        if not rec.scope.covers(sess.state) or not follows(sess, rec.spec.steps):
            continue
        fin = final_outcome(sess)
        if fin is not None and outcome_ok(fin):
            found.append((sess, fin))
    return found


def check_passed(sessions: list[Session], check: str) -> Event | None:
    """A tool-observed run of exactly `check` that succeeded in one of `sessions`.

    The check is proposed by the miner, so it reaches an auto-loading skill
    only after the agent has really run it and it passed.
    """
    want = normalize_command(check)
    for sess in sessions:
        for ev in sess.events:
            if (
                ev.kind in OUTCOME_KINDS
                and ev.actor is Actor.TOOL
                and normalize_command(str(ev.payload.get("command", ""))) == want
                and outcome_ok(ev)
            ):
                return ev
    return None


def _used_skill(sess: Session, rec: RecordState) -> int | None:
    """Index into sess.events from which the skill was in use, or None."""
    name = skill_name(rec.id)
    for i, ev in enumerate(sess.events):
        if ev.kind is Kind.TOOL_CALL and str(ev.payload.get("tool", "")).lower() == "skill":
            if name in str(ev.payload.get("input", "")).lower():
                return i
    return 0 if follows(sess, rec.spec.steps) else None


def skill_failure(rec: RecordState, world: World) -> tuple[Event, str] | None:
    """The first post-promotion event that retires an L3 skill, with the reason."""
    promoted = rec.last_move_to(rec.rung) if rec.rung else None
    if promoted is None or not rec.acceptance_check:
        return None
    check = normalize_command(rec.acceptance_check)
    using: set[str] = set()
    for sess in _sessions_after(world, promoted.event_id):
        start = _used_skill(sess, rec)
        if start is None:
            continue
        using.add(sess.id)
        runs = [
            e
            for e in sess.events[start:]
            if e.kind in OUTCOME_KINDS and normalize_command(str(e.payload.get("command", ""))) == check
        ]
        if runs and not outcome_ok(runs[-1]):
            return runs[-1], "acceptance check failed after the skill was used"
    for ev in world.events.values():
        if ev.kind is not Kind.USER_UNDO or not world.before(promoted.event_id, ev.id):
            continue
        if ev.payload.get("write_session_id") in using:
            return ev, "user undid a file written while the skill was in use"
    return None

"""Randomized trials for prescriptive records (DESIGN_V2 5.3).

A prescriptive record at L1 whose own counted support meets the threshold
that would validate a fact is a trial candidate. In each gated session where
it applies, the runner logs a coin before building the packet: with chance
`q` the candidate is delivered (treated), otherwise withheld. The trial is
decided once, when both arms first hold `m` ended sessions: the record goes
to L2 if a one-sided Fisher exact test finds the treated arm's share of good
outcomes higher than the withheld arm's at level `alpha`, and closes at L1
otherwise; a closed trial never reopens. A session's outcome, for every
prescriptive kind, is good when the user left it uncorrected (no undo or
edit of what it wrote, no repeated request) and the user's outcome check,
if one ran, passed. The PI registered q = 0.5 and m = 3 before any run, and
alpha = 0.05 in place of the margin delta = 1/3 after the trial-ceiling
study.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from math import comb

from rka_memory.config import Config
from rka_memory.domain import Kind, RecordKind
from rka_memory.world import RecordState, Session, World

PRESCRIPTIVE = frozenset({RecordKind.PROCEDURE, RecordKind.CONSTRAINT, RecordKind.PREFERENCE, RecordKind.DECISION})
CORRECTIONS = frozenset({Kind.USER_UNDO, Kind.USER_EDIT})


def coin(session_id: str, record_id: str) -> float:
    """A uniform draw in [0, 1) fixed by the session and the record, so a
    replay or a second packet in the same session gets the same arm."""
    digest = hashlib.sha256(f"{session_id}\x1f{record_id}".encode()).hexdigest()
    return int(digest[:12], 16) / 16**12


def fisher(treated: tuple[int, int], withheld: tuple[int, int]) -> float:
    """One-sided Fisher exact p: the chance, with the arms' outcomes
    exchangeable, that the treated arm holds at least as many good outcomes
    as it does. `treated` and `withheld` are (sessions, good outcomes)."""
    (tn, tk), (wn, wk) = treated, withheld
    good = tk + wk
    return sum(comb(good, x) * comb(tn + wn - good, tn - x) for x in range(tk, min(good, tn) + 1)) / comb(tn + wn, tn)


@dataclass(frozen=True)
class Trial:
    status: str  # "open", "positive" or "closed"
    treated: tuple[int, int]  # sessions, of which with a good outcome
    withheld: tuple[int, int]
    completed_at: int | None = None  # ledger position of the session end that completed it
    p: float | None = None

    def reason(self) -> str:
        (tn, tk), (wn, wk) = self.treated, self.withheld
        if self.status == "open":
            return f"in trial: treated {tn}, withheld {wn} sessions"
        verdict = "positive interventional contrast" if self.status == "positive" else "trial closed without a positive contrast"
        return f"{verdict}: treated {tk}/{tn}, withheld {wk}/{wn}, p = {self.p:.3f}"


def _corrected(world: World, session: Session) -> bool:
    if any(e.kind is Kind.USER_REPEAT for e in session.events):
        return True
    return any(
        e.kind in CORRECTIONS and e.payload.get("write_session_id") == session.id for e in world.events.values()
    )


def _outcome_check(session: Session) -> bool | str | None:
    """The verdict of the session's last outcome check: True or False, None
    when it ran none, and "due" when one was due and never logged a result."""
    checks = [e for e in session.events if e.kind is Kind.CHECK and e.payload.get("check") == "outcome"]
    if not checks:
        return None
    last = checks[-1].payload
    return "due" if "passed" not in last else bool(last["passed"])


def outcome(world: World, session: Session) -> bool:
    return _outcome_check(session) is not False and not _corrected(world, session)


def failed(world: World, session: Session) -> bool:
    """The user corrected the session, its outcome check failed or never
    logged a result, or its last test run failed. A pending restriction
    withholds only a target that was in context in such a session
    (DESIGN_V2 6.1)."""
    check = _outcome_check(session)
    tests = [e for e in session.events if e.kind is Kind.TEST_RESULT]
    return check is False or check == "due" or _corrected(world, session) or bool(tests and not tests[-1].payload.get("passed"))


def evaluate(rec: RecordState, world: World, cfg: Config, suspect: frozenset[str] = frozenset()) -> Trial:
    """The record's trial, from the logged coins and the ended sessions.

    A `suspect` session still counts in its arm, but its outcome is set
    against promotion: a failure if treated, a success if withheld. These
    are sessions that changed their own tests, sessions that had a withdrawn
    record in context, and sessions whose outcome check was due and never
    logged a result. Dropping them instead would let what happens after the
    coin choose which sessions count; scored this way, no such session can
    help a record pass."""
    arms: dict[str, str] = {}
    for ev in world.trials:
        if ev.payload.get("record_id") == rec.id and ev.session_id not in arms:
            arms[ev.session_id] = str(ev.payload.get("arm"))
    ended = sorted(
        (world.position[s.ended.id], s)
        for sid in arms
        if (s := world.sessions.get(sid)) is not None and s.ended is not None
    )
    counts = {"treated": [0, 0], "withheld": [0, 0]}
    m = cfg.trials.m
    for pos, sess in ended:
        arm = counts.get(arms[sess.id])
        if arm is None:
            continue
        arm[0] += 1
        against = sess.id in suspect or _outcome_check(sess) == "due"
        arm[1] += (arms[sess.id] == "withheld") if against else outcome(world, sess)
        if counts["treated"][0] >= m and counts["withheld"][0] >= m:
            treated, withheld = tuple(counts["treated"]), tuple(counts["withheld"])
            p = fisher(treated, withheld)
            return Trial("positive" if p <= cfg.trials.alpha + 1e-12 else "closed", treated, withheld, pos, p)
    return Trial("open", tuple(counts["treated"]), tuple(counts["withheld"]))


def candidates(world: World, labels, cfg: Config) -> list[str]:
    """Records in an open trial, in log order."""
    return [
        r.id for r in sorted(world.records.values(), key=lambda r: world.position[r.proposed.id])
        if labels.by_id[r.id].trial == "open"
    ]

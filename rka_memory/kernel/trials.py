"""Randomized trials for prescriptive records (DESIGN_V2 5.3).

A prescriptive record at L1 whose own counted support meets the threshold
that would validate a fact is a trial candidate. In each gated session where
it applies, the runner logs a coin before building the packet: with chance
`q` the candidate is delivered (treated), otherwise withheld. The trial ends
when both arms hold `m` ended sessions. The record goes to L2 if the treated
arm's outcome share beats the withheld arm's by at least `delta`, and closes
at L1 otherwise; a closed trial never reopens. The outcome of a session is,
for a procedure or a decision, whether its final check passed, and for a
preference or a constraint, whether the user left the session uncorrected
(no undo or edit of what it wrote, no repeated request). The PI registered
q = 0.5, m = 3 and delta = 1/3 before any run.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from rka_memory.config import Config
from rka_memory.domain import Kind, RecordKind
from rka_memory.skills.gate import final_outcome, outcome_ok
from rka_memory.world import RecordState, Session, World

PRESCRIPTIVE = frozenset({RecordKind.PROCEDURE, RecordKind.CONSTRAINT, RecordKind.PREFERENCE, RecordKind.DECISION})
BY_OUTCOME = frozenset({RecordKind.PROCEDURE, RecordKind.DECISION})
CORRECTIONS = frozenset({Kind.USER_UNDO, Kind.USER_EDIT})


def coin(session_id: str, record_id: str) -> float:
    """A uniform draw in [0, 1) fixed by the session and the record, so a
    replay or a second packet in the same session gets the same arm."""
    digest = hashlib.sha256(f"{session_id}\x1f{record_id}".encode()).hexdigest()
    return int(digest[:12], 16) / 16**12


@dataclass(frozen=True)
class Trial:
    status: str  # "open", "positive" or "closed"
    treated: tuple[int, int]  # sessions, of which with a good outcome
    withheld: tuple[int, int]
    completed_at: int | None = None  # ledger position of the session end that completed it

    def reason(self) -> str:
        (tn, tk), (wn, wk) = self.treated, self.withheld
        if self.status == "open":
            return f"in trial: treated {tn}, withheld {wn} sessions"
        verdict = "positive interventional contrast" if self.status == "positive" else "trial closed without a positive contrast"
        return f"{verdict}: treated {tk}/{tn}, withheld {wk}/{wn}"


def _corrected(world: World, session: Session) -> bool:
    if any(e.kind is Kind.USER_REPEAT for e in session.events):
        return True
    return any(
        e.kind in CORRECTIONS and e.payload.get("write_session_id") == session.id for e in world.events.values()
    )


def outcome(rec: RecordState, world: World, session: Session) -> bool:
    if rec.spec.kind in BY_OUTCOME:
        fin = final_outcome(session)
        return fin is not None and outcome_ok(fin)
    return not _corrected(world, session)


def evaluate(rec: RecordState, world: World, cfg: Config) -> Trial:
    """The record's trial, from the logged coins and the ended sessions."""
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
        arm[1] += outcome(rec, world, sess)
        if counts["treated"][0] >= m and counts["withheld"][0] >= m:
            (tn, tk), (wn, wk) = counts["treated"], counts["withheld"]
            positive = tk / tn - wk / wn >= cfg.trials.delta - 1e-9
            return Trial("positive" if positive else "closed", (tn, tk), (wn, wk), pos)
    return Trial("open", tuple(counts["treated"]), tuple(counts["withheld"]))


def candidates(world: World, labels, cfg: Config) -> list[str]:
    """Records in an open trial, in log order."""
    return [
        r.id for r in sorted(world.records.values(), key=lambda r: world.position[r.proposed.id])
        if labels.by_id[r.id].trial == "open"
    ]

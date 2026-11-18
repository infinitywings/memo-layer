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

Under attested validation (DESIGN_V3 5, step 3) a trial is sequential and
never closes. Its wealth is a mixture e-process on the coins: each session
multiplies it by 1 + u * 2 min(q, 1 - q) * Z, Z = (Y - 1/2)(A - q) / (q(1 - q)),
averaged over u in {0.05, ..., 0.95}. Under no effect its mean never grows,
whatever the base rate and even if it drifts, so promoting at wealth 20
promotes a useless record at most 5% of the time, over any number of
sessions (Ville's inequality). While the posterior chance that the treated
arm does better is at most 0.1 the record is on hold: still randomized, at a
lower q, so a harmful record is delivered less and can still recover.
Records that are linked or alike share one coin per session (`clusters`).
"""

from __future__ import annotations

import hashlib
import math
import re
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
    status: str  # "open", "positive" or "closed"; sequential trials also "hold"
    treated: tuple[int, int]  # sessions, of which with a good outcome
    withheld: tuple[int, int]
    completed_at: int | None = None  # ledger position of the session end that completed it
    p: float | None = None
    wealth: float | None = None  # sequential trials: the e-process after the last session counted

    def reason(self) -> str:
        (tn, tk), (wn, wk) = self.treated, self.withheld
        if self.wealth is not None:
            if self.status == "positive":
                return f"positive sequential contrast: treated {tk}/{tn}, withheld {wk}/{wn}, wealth {self.wealth:.1f}"
            hold = ", on hold" if self.status == "hold" else ""
            return f"in trial{hold}: treated {tk}/{tn}, withheld {wk}/{wn}, wealth {self.wealth:.2f}"
        if self.status == "open":
            return f"in trial: treated {tn}, withheld {wn} sessions"
        verdict = "positive interventional contrast" if self.status == "positive" else "trial closed without a positive contrast"
        return f"{verdict}: treated {tk}/{tn}, withheld {wk}/{wn}, p = {self.p:.3f}"


U_GRID = tuple(k / 20 for k in range(1, 20))


def _logbeta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def p_better(treated: tuple[int, int], withheld: tuple[int, int]) -> float:
    """P(p_treated > p_withheld) under independent Beta(1, 1) priors, exact
    (Miller's sum, finite for an integer first parameter). `treated` and
    `withheld` are (sessions, good outcomes)."""
    (tn, tk), (wn, wk) = treated, withheld
    ab, bb, aa, ba = 1 + tk, 1 + tn - tk, 1 + wk, 1 + wn - wk
    base = _logbeta(aa, ba)
    v = sum(math.exp(_logbeta(aa + i, ba + bb) - math.log(bb + i) - _logbeta(1 + i, bb) - base) for i in range(ab))
    return min(max(v, 0.0), 1.0)


def wealth_factors(arm: str, q: float, good: bool) -> list[float]:
    """One session's factor in the wealth, for each u in U_GRID."""
    a, y = (1 if arm == "treated" else 0), (1 if good else 0)
    z = (y - 0.5) * (a - q) / (q * (1 - q))
    return [1 + u * 2 * min(q, 1 - q) * z for u in U_GRID]


def evaluate_sequential(rec: RecordState, world: World, cfg: Config, suspect: frozenset[str] = frozenset()) -> Trial:
    """The record's sequential trial (DESIGN_V3 step 3), scored session by
    session in the order they ended. Suspect sessions count against
    promotion, as in `evaluate`."""
    coins: dict[str, tuple[str, float]] = {}
    for ev in world.trials:
        if ev.payload.get("record_id") == rec.id and ev.session_id not in coins:
            q = float(ev.payload.get("q") or cfg.trials.q)
            coins[ev.session_id] = (str(ev.payload.get("arm")), q)
    ended = sorted(
        (world.position[s.ended.id], s)
        for sid in coins
        if (s := world.sessions.get(sid)) is not None and s.ended is not None
    )
    logs = [0.0] * len(U_GRID)
    counts = {"treated": [0, 0], "withheld": [0, 0]}
    wealth = 1.0
    for pos, sess in ended:
        arm, q = coins[sess.id]
        if arm not in counts or not 0 < q < 1:
            continue
        against = sess.id in suspect or _outcome_check(sess) == "due"
        good = (arm == "withheld") if against else outcome(world, sess)
        counts[arm][0] += 1
        counts[arm][1] += good
        logs = [w + math.log(f) for w, f in zip(logs, wealth_factors(arm, q, good))]
        wealth = sum(math.exp(w) for w in logs) / len(logs)
        floor = cfg.trials.min_per_arm
        if counts["treated"][0] >= floor and counts["withheld"][0] >= floor and wealth >= cfg.trials.e_threshold:
            return Trial("positive", tuple(counts["treated"]), tuple(counts["withheld"]), pos, wealth=wealth)
    treated, withheld = tuple(counts["treated"]), tuple(counts["withheld"])
    status = "hold" if p_better(treated, withheld) <= cfg.trials.hold_below else "open"
    return Trial(status, treated, withheld, wealth=wealth)


_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]+")


def tokens(text: str) -> frozenset[str]:
    """Words, and for CJK text character bigrams (a lone character stands
    for itself), so records in Chinese compare as records in English do."""
    low = text.lower()
    out = set(_WORD.findall(low))
    for run in _CJK.findall(low):
        out.update(run[i : i + 2] for i in range(max(len(run) - 1, 1)))
    return frozenset(out)


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def clusters(world: World, record_ids: list[str], cfg: Config) -> dict[str, str]:
    """For records on trial: the key of the cluster each belongs to, its
    earliest record. Records linked by an edge, or whose contents are alike,
    share a cluster, so one carrying the same advice as another cannot reach
    the other's withheld sessions."""
    parent = {rid: rid for rid in record_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            first = min((ra, rb), key=lambda r: world.position[world.records[r].proposed.id])
            parent[ra if first == rb else rb] = first

    for i, a in enumerate(record_ids):
        spec = world.records[a].spec
        edges = {*spec.refines, *spec.dependencies, *spec.implements, *([spec.supersedes] if spec.supersedes else [])}
        for b in record_ids[i + 1 :]:
            other = world.records[b].spec
            linked = b in edges or a in {*other.refines, *other.dependencies, *other.implements, other.supersedes}
            if linked or similarity(spec.content, other.content) >= cfg.trials.cluster_similarity:
                join(a, b)
    return {rid: find(rid) for rid in record_ids}


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
        if labels.by_id[r.id].trial in ("open", "hold")
    ]

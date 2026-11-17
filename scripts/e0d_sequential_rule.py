"""E0d: a sequential replacement for the one-shot trial rule.

Pre-registered in RKA jrn_01M7NGP5QFYXE19PQEDYYYXDJM, motivated by
docs/EXTERNAL_REVIEW_1.md (M1, answer 3). Offline: no docker, no LLM, no
RKA Core. Writes workload-runs/e0d/results.md and results.json.

Usage: .venv/bin/python scripts/e0d_sequential_rule.py [--reps 100000] [--jobs N]

Model. One record on trial. Each applicable gated session draws a coin: with
chance q_i the record is delivered (treated, A_i = 1), otherwise withheld
(A_i = 0). q_i may depend only on earlier sessions. The session's outcome
Y_i is 1 (good: the user's outcome check passed and nothing was corrected)
with chance pT if treated and pW if withheld, independently across sessions.
Counts after n sessions: tn treated sessions of which tk good, wn withheld
of which wk good. A promoted record is delivered in every later session; a
closed one is never delivered again; otherwise it is delivered exactly when
treated. "Null" is pT = pW.

Rules (q = 0.5 unless stated; "min arms 2" means tn >= 2 and wn >= 2):

A3  The current rule (rka_memory/kernel/trials.py). At the first session
    after which tn >= 3 and wn >= 3, take the one-sided Fisher exact p of
    (tn, tk) against (wn, wk) (the repository's own `fisher`); promote if
    p <= 0.05, otherwise close for good.
A8  The same with m = 8.
SF  Sequential Fisher. After every session with min arms 2, promote if the
    one-sided Fisher p <= alpha*. Never closes. alpha* tuned (below).
SB  Beta-Binomial posterior. Independent Beta(1, 1) priors on pT and pW;
    P = P(pT > pW | data), exact for integer parameters. After every
    session with min arms 2, promote if P >= c. Never closes. A record with
    P <= h = 0.1 is labelled "on hold" (shown for review) but q stays 0.5.
SBH Beta-Binomial with hold. As SB, but while P <= h (h = 0.1) the record
    is on hold: q drops to q_hold = 0.1 for that session (it is still
    randomized, so evidence keeps arriving and it can leave hold). c tuned.
SE  Mixture e-process on the randomization. Per session
    Z_i = (Y_i - 1/2)(A_i - q_i) / (q_i (1 - q_i)), whose mean given the past
    is pT - pW whatever the base rate. With q = 0.5, Z_i = +1 when the
    session is concordant (treated and good, or withheld and bad) and -1
    otherwise. E_n = mean over u in {0.05, 0.10, ..., 0.95} of
    prod_i (1 + u * 2 min(q_i, 1 - q_i) * Z_i). Under any null pT <= pW (the
    base rate may even drift from session to session) E_n is a nonnegative
    supermartingale, so P(E_n ever >= 1/a) <= a (Ville). Promote at the
    first session with min arms 2 and E_n >= tau. Never closes. tau tuned.
SEv SE with tau = 20 = 1/0.05: anytime-valid, with no horizon.
SEH SE with q lowered as evidence against builds (rule d): q_i = 0.1 while
    the SB posterior P <= 0.1, else 0.5; E_n as in SE, which stays valid
    because q_i is fixed before session i. tau tuned.

Tuning. Each tuned rule takes the most permissive threshold whose null
promotion within 40 applicable sessions is at most 5% at base rates 0.1,
0.5 and 0.9. For SF, SB, SBH and SE the null promotion is computed exactly
by dynamic programming over (tn, tk, wn, wk); SEH depends on the path, so
its tau is tuned on 100k separate Monte Carlo null paths per base rate.

Operating characteristics. Monte Carlo, `--reps` replications per cell
(default 100,000), seeds fixed per cell, horizon 40 applicable sessions
(20-session figures read off the same paths). For every rule but SEH the
exact DP value is also computed and the largest MC-DP gap is reported.

Replay. For each gated-arm trial in runs 6, 7 and 9 the arm of each session
comes from its `trial` event (Kind.TRIAL, logged by Memory._trial_arms) and
the outcome from its `check` events with check = "outcome", its
`user_repeat` events and any `user_undo` / `user_edit` naming it as the
write session, scored exactly as trials.evaluate scores them. Then
each rule is run over the recorded sequence. Records are grouped into
clusters: two records are linked when they share an `about` anchor or were
proposed in the same session, across kinds, and clusters are the connected
components.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
import zlib
from bisect import bisect_left
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from rka_memory.kernel.trials import fisher as repo_fisher  # noqa: E402

SEED = 20261117
H = 40
SHORT = 20
DRIFT = 100
BOUND = 0.05
BASES = (0.1, 0.5, 0.9)
GRID = tuple(round(0.05 * k, 2) for k in range(1, 20))  # base rates for the worst-case null
ALTS = ((0.9, 0.1), (0.8, 0.2), (0.9, 0.5))
HARM = (0.2, 0.8)
U = tuple(k / 20 for k in range(1, 20))
H_HOLD, Q_HOLD, NMIN = 0.1, 0.1, 2
RUNS = {"run 6": "20261116T062700Z", "run 7": "20261116T144401Z", "run 9": "20261116T154835Z"}

# A state (tn, tk, wn, wk) packed in one int, 8 bits a field.
T1, K1, W1, G1 = 1 << 24, 1 << 16, 1 << 8, 1


def unpack(key: int) -> tuple[int, int, int, int]:
    return key >> 24, (key >> 16) & 255, (key >> 8) & 255, key & 255


# ---------------------------------------------------------------- statistics

_FISHER: dict[int, float] = {}
_POST: dict[int, float] = {}
_EMIX: dict[tuple[int, int], float] = {}
_EHOLD: dict[tuple[int, int, int, int, float], float] = {}


def _logbeta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def fisher_p(key: int) -> float:
    """One-sided Fisher p, the repository's exact function up to 40
    sessions, a log-space hypergeometric tail beyond."""
    v = _FISHER.get(key)
    if v is None:
        tn, tk, wn, wk = unpack(key)
        v = repo_fisher((tn, tk), (wn, wk)) if tn + wn <= 40 else _fisher_float(tn, tk, wn, wk)
        _FISHER[key] = v
    return v


def _fisher_float(tn: int, tk: int, wn: int, wk: int) -> float:
    n, good = tn + wn, tk + wk
    lc = math.lgamma(n + 1) - math.lgamma(tn + 1) - math.lgamma(wn + 1)
    v = 0.0
    for x in range(tk, min(good, tn) + 1):
        v += math.exp(
            math.lgamma(good + 1) - math.lgamma(x + 1) - math.lgamma(good - x + 1)
            + math.lgamma(n - good + 1) - math.lgamma(tn - x + 1) - math.lgamma(n - good - tn + x + 1)
            - lc
        )
    return min(v, 1.0)


def posterior(key: int) -> float:
    """P(pT > pW) under independent Beta(1, 1) priors, exact sum for an
    integer first parameter (E. Miller's closed form)."""
    v = _POST.get(key)
    if v is None:
        tn, tk, wn, wk = unpack(key)
        ab, bb = 1 + tk, 1 + tn - tk  # treated
        aa, ba = 1 + wk, 1 + wn - wk  # withheld
        base = _logbeta(aa, ba)
        v = 0.0
        for i in range(ab):
            v += math.exp(_logbeta(aa + i, ba + bb) - math.log(bb + i) - _logbeta(1 + i, bb) - base)
        v = min(max(v, 0.0), 1.0)
        _POST[key] = v
    return v


def emix(n: int, s: int) -> float:
    """SE wealth at q = 0.5 after n sessions, s of them concordant."""
    v = _EMIX.get((n, s))
    if v is None:
        v = sum(math.exp(s * math.log1p(u) + (n - s) * math.log1p(-u)) for u in U) / len(U)
        _EMIX[(n, s)] = v
    return v


def ehold(p1: int, m1: int, p9: int, m9: int, r: float) -> float:
    """SEH wealth. With q_i <= 1/2 a treated session multiplies by (1 +- u)
    and a withheld one by (1 +- u q_i / (1 - q_i)); p1/m1 count concordant /
    discordant factors of ratio 1 (treated, or withheld at q = 0.5), p9/m9
    those of ratio r = q_hold / (1 - q_hold) (withheld while on hold)."""
    k = (p1, m1, p9, m9, r)
    v = _EHOLD.get(k)
    if v is None:
        v = sum(
            math.exp(p1 * math.log1p(u) + m1 * math.log1p(-u) + p9 * math.log1p(u * r) + m9 * math.log1p(-u * r))
            for u in U
        ) / len(U)
        _EHOLD[k] = v
    return v


def concordant(key: int) -> tuple[int, int]:
    tn, tk, wn, wk = unpack(key)
    return tn + wn, tk + (wn - wk)


# --------------------------------------------------------------------- rules

class Rule:
    """A rule whose coin chance and decision depend only on (tn, tk, wn, wk).
    decide(key) is 0 to continue, 1 to promote, 2 to close for good."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.kind = spec["kind"]
        self._d: dict[int, int] = {}
        self._q: dict[int, float] = {}
        self.fixed_q = 0.5 if self.kind not in ("sbh",) else None

    def q(self, key: int) -> float:
        v = self._q.get(key)
        if v is None:
            v = self.spec["q_hold"] if posterior(key) <= self.spec["h"] else 0.5
            self._q[key] = v
        return v

    def stat(self, key: int) -> float:
        """The statistic a sequential rule promotes on (larger is stronger)."""
        if self.kind == "sf":
            return -fisher_p(key)
        if self.kind in ("sb", "sbh"):
            return posterior(key)
        if self.kind == "se":
            return emix(*concordant(key))
        raise ValueError(self.kind)

    def decide(self, key: int) -> int:
        d = self._d.get(key)
        if d is None:
            tn, tk, wn, wk = unpack(key)
            s = self.spec
            if self.kind == "once":
                d = 0
                if tn >= s["m"] and wn >= s["m"]:
                    d = 1 if fisher_p(key) <= s["alpha"] + 1e-12 else 2
            else:
                d = 1 if tn >= NMIN and wn >= NMIN and self.stat(key) >= s["thr"] - 1e-12 else 0
            self._d[key] = d
        return d


# ------------------------------------------------------------------ exact DP

def dp(spec: dict, pt: float, pw: float, horizon: int = H) -> dict:
    """Exact promotion and close probabilities by session, and expected
    deliveries over the horizon, for a count-state rule."""
    rule = Rule(spec)
    alive = {0: 1.0}
    promo = [0.0] * (horizon + 1)
    close = [0.0] * (horizon + 1)
    promoted = deliv = 0.0
    for n in range(1, horizon + 1):
        deliv += promoted
        nxt: dict[int, float] = {}
        for key, pr in alive.items():
            q = rule.fixed_q if rule.fixed_q is not None else rule.q(key)
            deliv += pr * q
            for k2, w in ((key + T1 + K1, q * pt), (key + T1, q * (1 - pt)), (key + W1 + G1, (1 - q) * pw), (key + W1, (1 - q) * (1 - pw))):
                if w <= 0.0:
                    continue
                d = rule.decide(k2)
                if d == 1:
                    promo[n] += pr * w
                elif d == 2:
                    close[n] += pr * w
                else:
                    nxt[k2] = nxt.get(k2, 0.0) + pr * w
        promoted += promo[n]
        alive = nxt
    return summarize(promo, close, deliv, horizon)


def summarize(promo, close, deliv, horizon) -> dict:
    total = sum(promo)
    decided = [promo[n] + close[n] for n in range(horizon + 1)]
    return {
        "p20": sum(promo[: SHORT + 1]),
        "p40": total,
        "mean_promo": sum(n * promo[n] for n in range(horizon + 1)) / total if total > 0 else None,
        "mean_decision": sum(n * decided[n] for n in range(horizon + 1)) / sum(decided) if sum(decided) > 0 else None,
        "closed": sum(close),
        "deliv": deliv,
    }


def null_dp(spec: dict, bases=BASES) -> float:
    worst = 0.0
    for b in sorted(bases, key=lambda b: abs(b - 0.5)):
        worst = max(worst, dp(spec, b, b)["p40"])
        if worst > BOUND:
            break
    return worst


def candidates(kind: str) -> list[float]:
    """Every value the statistic takes on a state with min arms 2 and at
    most H sessions, ascending."""
    rule = Rule({"kind": kind, "thr": 0, "h": H_HOLD, "q_hold": Q_HOLD})
    vals = set()
    for tn in range(NMIN, H - NMIN + 1):
        for wn in range(NMIN, H - tn + 1):
            for tk in range(tn + 1):
                for wk in range(wn + 1):
                    vals.add(round(rule.stat((tn << 24) | (tk << 16) | (wn << 8) | wk), 12))
    return sorted(vals)


def tune(base_spec: dict) -> dict:
    """The smallest threshold in the statistic's range whose exact null
    promotion within H is at most BOUND at every base rate in BASES."""
    cand = candidates(base_spec["kind"])
    lo, hi = 0, len(cand) - 1
    assert null_dp({**base_spec, "thr": cand[hi]}) <= BOUND
    while lo < hi:
        mid = (lo + hi) // 2
        if null_dp({**base_spec, "thr": cand[mid]}) <= BOUND:
            hi = mid
        else:
            lo = mid + 1
    return {**base_spec, "thr": cand[lo]}


# ----------------------------------------------------------------- Monte Carlo

def mc(task: tuple) -> dict:
    spec, pt, pw, horizon, reps, seed, mode = task
    rng = random.Random(seed)
    rand = rng.random
    if spec["kind"] == "seh":
        return mc_seh(spec, pt, pw, horizon, reps, rand, mode)
    rule = Rule(spec)
    decide, qf, fixed = rule.decide, rule.q, rule.fixed_q
    hist = [0] * (horizon + 1)
    closed = deliv = 0
    for _ in range(reps):
        key = 0
        for n in range(1, horizon + 1):
            q = fixed if fixed is not None else qf(key)
            if rand() < q:
                deliv += 1
                key += T1 + K1 if rand() < pt else T1
            else:
                key += W1 + G1 if rand() < pw else W1
            d = decide(key)
            if d:
                if d == 1:
                    hist[n] += 1
                    deliv += horizon - n
                else:
                    closed += 1
                break
    return {"hist": hist, "closed": closed, "deliv": deliv, "reps": reps}


def mc_seh(spec, pt, pw, horizon, reps, rand, mode) -> dict:
    h, qh, thr = spec["h"], spec["q_hold"], spec.get("thr", math.inf)
    r = qh / (1 - qh)
    hist = [0] * (horizon + 1)
    deliv = 0
    maxima: Counter = Counter()
    for _ in range(reps):
        key = p1 = m1 = p9 = m9 = 0
        best = 0.0
        for n in range(1, horizon + 1):
            held = posterior(key) <= h
            if rand() < (qh if held else 0.5):
                deliv += 1
                if rand() < pt:
                    key += T1 + K1
                    p1 += 1
                else:
                    key += T1
                    m1 += 1
            else:
                good = rand() < pw
                key += W1 + G1 if good else W1
                if held:
                    if good:
                        m9 += 1
                    else:
                        p9 += 1
                elif good:
                    m1 += 1
                else:
                    p1 += 1
            if key >> 24 >= NMIN and (key >> 8) & 255 >= NMIN:
                e = ehold(p1, m1, p9, m9, r)
                if mode == "max":
                    best = max(best, e)
                elif e >= thr - 1e-12:
                    hist[n] += 1
                    deliv += horizon - n
                    break
        if mode == "max":
            maxima[round(best, 12)] += 1
    out = {"hist": hist, "closed": 0, "deliv": deliv, "reps": reps}
    if mode == "max":
        out["maxima"] = dict(maxima)
    return out


def mc_summary(res: dict, horizon: int = H) -> dict:
    hist, reps = res["hist"], res["reps"]
    promo = [c / reps for c in hist]
    close = [0.0] * (horizon + 1)
    out = summarize(promo, close, res["deliv"] / reps, horizon)
    out["closed"] = res["closed"] / reps
    return out


def seed_for(*parts) -> int:
    return SEED + zlib.crc32(repr(parts).encode()) % 1_000_000


def tune_seh(pool, reps: int) -> dict:
    spec = {"kind": "seh", "h": H_HOLD, "q_hold": Q_HOLD}
    tasks = [(spec, b, b, H, reps, seed_for("tune-seh", b), "max") for b in BASES]
    maxima = [r["maxima"] for r in pool.map(mc, tasks)]
    values = sorted({v for m in maxima for v in m})
    surv = []
    for m in maxima:
        ordered = sorted(m)
        counts = [m[v] for v in ordered]
        tail = [0] * (len(ordered) + 1)
        for i in range(len(ordered) - 1, -1, -1):
            tail[i] = tail[i + 1] + counts[i]
        surv.append((ordered, tail))
    for v in values:
        if v <= 1.0:
            continue
        worst = max(tail[bisect_left(ordered, v)] / reps for ordered, tail in surv)
        if worst <= BOUND:
            return {**spec, "thr": v, "tuning_null": worst}
    raise RuntimeError("no SEH threshold meets the bound")


# --------------------------------------------------------------------- replay

def replay() -> dict:
    from unseen_requirement_gate import about_operations
    from v2_replay import load

    from rka_memory.domain import parse_ts
    from rka_memory.kernel import trials
    from rka_memory.kernel.evidence import modified_sessions
    from rka_memory.kernel.influence import Influence
    from rka_memory.kernel.labels import label
    from rka_memory.world import project

    out = {}
    for name, run in RUNS.items():
        d = ROOT / "workload-runs" / run / "unseen-requirement-gated"
        events, cfg, _ = load(d / "ledger" / "ledger.jsonl")
        world = project(events)
        labels = label(world, cfg, max(parse_ts(e.ts) for e in events))
        starts = sorted((world.position[s.started.id], sid) for sid, s in world.sessions.items() if s.started is not None)
        index = {sid: i for i, (_, sid) in enumerate(starts, 1)}
        influence = Influence(world)
        suspect = modified_sessions(world, cfg) | {
            sid for sid in {e.session_id for e in world.trials} if influence.session(sid) & labels.withdrawn
        }
        delivered = {}
        for dl in world.deliveries:
            delivered.setdefault(dl.event.session_id, set()).update(dl.record_ids)

        trial_rows = []
        for rid in dict.fromkeys(e.payload.get("record_id") for e in world.trials):
            rec = world.records[rid]
            arms: dict[str, str] = {}
            for ev in world.trials:
                if ev.payload.get("record_id") == rid and ev.session_id not in arms:
                    arms[ev.session_id] = str(ev.payload.get("arm"))
            ended = sorted(
                (world.position[s.ended.id], s) for sid in arms if (s := world.sessions.get(sid)) is not None and s.ended is not None
            )
            seq = []
            for _, sess in ended:
                against = sess.id in suspect or trials._outcome_check(sess) == "due"
                good = (arms[sess.id] == "withheld") if against else trials.outcome(world, sess)
                seq.append({"s": index[sess.id], "arm": arms[sess.id], "good": bool(good), "suspect": against})
            tr = trials.evaluate(rec, world, cfg, frozenset(suspect))
            trial_rows.append({
                "id": rid, "kind": rec.spec.kind.value, "content": rec.spec.content, "seq": seq,
                "evaluate": {"status": tr.status, "treated": tr.treated, "withheld": tr.withheld, "p": tr.p,
                             "completed_s": next((index[s.id] for _, s in ended if world.position[s.ended.id] == tr.completed_at), None)},
                "rung": labels.by_id[rid].rung.value,
            })

        recs = sorted(world.records.values(), key=lambda r: world.position[r.proposed.id])
        rows = []
        for r in recs:
            lab = labels.by_id[r.id]
            rows.append({
                "id": r.id, "kind": r.spec.kind.value, "rung": lab.rung.value, "affirmed": lab.affirmed,
                "s": index.get(r.proposed.session_id), "about": list(r.spec.about),
                "operations": about_operations(r.spec.content), "content": r.spec.content,
                "delivered_in": sorted(index[s] for s, ids in delivered.items() if r.id in ids and s in index),
            })
        # Sessions where the code at HEAD, replayed on the ledger up to each
        # gated packet, would have logged a coin that the recorded run did not.
        from rka_memory.domain import Kind
        from rka_memory.kernel.evidence import applicable

        logged = {(e.session_id, e.payload.get("record_id")) for e in world.trials}
        missing = set()
        for i, ev in enumerate(events):
            if ev.kind is not Kind.PACKET_DELIVERED or ev.payload.get("arm") != "gated":
                continue
            w = project(events[:i])
            labs = label(w, cfg, parse_ts(ev.ts))
            for rid in trials.candidates(w, labs, cfg):
                if (ev.session_id, rid) not in logged and applicable(w.records[rid], w.session(ev.session_id).state, w) is True:
                    missing.add((rid, index[ev.session_id]))

        sessions = []
        for i, (_, sid) in enumerate(starts, 1):
            sess = world.sessions[sid]
            sessions.append({"s": i, "check": trials._outcome_check(sess), "good": trials.outcome(world, sess)})
        out[name] = {"run": run, "trials": trial_rows, "records": rows, "sessions": sessions, "missing_coins": sorted(missing),
                     "clusters": clusters(rows, by_session=True), "clusters_anchor_only": clusters(rows, by_session=False)}
    return out


def clusters(rows: list[dict], by_session: bool) -> list[list[str]]:
    parent = {r["id"]: r["id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if set(a["about"]) & set(b["about"]) or (by_session and a["s"] == b["s"]):
                parent[find(a["id"])] = find(b["id"])
    groups: dict[str, list[str]] = {}
    for r in rows:
        groups.setdefault(find(r["id"]), []).append(r["id"])
    return sorted(groups.values(), key=len, reverse=True)


def run_rules_on(seq: list[dict], specs: dict[str, dict]) -> dict[str, dict]:
    """Each rule over a recorded sequence: the session it would have promoted
    or closed in, and for hold rules the sessions it would have spent on hold."""
    out = {}
    for name, spec in specs.items():
        key = p1 = m1 = 0
        held, verdict = [], None
        rule = Rule(spec) if spec["kind"] != "seh" else None
        for step in seq:
            if spec["kind"] in ("sbh", "seh", "sb") and posterior(key) <= H_HOLD:
                held.append(step["s"])
            if step["arm"] == "treated":
                key += T1 + K1 if step["good"] else T1
            else:
                key += W1 + G1 if step["good"] else W1
            concord = step["good"] == (step["arm"] == "treated")
            p1, m1 = p1 + concord, m1 + (not concord)
            if spec["kind"] == "seh":
                tn, _, wn, _ = unpack(key)
                d = 1 if tn >= NMIN and wn >= NMIN and ehold(p1, m1, 0, 0, 1.0) >= spec["thr"] - 1e-12 else 0
            else:
                d = rule.decide(key)
            if d:
                verdict = ("promoted" if d == 1 else "closed", step["s"])
                break
        tn, tk, wn, wk = unpack(key)
        out[name] = {
            "verdict": verdict, "held": held,
            "final": {"treated": f"{tk}/{tn}", "withheld": f"{wk}/{wn}", "fisher_p": fisher_p(key),
                      "posterior": posterior(key), "E": emix(tn + wn, tk + wn - wk)},
        }
    return out


# -------------------------------------------------------------------- report

def pct(x: float | None, digits: int = 1) -> str:
    return "-" if x is None else f"{100 * x:.{digits}f}"


def num(x: float | None, digits: int = 1) -> str:
    return "-" if x is None else f"{x:.{digits}f}"


def describe(name: str, spec: dict) -> str:
    k = spec["kind"]
    if k == "once":
        return f"Fisher once at m = {spec['m']}, alpha = {spec['alpha']}, then close for good"
    if k == "sf":
        return f"Fisher after every session, alpha* = {-spec['thr']:.4f}, never closes"
    if k == "sb":
        return f"promote at P(pT > pW) >= {spec['thr']:.4f}, q = 0.5 always"
    if k == "sbh":
        return f"promote at P(pT > pW) >= {spec['thr']:.4f}; hold (q = {spec['q_hold']}) while P <= {spec['h']}"
    if k == "se":
        return f"promote at E >= {spec['thr']:.2f}" + (" (= 1/0.05, anytime-valid)" if name == "SEv" else "")
    if k == "seh":
        return f"promote at E >= {spec['thr']:.2f}; q = {spec['q_hold']} while P(pT > pW) <= {spec['h']}"
    return k


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=100_000)
    ap.add_argument("--jobs", type=int, default=None)
    ap.add_argument("--out", default=str(ROOT / "workload-runs" / "e0d"))
    args = ap.parse_args(argv)
    started = time.time()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    # Self-checks: the closed forms against the repository and a quadrature.
    assert abs(posterior(0) - 0.5) < 1e-12
    for key in ((3 << 24) | (3 << 16) | (3 << 8), (7 << 24) | (6 << 16) | (3 << 8) | 1, (5 << 24) | (2 << 16) | (9 << 8) | 4):
        tn, tk, wn, wk = unpack(key)
        steps = 4000
        grid = [(i + 0.5) / steps for i in range(steps)]
        ft = [x ** tk * (1 - x) ** (tn - tk) for x in grid]
        fw = [x ** wk * (1 - x) ** (wn - wk) for x in grid]
        cum, acc = [], 0.0
        for v in fw:
            acc += v
            cum.append(acc)
        quad = sum(a * c for a, c in zip(ft, cum)) / (sum(ft) * acc)
        assert abs(quad - posterior(key)) < 2e-3, (key, quad, posterior(key))
    for tn, tk, wn, wk in ((20, 15, 21, 9), (30, 12, 25, 14), (7, 6, 3, 1), (3, 3, 5, 0), (19, 19, 21, 0)):
        assert abs(repo_fisher((tn, tk), (wn, wk)) - _fisher_float(tn, tk, wn, wk)) < 1e-9

    with Pool(args.jobs) as pool:
        # Tune. SF, SB, SBH, SE exactly; SEH by Monte Carlo on its own seeds.
        bases = [
            {"kind": "sf"},
            {"kind": "sb", "h": H_HOLD, "q_hold": Q_HOLD},
            {"kind": "sbh", "h": H_HOLD, "q_hold": Q_HOLD},
            {"kind": "se"},
        ]
        sens_bases = [{"kind": "sbh", "h": h, "q_hold": qh} for h in (0.05, 0.1, 0.2) for qh in (0.1, 0.25) if (h, qh) != (H_HOLD, Q_HOLD)]
        tuned = pool.map(tune, bases + sens_bases)
        seh = tune_seh(pool, args.reps)
        specs = {
            "A3": {"kind": "once", "m": 3, "alpha": 0.05},
            "A8": {"kind": "once", "m": 8, "alpha": 0.05},
            "SF": tuned[0],
            "SB": tuned[1],
            "SBH": tuned[2],
            "SE": tuned[3],
            "SEv": {"kind": "se", "thr": 20.0},
            "SEH": seh,
        }
        sens = tuned[4:]
        print(f"tuned in {time.time() - started:.0f}s:", {k: v.get("thr") for k, v in specs.items()}, flush=True)

        # Monte Carlo cells.
        scen = [("null", b, b) for b in BASES] + [("alt", pt, pw) for pt, pw in ALTS] + [("harm", *HARM)]
        tasks, keys = [], []
        for name, spec in specs.items():
            for tag, pt, pw in scen:
                tasks.append((spec, pt, pw, H, args.reps, seed_for(name, pt, pw, H), "run"))
                keys.append((name, pt, pw, H))
            if spec["kind"] != "once":
                tasks.append((spec, 0.5, 0.5, DRIFT, args.reps, seed_for(name, 0.5, 0.5, DRIFT), "run"))
                keys.append((name, 0.5, 0.5, DRIFT))
        # Longest cells first so the pool stays busy.
        order = sorted(range(len(tasks)), key=lambda i: -tasks[i][3])
        results = dict(zip([keys[i] for i in order], pool.map(mc, [tasks[i] for i in order], chunksize=1)))
        mcs = {k: mc_summary(v, k[3]) for k, v in results.items()}
        print(f"Monte Carlo done in {time.time() - started:.0f}s", flush=True)

        # Exact values, worst-case null over a grid of base rates, sensitivity.
        exact = {}
        worst_grid = {}
        for name, spec in specs.items():
            if spec["kind"] == "seh":
                continue
            for tag, pt, pw in scen:
                exact[(name, pt, pw)] = dp(spec, pt, pw)
            worst_grid[name] = max((dp(spec, b, b)["p40"], b) for b in GRID)
        sens_rows = []
        for spec in [specs["SBH"]] + sens:
            alt, harm = dp(spec, 0.8, 0.2), dp(spec, *HARM)
            sens_rows.append({"h": spec["h"], "q_hold": spec["q_hold"], "c": spec["thr"], "null40": null_dp(spec),
                              "p20": alt["p20"], "p40": alt["p40"], "harm_deliv": harm["deliv"]})

    rep = replay()
    replays = {name: [dict(t, rules=run_rules_on(t["seq"], specs)) for t in r["trials"]] for name, r in rep.items()}
    # The repository's own trial verdict must come out of A3 on the extracted sequence.
    for name, ts in replays.items():
        for t in ts:
            ev, a3 = t["evaluate"], t["rules"]["A3"]["verdict"]
            want = None if ev["status"] == "open" else ("promoted" if ev["status"] == "positive" else "closed", ev["completed_s"])
            assert a3 == want, (name, t["id"], a3, want)

    elapsed = time.time() - started
    write_report(outdir, args, specs, mcs, exact, worst_grid, sens_rows, rep, replays, elapsed)
    print(f"wrote {outdir / 'results.md'} in {elapsed:.0f}s")
    return 0


def _group(pairs):
    out: dict[str, list] = {}
    for rid, sess in pairs:
        out.setdefault(rid, []).append((rid, sess))
    return sorted(out.items(), key=lambda kv: kv[1][0][1])


def write_report(outdir, args, specs, mcs, exact, worst_grid, sens_rows, rep, replays, elapsed):
    L = []
    w = L.append
    w("# E0d: a sequential trial rule\n")
    w(f"Generated by `.venv/bin/python scripts/e0d_sequential_rule.py --reps {args.reps}` in {elapsed:.0f} s. "
      f"Monte Carlo: {args.reps:,} replications per cell, seeds fixed per cell (base {SEED}). "
      "Exact: dynamic programming over (tn, tk, wn, wk). Rule definitions are in the script's docstring. "
      "Horizon: applicable gated sessions of one record. Min arms 2 for every sequential rule.\n")

    w("## Rules and tuned parameters\n")
    w("| Rule | Definition | Null promotion within 40, worst of 0.1/0.5/0.9 (exact) | Worst over base rates 0.05..0.95 (exact) |")
    w("|---|---|---|---|")
    for name, spec in specs.items():
        if spec["kind"] == "seh":
            tuned = f"{pct(spec['tuning_null'], 2)} (MC, tuning seeds)"
            grid = "-"
        else:
            tuned = pct(max(exact[(name, b, b)]["p40"] for b in BASES), 2)
            g, b = worst_grid[name]
            grid = f"{pct(g, 2)} at {b}"
        w(f"| {name} | {describe(name, spec)} | {tuned} | {grid} |")
    w("")

    w("## Null promotion (%), Monte Carlo\n")
    w(f"| Rule | 0.1, 20 | 0.1, 40 | 0.5, 20 | 0.5, 40 | 0.9, 20 | 0.9, 40 | 0.5, {DRIFT} |")
    w("|---|---|---|---|---|---|---|---|")
    for name in specs:
        cells = []
        for b in BASES:
            m = mcs[(name, b, b, H)]
            cells += [pct(m["p20"], 2), pct(m["p40"], 2)]
        drift = mcs.get((name, 0.5, 0.5, DRIFT))
        cells.append(pct(drift["p40"], 2) if drift else "= 40")
        w(f"| {name} | " + " | ".join(cells) + " |")
    w("")

    w("## Power (%) and sessions to promotion, Monte Carlo\n")
    w("Mean sessions to promotion is among records promoted within 40 sessions.\n")
    w("| Rule | 0.9 v 0.1, 20 | 0.9 v 0.1, 40 | 0.8 v 0.2, 20 | 0.8 v 0.2, 40 | 0.9 v 0.5, 20 | 0.9 v 0.5, 40 | mean sessions (0.9v0.1 / 0.8v0.2 / 0.9v0.5) |")
    w("|---|---|---|---|---|---|---|---|")
    for name in specs:
        cells, means = [], []
        for pt, pw in ALTS:
            m = mcs[(name, pt, pw, H)]
            cells += [pct(m["p20"]), pct(m["p40"])]
            means.append(num(m["mean_promo"]))
        w(f"| {name} | " + " | ".join(cells) + " | " + " / ".join(means) + " |")
    w("")

    w(f"## A harmful record (treated {HARM[0]} v withheld {HARM[1]}), Monte Carlo\n")
    w("| Rule | expected sessions delivered, of 40 | promoted within 40 (%) | closed within 40 (%) |")
    w("|---|---|---|---|")
    for name in specs:
        m = mcs[(name, *HARM, H)]
        w(f"| {name} | {num(m['deliv'], 2)} | {pct(m['p40'], 3)} | {pct(m['closed'])} |")
    w("")

    # Agreement of MC with exact.
    gaps = {}
    for (name, pt, pw), ex in exact.items():
        m = mcs[(name, pt, pw, H)]
        gaps[name] = max(gaps.get(name, 0.0), abs(m["p20"] - ex["p20"]), abs(m["p40"] - ex["p40"]))
    a3 = {(pt, pw): exact[("A3", pt, pw)] for pt, pw in ((0.5, 0.5),) + ALTS}
    w("## Checks\n")
    w("- Largest |MC - exact| over every promotion figure above, by rule: "
      + ", ".join(f"{k} {100 * v:.2f} pts" for k, v in gaps.items()) + ". "
      f"The binomial standard error at 50% and {args.reps:,} replications is {100 * math.sqrt(0.25 / args.reps):.2f} pts.")
    w(f"- A3 reproduces the verified figures (exact, within 40 sessions): null {pct(a3[(0.5, 0.5)]['p40'], 2)}% at base 0.5; "
      f"power {pct(a3[(0.9, 0.1)]['p40'])}%, {pct(a3[(0.8, 0.2)]['p40'])}%, {pct(a3[(0.9, 0.5)]['p40'])}%; "
      f"{num(a3[(0.5, 0.5)]['mean_decision'], 2)} sessions to a decision. A8: {num(exact[('A8', 0.5, 0.5)]['mean_decision'], 1)} sessions to a decision.")
    w("- The replay of A3 on each extracted trial sequence returns the verdict and session of `trials.evaluate` on the same ledger.\n")

    w("## Sensitivity of SBH to its hold parameters (exact)\n")
    w("| h | q_hold | tuned c | null within 40 (%) | power 0.8 v 0.2 within 20 (%) | within 40 (%) | harmful record: sessions delivered of 40 |")
    w("|---|---|---|---|---|---|---|")
    for r in sens_rows:
        w(f"| {r['h']} | {r['q_hold']} | {r['c']:.4f} | {pct(r['null40'], 3)} | {pct(r['p20'], 3)} | {pct(r['p40'], 2)} | {num(r['harm_deliv'], 2)} |")
    w("")

    # Pre-registered selection.
    def null40(n, b):
        return exact[(n, b, b)]["p40"] if (n, b, b) in exact else mcs[(n, b, b, H)]["p40"]

    def power(n):
        return exact[(n, 0.8, 0.2)]["p20"] if (n, 0.8, 0.2) in exact else mcs[(n, 0.8, 0.2, H)]["p20"]

    eligible = [n for n in specs if all(null40(n, b) <= BOUND + 1e-12 for b in BASES)]
    ranked = sorted(eligible, key=lambda n: (-round(power(n), 6), mcs[(n, *HARM, H)]["deliv"]))
    w("## Pre-registered selection\n")
    w("Among rules whose null promotion within 40 is at most 5% at 0.1, 0.5 and 0.9 (exact where the DP applies, Monte Carlo for SEH), "
      "ranked by power at 0.8 v 0.2 within 20 (exact where the DP applies; Monte Carlo for SEH, whose standard error is about 0.14 pts); "
      "ties broken by fewer deliveries of a harmful record:\n")
    for n in ranked:
        ex = exact.get((n, 0.8, 0.2))
        w(f"- {n}: {pct(ex['p20'], 3) if ex else '-'}% exact, {pct(mcs[(n, 0.8, 0.2, H)]['p20'], 2)}% Monte Carlo; "
          f"harmful record delivered in {num(mcs[(n, *HARM, H)]['deliv'], 2)} of 40 sessions")
    excluded = [n for n in specs if n not in eligible]
    if excluded:
        w("\nExcluded by the null bound: " + ", ".join(
            f"{n} ({', '.join(pct(null40(n, b), 2) for b in BASES)}%)" for n in excluded) + ".")
    w("")

    w("## Replay on recorded outcomes\n")
    w("Sequence: session, arm (T treated, W withheld), outcome (+ good, - bad), as `trials.evaluate` scores it. "
      "Each rule is run on the recorded arms; rules that lower q would have drawn different later coins, so their hold sessions are where q would have dropped.\n")
    for name, ts in replays.items():
        w(f"### {name} ({RUNS[name]})\n")
        for t in ts:
            seq = " ".join(f"s{x['s']}{'T' if x['arm'] == 'treated' else 'W'}{'+' if x['good'] else '-'}" for x in t["seq"])
            ev = t["evaluate"]
            w(f"**{t['id']}** ({t['kind']}, now {t['rung']}): {t['content'][:140]}\n")
            w(f"- Sequence: {seq}")
            w(f"- trials.evaluate: {ev['status']}, treated {ev['treated'][1]}/{ev['treated'][0]}, withheld {ev['withheld'][1]}/{ev['withheld'][0]}"
              + (f", p = {ev['p']:.3f} at s{ev['completed_s']}" if ev["p"] is not None else ""))
            w("")
            w("| Rule | verdict | on hold in | final treated / withheld | Fisher p | P(pT > pW) | E (q = 0.5) |")
            w("|---|---|---|---|---|---|---|")
            for rn, r in t["rules"].items():
                v = r["verdict"]
                verdict = f"{v[0]} at s{v[1]}" if v else f"open after s{t['seq'][-1]['s']}"
                held = ", ".join(f"s{s}" for s in r["held"])
                if specs[rn]["kind"] == "sb" and held:
                    held = "label only: " + held
                elif specs[rn]["kind"] not in ("sb", "sbh", "seh"):
                    held = "n/a"
                f = r["final"]
                w(f"| {rn} | {verdict} | {held or '-'} | {f['treated']} / {f['withheld']} | {f['fisher_p']:.3f} | {f['posterior']:.3f} | {f['E']:.2f} |")
            w("")

    w("### Sessions where the code at HEAD would have flipped a coin the recorded run did not\n")
    for name, r in rep.items():
        miss = r["missing_coins"]
        w(f"- {name}: " + ("none" if not miss else "; ".join(
            f"{rid} in " + ", ".join(f"s{s}" for _, s in grp)
            for rid, grp in _group(miss))))
    w("")

    w("### Run 6: what carried the requirement into the withheld sessions that passed\n")
    r6 = rep["run 6"]
    by_id = {x["id"]: x for x in r6["records"]}
    home = {rid: i for i, cl in enumerate(r6["clusters"], 1) for rid in cl}
    for t in replays["run 6"]:
        passed = [x for x in t["seq"] if x["arm"] == "withheld" and x["good"]]
        for x in passed:
            got = [y for y in r6["records"] if x["s"] in y["delivered_in"]]
            w(f"- {t['id']}, withheld s{x['s']} passed. Delivered there: " + "; ".join(
                f"{y['id']} ({y['kind']}, {y['rung']}{', names OPERATIONS' if y['operations'] else ''}, "
                f"{'same cluster' if home[y['id']] == home[t['id']] else 'cluster ' + str(home[y['id']])})" for y in got) + ".")
            alt = [dict(z, good=False) if z["s"] == x["s"] else z for z in t["seq"]]
            res = run_rules_on(alt, specs)
            w(f"  - Had s{x['s']} failed: Fisher p = {res['A3']['final']['fisher_p']:.3f}; "
              + ", ".join(f"{k} {v['verdict'][0] + ' at s' + str(v['verdict'][1]) if v['verdict'] else 'open'}" for k, v in res.items()) + ".")
    w("")

    w("## Clusters\n")
    w("A cluster is a connected component of records linked by a shared `about` anchor or the same proposing session, across kinds. "
      "OPS marks records whose text names OPERATIONS.\n")
    for name, r in rep.items():
        by_id = {x["id"]: x for x in r["records"]}
        w(f"### {name}\n")
        w("| Cluster | Record | kind | rung | proposed | about | OPS | delivered in |")
        w("|---|---|---|---|---|---|---|---|")
        for i, cl in enumerate(r["clusters"], 1):
            for rid in cl:
                x = by_id[rid]
                w(f"| {i} | {rid} | {x['kind']} | {x['rung']}{' (affirmed)' if x['affirmed'] else ''} | s{x['s']} | {', '.join(x['about']) or '-'} | {'yes' if x['operations'] else ''} | {', '.join(f's{s}' for s in x['delivered_in']) or '-'} |")
        ops = [x["id"] for x in r["records"] if x["operations"]]
        home = {rid: i for i, cl in enumerate(r["clusters"]) for rid in cl}
        home_a = {rid: i for i, cl in enumerate(r["clusters_anchor_only"]) for rid in cl}
        w("")
        w(f"- OPERATIONS records in one cluster (anchor or session): {'yes' if len({home[x] for x in ops}) == 1 else 'no'}; "
          f"by anchor alone: {'yes' if len({home_a[x] for x in ops}) == 1 else 'no'}. "
          f"Clusters: {len(r['clusters'])} (anchor or session), {len(r['clusters_anchor_only'])} (anchor only), of {len(r['records'])} records.")
        w("")

    (outdir / "results.md").write_text("\n".join(L) + "\n")
    payload = {
        "specs": specs,
        "mc": {f"{k[0]}|{k[1]}|{k[2]}|{k[3]}": v for k, v in mcs.items()},
        "exact": {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in exact.items()},
        "sensitivity": sens_rows,
        "replay": {n: [{k: v for k, v in t.items()} for t in ts] for n, ts in replays.items()},
        "clusters": {n: {"anchor_or_session": r["clusters"], "anchor_only": r["clusters_anchor_only"]} for n, r in rep.items()},
    }
    (outdir / "results.json").write_text(json.dumps(payload, indent=1, default=str))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

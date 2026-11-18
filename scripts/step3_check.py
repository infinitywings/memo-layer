"""DESIGN_V3 step 3 check (RKA jrn_01M7R3MVT74RMM4P8CFH8VR7QQ): the kernel's
own sequential trial rule, by Monte Carlo (P2) and replayed on the recorded
trials of runs 6, 7 and 9 (P3).

The simulation drives `trials.wealth_factors` and `trials.p_better` with the
default config, so it measures the code, not a copy of it: one record on
trial, a coin per applicable session at q = 0.5, or q_hold while the record
is on hold, the outcome drawn at the arm's success rate. The replay labels
each recorded gated ledger under attested validation with its run's own
outcome check and reports every record that had coins.

Usage: .venv/bin/python scripts/step3_check.py [--reps 20000] [--seed 20261118]
"""

from __future__ import annotations

import argparse
import glob
import math
import random
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rka_memory import config as config_mod  # noqa: E402
from rka_memory.kernel import trials  # noqa: E402
from rka_memory.world import project  # noqa: E402
from v2_replay import load  # noqa: E402

CFG = config_mod.load(path=config_mod.DEFAULT_PATH)
RUNS = {"run 6": "20261116T062700Z", "run 7": "20261116T144401Z", "run 9": "20261116T154835Z"}


def simulate(pt: float, pw: float, horizon: int, reps: int, rng: random.Random) -> dict:
    t = CFG.trials
    logf = {
        (arm, q, good): [math.log(f) for f in trials.wealth_factors(arm, q, good)]
        for arm in ("treated", "withheld") for q in (t.q, t.q_hold) for good in (True, False)
    }
    post: dict = {}
    promoted_at, delivered = [], []
    for _ in range(reps):
        logs = [0.0] * len(trials.U_GRID)
        counts = {"treated": [0, 0], "withheld": [0, 0]}
        given, at = 0, None
        for n in range(1, horizon + 1):
            key = (tuple(counts["treated"]), tuple(counts["withheld"]))
            if key not in post:
                post[key] = trials.p_better(*key)
            q = t.q_hold if post[key] <= t.hold_below else t.q
            arm = "treated" if rng.random() < q else "withheld"
            given += arm == "treated"
            good = rng.random() < (pt if arm == "treated" else pw)
            counts[arm][0] += 1
            counts[arm][1] += good
            logs = [a + b for a, b in zip(logs, logf[(arm, q, good)])]
            if (counts["treated"][0] >= t.min_per_arm and counts["withheld"][0] >= t.min_per_arm
                    and sum(math.exp(x) for x in logs) / len(logs) >= t.e_threshold):
                at = n
                given += horizon - n  # a promoted record is delivered in every later session
                break
        promoted_at.append(at)
        delivered.append(given)
    return {
        "within": lambda h: sum(a is not None and a <= h for a in promoted_at) / reps,
        "delivered": sum(delivered) / reps,
    }


def replay() -> list[str]:
    out = []
    for name, run in RUNS.items():
        for path in sorted(glob.glob(f"workload-runs/{run}/*gated*/ledger/ledger.jsonl")):
            events, cfg, _ = load(Path(path))
            cfg = replace(cfg, ledger_dir=Path(path).parent, gate=replace(cfg.gate, validation="attested"))
            world = project(events)
            order = {s.id: i for i, s in enumerate(sorted(
                (s for s in world.sessions.values() if s.started is not None), key=lambda s: world.position[s.started.id]), 1)}
            ends = {world.position[s.ended.id]: s.id for s in world.sessions.values() if s.ended is not None}
            for rid in sorted({str(e.payload.get("record_id")) for e in world.trials}):
                t = trials.evaluate_sequential(world.records[rid], world, cfg)
                at = f" at s{order[ends[t.completed_at]]}" if t.completed_at is not None else ""
                text = world.records[rid].spec.content[:70]
                out.append(f"{name} {rid[-6:]} {t.status}{at}: {t.reason()} | {text}")
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=20261118)
    args = ap.parse_args(argv)
    t = CFG.trials
    print(f"rule: promote at wealth >= {t.e_threshold:g} with {t.min_per_arm} per arm; q = {t.q:g}, "
          f"{t.q_hold:g} on hold (P(treated better) <= {t.hold_below:g}); {args.reps} paths per cell")
    for base in (0.1, 0.5, 0.9):
        r = simulate(base, base, 100, args.reps, random.Random(f"{args.seed}-null-{base}"))
        print(f"null {base}: promoted within 40 {r['within'](40):.2%}, within 100 {r['within'](100):.2%}")
    for pt, pw, h in ((0.9, 0.1, 40), (0.8, 0.2, 40), (0.9, 0.5, 40)):
        r = simulate(pt, pw, h, args.reps, random.Random(f"{args.seed}-{pt}-{pw}"))
        print(f"power {pt} vs {pw}: within 20 {r['within'](20):.2%}, within 40 {r['within'](40):.2%}")
    r = simulate(0.2, 0.8, 40, args.reps, random.Random(f"{args.seed}-harm"))
    print(f"harmful 0.2 vs 0.8: delivered in {r['delivered']:.1f} of 40 sessions, promoted within 40 {r['within'](40):.2%}")
    print("replay (recorded coins, attested validation):")
    for line in replay():
        print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

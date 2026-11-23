"""E2b: a route for true agent-visible text (docs/E2B.md; design and
predictions in RKA jrn_01M7TCXK0GWJB8BVMQ58YA35YJ).

Reads the E2b runs (default workload-runs/e2b/seed-*) and E2's runs
(default workload-runs/e2/seed-*), which serve as rescored controls: under
the E2b check a session passes when T_X and T_A are met and F_A is not
followed, and an E2 run is scored that way from its own observers. Prints
per run (experiment, fixture, seed, arm), over s4 to s20, pass under the
E2b check, harm, and the T_A and T_X rates; for route and gated runs every
record the miner proposed for T_A or F_A with its first coin, status and
promotion; then the drift rule and Q1 to Q8, with one-sided exact sign
tests in which a tie counts against the prediction.

Usage: .venv/bin/python scripts/e2b_analysis.py [--e2 DIR ...] [--e2b DIR ...] [--promoted]
       On OpenAI runs, compare only concurrent controls:
       .venv/bin/python scripts/e2b_analysis.py --e2b workload-runs/e2b-gpt-6.2-sol-medium/seed-* --e2
"""

from __future__ import annotations

import argparse
import glob
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2_analysis as e2  # noqa: E402

DRIFT_POINTS = 0.25


def e2b_pass(run: e2.Run) -> float | None:
    if run.experiment == "e2b":
        return run.pass_rate
    return run.rate(lambda s: s.observed.get("T_X met") is True and s.observed.get("T_A met") is True and s.observed.get("F_A followed") is not True)


def item_records(run: e2.Run, item: str) -> list[dict]:
    """Records the miner proposed that name the item (an item pattern; read them with --promoted)."""
    pattern = e2.ITEMS[run.fixture][item]
    return [t for t in run.trial_info if t["by"] == "model" and re.search(pattern, t["content"], re.I)
            and not re.search(e2.NEGATED + f"(?:{pattern})", t["content"], re.I)]


def first(values: list[int | None]) -> int | None:
    found = [v for v in values if v is not None]
    return min(found) if found else None


def rates(run: e2.Run) -> dict[str, float | None]:
    return {"pass": e2b_pass(run), "harm": run.follows("F_A"), "T_A": run.follows("T_A")}


def sign(name: str, wins: list[bool], predicted: str) -> str:
    return e2.sign(name, wins, predicted)


def drift(e2b_runs: list[e2.Run], e2_runs: list[e2.Run]) -> tuple[list[str], set[str]]:
    """Concurrent seed-1 controls against the same fixture's rescored E2 seed 1."""
    lines, drifted = [], set()
    old = {(r.fixture, r.arm): r for r in e2_runs if r.seed == 1}
    for r in (r for r in e2b_runs if r.arm in ("gated", "notepad") and r.seed == 1):
        ref = old.get((r.fixture, r.arm))
        if ref is None:
            continue
        now, then = rates(r), rates(ref)
        gaps = {k: abs((now[k] or 0) - (then[k] or 0)) for k in now}
        worst = max(gaps, key=gaps.get)
        flag = gaps[worst] > DRIFT_POINTS
        if flag:
            drifted.add(r.arm)
        lines.append(f"drift {r.fixture} {r.arm}: " + ", ".join(f"{k} {e2.pct(now[k])} vs E2 {e2.pct(then[k])}" for k in now)
                     + (f" -> DRIFT on {worst}" if flag else " -> within 25 points"))
    return lines, drifted


def controls(e2b_runs: list[e2.Run], e2_runs: list[e2.Run], drifted: set[str]) -> dict[tuple[str, int, str], e2.Run]:
    """The control for each (fixture, seed, arm): E2's rescored runs, or,
    for an arm that drifted or with no E2 runs (another model family, as on
    the ChatGPT plan), only the concurrent E2b runs."""
    source = {arm: e2b_runs if arm in drifted or not e2_runs else e2_runs for arm in ("gated", "notepad")}
    return {(r.fixture, r.seed, r.arm): r for arm, runs in source.items() for r in runs if r.arm == arm}


def predictions(e2b_runs: list[e2.Run], e2_runs: list[e2.Run]) -> list[str]:
    drift_lines, drifted = drift(e2b_runs, e2_runs)
    ctl = controls(e2b_runs, e2_runs, drifted)
    route = [r for r in e2b_runs if r.arm == "route"]
    out = list(drift_lines)
    t_a = {id(r): item_records(r, "T_A") for r in route}
    f_a = {id(r): item_records(r, "F_A") for r in route}
    q1a = sum(bool(t_a[id(r)]) for r in route)
    q1f = sum(bool(f_a[id(r)]) for r in route)
    out.append(f"Q1 miner proposed T_A in {q1a} of {len(route)} route runs, F_A in {q1f} (predicted at least 5 of 6 each)")
    q2 = [r for r in route if (first([t["first_coin"] for t in t_a[id(r)]]) or 99) <= 3 and (first([t["first_coin"] for t in f_a[id(r)]]) or 99) <= 3]
    out.append(f"Q2 T_A and F_A both on trial by s3 in {len(q2)} of {len(route)} route runs (predicted all where proposed)")
    in_range = [r for r in route if 0.25 <= (r.follows("T_A") or 0) <= 0.65]
    out.append(f"Q3 route T_A met in 25-65% in {len(in_range)} of {len(route)} runs; per run: "
               + ", ".join(f"{r.fixture}{r.seed} {e2.pct(r.follows('T_A'))}" for r in route))
    pairs_g = [(r, ctl[(r.fixture, r.seed, "gated")]) for r in route if (r.fixture, r.seed, "gated") in ctl]
    pairs_n = [(r, ctl[(r.fixture, r.seed, "notepad")]) for r in route if (r.fixture, r.seed, "notepad") in ctl]
    out.append(sign("Q3 route meets T_A more than gated", [(r.follows("T_A") or 0) > (g.follows("T_A") or 0) for r, g in pairs_g], "6 of 6"))
    promoted = [first([t["promoted"] for t in t_a[id(r)]]) for r in route]
    out.append(f"Q4 T_A promoted by s20 in {sum(p is not None for p in promoted)} of {len(route)} route runs (predicted 2 to 5), "
               f"by s10 in {sum(p is not None and p <= 10 for p in promoted)} (predicted at most 2); sessions {promoted}")
    harm_in = [r for r in route if 0.10 <= (r.follows("F_A") or 0) <= 0.30]
    out.append(f"Q5 route harm in 10-30% in {len(harm_in)} of {len(route)} runs; per run: "
               + ", ".join(f"{r.fixture}{r.seed} {e2.pct(r.follows('F_A'))}" for r in route))
    out.append(sign("Q5 route harm below the notepad's", [(r.follows("F_A") or 0) < (n.follows("F_A") or 0) for r, n in pairs_n], "6 of 6"))
    out.append(sign("Q5 route harm above gated's", [(r.follows("F_A") or 0) > (g.follows("F_A") or 0) for r, g in pairs_g], "6 of 6"))
    out.append(sign("Q6 route passes more than gated", [(e2b_pass(r) or 0) > (e2b_pass(g) or 0) for r, g in pairs_g], "6 of 6"))
    out.append(sign("Q6 route passes more than the notepad", [(e2b_pass(r) or 0) > (e2b_pass(n) or 0) for r, n in pairs_n], "no direction"))
    out.append(sign("Q6 the notepad passes more than route", [(e2b_pass(n) or 0) > (e2b_pass(r) or 0) for r, n in pairs_n], "no direction"))
    f_promoted = sum(any(t["promoted"] is not None for t in f_a[id(r)]) for r in route)
    others = sum(1 for r in route for t in r.trial_info if t["promoted"] is not None and t not in t_a[id(r)])
    out.append(f"Q7 F_A promoted in {f_promoted} route runs (predicted 0); other promoted records to read: {others} (at most 1 run may hold a false one)")
    omitted = sum(s.trial_omitted for r in route for s in r.sessions)
    out.append(f"Q8 treated lines omitted for budget in route runs: {omitted} (predicted 0)")
    return out


def render(e2b_runs: list[e2.Run], e2_runs: list[e2.Run], show_promoted: bool) -> str:
    lines = ["run (exp fixture seed arm) | pass (E2b check) | harm | T_A | T_X | trials | agent/miner/curator tok"]
    for r in [*e2b_runs, *e2_runs]:
        if r.arm not in ("route", "gated", "notepad"):
            continue
        agent = sum(s.tokens.get("tokens_total", 0) for s in r.sessions)
        lines.append(f"{r.experiment} {r.fixture} {r.seed} {r.arm} | {e2.pct(e2b_pass(r))} | {e2.pct(r.follows('F_A'))} | "
                     f"{e2.pct(r.follows('T_A'))} | {e2.pct(r.follows('T_X'))} | {len(r.trial_info)} | {agent}/{r.miner_tokens}/{r.curator_tokens}")
    for r in e2b_runs:
        if r.arm not in ("route", "gated"):
            continue
        for item in ("T_A", "F_A"):
            for t in item_records(r, item):
                lines.append(f"  {r.fixture} {r.seed} {r.arm} {item}: proposed s{t['proposed']}, first coin s{t['first_coin']}, "
                             f"{t['status']} (wealth {t['wealth']:.2f}), promoted s{t['promoted']}: {t['content'][:90]}")
        if show_promoted:
            for t in r.trial_info:
                if t["promoted"] is not None:
                    lines.append(f"  promoted in {r.fixture} {r.seed} {r.arm} at s{t['promoted']}: {t['content']}")
    return "\n".join([*lines, "", *predictions(e2b_runs, e2_runs)])


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--e2", nargs="*", default=sorted(glob.glob("workload-runs/e2/seed-*")))
    ap.add_argument("--e2b", nargs="*", default=sorted(glob.glob("workload-runs/e2b/seed-*")))
    ap.add_argument("--promoted", action="store_true", help="print every promoted record to read")
    args = ap.parse_args(argv)
    e2b_runs = e2.load_runs(args.e2b) if args.e2b else []
    e2_runs = e2.load_runs(args.e2) if args.e2 else []
    if not e2b_runs:
        print("no E2b runs found")
        return 1
    print(render(e2b_runs, e2_runs, args.promoted))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""E2c: agent-visible claims routed to a typed confirm (docs/E2C.md; design and
predictions in RKA jrn_01M84P9ZSVWTNVKH7A6MJG89J0).

Reads E2c runs (default workload-runs/e2c*/seed-*) and, with --notepad, E2b's
notepad runs on the same model and level, which are compared with the stamp
runs only. Runs are kept apart by provider, model and reasoning level: the
directory a launcher wrote them to (<experiment>-<model>-<level>) and the
provider in each manifest. A set that mixes them is refused.

Prints per run (fixture, seed, arm), over s4 to s20, pass under the E2b
check, harm and the U, T_X and T_A rates. For the review arms it adds what
the review list did: lines listed, the session where a line stating T_A and
one stating F_A was first listed, the verdicts typed and when, the share of
listed lines that state T_A or F_A, T_A met and F_A followed after the
verdict on them, records withdrawn as dependents of a reject, unconfirmed
file claims that reached a packet, and calls to memory_explain on a file
claim. Then Q1 to Q7 and the decision rules of Section 7, with one-sided
exact sign tests in which a tie counts against the prediction.

Precision here uses the analysis's item patterns (scripts/e2_analysis.py
ITEMS), not the oracle's; the design judges it by reading, so --lines prints
every listed line with the oracle's decision to read.

Usage: .venv/bin/python scripts/e2c_analysis.py [--e2c DIR ...] [--notepad DIR ...] [--lines]
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2_analysis as e2

from rka_memory import config as config_mod
from rka_memory.domain import Kind, parse_ts
from rka_memory.kernel.labels import label
from rka_memory.world import World, project

REVIEW_ARMS = ("oracle", "stamp")
ORDER = ("oracle", "stamp", "gated-r1", "notepad")
HARM_CAP = 0.06
UPTAKE = 0.8
LISTED_CAP = 30
VERDICT_CAP = 4
PRECISION_FLOOR = 0.2
PRECISION_LOW = 0.1
STAMP_HARM = 0.5
WITHDRAWN_AS_DEPENDENT = "depends on a record the user withdrew"


@dataclass
class Review:
    """What the review list did in one run."""

    lines: list[dict] = field(default_factory=list)  # logs/review.jsonl, one entry per listed line
    first_listed: dict[str, int | None] = field(default_factory=dict)  # T_A, F_A -> session
    verdicts: list[tuple[int, str, str]] = field(default_factory=list)  # (session, decision, record_id), recorded
    untyped: list[str] = field(default_factory=list)  # verdicts the runner did not see recorded
    confirmed: dict[str, int | None] = field(default_factory=dict)  # T_A, F_A -> session of its confirm
    rejected: dict[str, int | None] = field(default_factory=dict)  # T_A, F_A -> session of its reject
    dependents_withdrawn: list[str] = field(default_factory=list)
    unconfirmed_delivered: list[str] = field(default_factory=list)
    explained: int = 0

    @property
    def precision(self) -> float | None:
        stating = [x for x in self.lines if x.get("item")]
        return len(stating) / len(self.lines) if self.lines else None


def session_number(label: str) -> int:
    return int(label.lstrip("s"))


def states(fixture: str, item: str, text: str) -> bool:
    pattern = e2.ITEMS[fixture][item]
    return bool(re.search(pattern, text, re.IGNORECASE)) and not re.search(e2.NEGATED + f"(?:{pattern})", text, re.IGNORECASE)


def item_of(fixture: str, text: str) -> str | None:
    """T_A or F_A when the line states exactly one of them."""
    found = [i for i in ("T_A", "F_A") if states(fixture, i, text)]
    return found[0] if len(found) == 1 else None


def review_of(run: e2.Run, arm_dir: Path) -> Review:
    out = Review()
    log = arm_dir / "logs" / "review.jsonl"
    entries = [json.loads(x) for x in log.read_text().splitlines() if x.strip()] if log.exists() else []
    for x in entries:
        x["item"] = item_of(run.fixture, x["content"])
        x["n"] = session_number(x["session"])
        out.lines.append(x)
        if x["decision"] != "leave" and x["typed"]:
            out.verdicts.append((x["n"], x["decision"], x["record_id"]))
        elif x["decision"] != "leave":
            out.untyped.append(x["record_id"])
    for item in ("T_A", "F_A"):
        out.first_listed[item] = min((x["n"] for x in entries if x["item"] == item), default=None)
        out.confirmed[item] = min((x["n"] for x in entries if x["item"] == item and x["decision"] == "confirm" and x["typed"]), default=None)
        out.rejected[item] = min((x["n"] for x in entries if x["item"] == item and x["decision"] == "reject" and x["typed"]), default=None)
    world = project(e2._events(arm_dir))
    cfg = replace(config_mod.load(path=arm_dir / "config.yaml"), ledger_dir=arm_dir / "ledger")
    now = max((parse_ts(e.ts) for e in world.events.values()), default=None)
    if now is not None:
        labels = label(world, cfg, now)
        out.dependents_withdrawn = sorted(r for r, lab in labels.by_id.items() if lab.reason == WITHDRAWN_AS_DEPENDENT)
    out.unconfirmed_delivered = unconfirmed_deliveries(world)
    claims = {r for r, rec in world.records.items() if rec.spec.claimed_by is not None}
    out.explained = sum(
        1 for e in world.events.values()
        if e.kind is Kind.TOOL_CALL and "memory_explain" in (text := json.dumps(dict(e.payload))) and any(c in text for c in claims)
    )
    return out


def unconfirmed_deliveries(world: World) -> list[str]:
    """File claims a packet held before the user confirmed or amended them."""
    by_user = {}
    for e in world.events.values():
        rid = e.payload.get("record_id") if e.kind is Kind.USER_CONFIRM else e.payload.get("target") if e.kind is Kind.USER_AMEND else None
        if rid is not None:
            by_user.setdefault(str(rid), world.position[e.id])
    out = []
    for d in world.deliveries:
        for rid in d.record_ids:
            rec = world.records.get(rid)
            if rec is not None and rec.spec.claimed_by is not None and by_user.get(rid, len(world.position)) > world.position[d.event.id]:
                out.append(rid)
    return sorted(set(out))


def rate_after(run: e2.Run, after: int | None, observer: str) -> float | None:
    """How often an observer held in the sessions after session `after`."""
    if after is None:
        return None
    later = [s for s in run.sessions if s.n > after and s.session_id]
    return sum(1 for s in later if s.observed.get(observer) is True) / len(later) if later else None


def route_key(arm_dir: Path) -> tuple[str, str]:
    """(provider, model and level) of a run: its manifest's provider, and its
    launcher directory without the experiment (e2c-gpt-6.2-sol-medium)."""
    manifest = json.loads((arm_dir / "manifest.json").read_text())
    root = arm_dir.parent.parent.name
    return manifest.get("provider") or "deepseek", root.split("-", 1)[1] if "-" in root else ""


@dataclass
class Loaded:
    runs: list[e2.Run]
    reviews: dict[int, Review]  # id(run) -> its review
    routes: set[tuple[str, str]]


def load(dirs: list[str]) -> Loaded:
    runs = e2.load_runs(dirs)
    arm_dirs = [a for d in dirs for a in sorted(Path(d).glob("e2*-*")) if (a / "report.json").exists()]
    reviews = {}
    for r in runs:
        if r.arm in REVIEW_ARMS:
            [arm_dir] = [a for a in arm_dirs if a.parent.name == r.run_dir and a.name == f"{r.experiment}-{r.fixture}-{r.arm}"]
            reviews[id(r)] = review_of(r, arm_dir)
    return Loaded(runs, reviews, {route_key(a) for a in arm_dirs})


def rates(run: e2.Run) -> dict[str, float | None]:
    return {"pass": run.pass_rate, "harm": run.follows("F_A"), "U": run.follows("U"), "T_X": run.follows("T_X"), "T_A": run.follows("T_A")}


def render_run(run: e2.Run, rv: Review | None) -> list[str]:
    r = rates(run)
    head = f"{run.fixture:9} {run.seed} {run.arm:8} " + " ".join(f"{k} {e2.pct(v)}" for k, v in r.items())
    if rv is None:
        return [head]
    listed = f"s{rv.first_listed['T_A']}" if rv.first_listed["T_A"] else "never"
    f_listed = f"s{rv.first_listed['F_A']}" if rv.first_listed["F_A"] else "never"
    verdicts = ", ".join(f"s{n} {d}" for n, d, _ in rv.verdicts) or "none"
    after_t = rate_after(run, rv.confirmed["T_A"], "T_A met")
    f_after = rv.confirmed["F_A"] if run.arm == "stamp" else rv.rejected["F_A"]
    return [
        head,
        f"  listed {len(rv.lines)} lines, T_A first {listed}, F_A first {f_listed}, precision {e2.pct(rv.precision)}",
        f"  verdicts {len(rv.verdicts)}: {verdicts}" + (f"; not recorded: {', '.join(rv.untyped)}" if rv.untyped else ""),
        (
            f"  T_A met after its confirm {e2.pct(after_t)}; F_A followed after its {'confirm' if run.arm == 'stamp' else 'reject'} "
            f"{e2.pct(rate_after(run, f_after, 'F_A followed'))}"
        ),
        (
            f"  withdrawn as dependents {len(rv.dependents_withdrawn)}, unconfirmed file claims delivered "
            f"{len(rv.unconfirmed_delivered)}, memory_explain on a file claim {rv.explained}"
        ),
    ]


def predictions(loaded: Loaded, notepad: list[e2.Run]) -> list[str]:
    runs, reviews = loaded.runs, loaded.reviews
    oracle = [r for r in runs if r.arm == "oracle"]
    stamp = [r for r in runs if r.arm == "stamp"]
    pairs = e2.pairs(runs)
    rv = {id(r): reviews[id(r)] for r in [*oracle, *stamp]}
    n = len(oracle)
    by_s3 = {i: sum(1 for r in oracle if (s := rv[id(r)].first_listed[i]) is not None and s <= 3) for i in ("T_A", "F_A")}
    both = sum(1 for r in oracle if all((s := rv[id(r)].first_listed[i]) is not None and s <= 3 for i in ("T_A", "F_A")))
    out = [f"Q1 T_A listed by s3 in {by_s3['T_A']} of {n} oracle runs, F_A in {by_s3['F_A']}, both in {both} (predicted at least 5 of 6 each)"]
    uptake = [(r, rate_after(r, rv[id(r)].confirmed["T_A"], "T_A met")) for r in oracle]
    confirmed = [(r, u) for r, u in uptake if u is not None]
    q2_fail = sum(1 for _, u in confirmed if u < UPTAKE)
    unread = sum(1 for r in oracle for x in rv[id(r)].lines if x["decision"] == "confirm" and x["item"] != "T_A")
    out.append(
        "Q2 T_A met after its confirm: " + (", ".join(f"{r.fixture} {r.seed} {e2.pct(u)}" for r, u in confirmed) or "no confirm")
        + f"; under {UPTAKE:.0%} in {q2_fail} of {len(confirmed)} (predicted none)"
        + (f"; {unread} oracle confirms the item patterns do not read as T_A, read them with --lines" if unread else "")
    )
    matched = [(p["oracle"], p["gated-r1"]) for p in pairs.values() if "oracle" in p and "gated-r1" in p]
    t_a_wins = [(o.follows("T_A") or 0) > (g.follows("T_A") or 0) for o, g in matched]
    pass_wins = [(o.pass_rate or 0) > (g.pass_rate or 0) for o, g in matched]
    out.append(e2.sign("Q3 oracle meets T_A more than gated-R1", t_a_wins, "6 of 6"))
    out.append(e2.sign("Q3 oracle passes more than gated-R1", pass_wins, "6 of 6"))
    harms = [(r, r.follows("F_A")) for r in oracle]
    over = [r for r, h in harms if h is not None and h > HARM_CAP]
    out.append(f"Q4 oracle harm at most {HARM_CAP:.0%} in {len(oracle) - len(over)} of {len(oracle)} runs (predicted every run): "
               + ", ".join(f"{r.fixture} {r.seed} {e2.pct(h)}" for r, h in harms))
    verdicts = [len(rv[id(r)].verdicts) for r in oracle]
    listed = [len(rv[id(r)].lines) for r in oracle]
    precision = [rv[id(r)].precision for r in oracle]
    out.append(
        f"Q5 oracle verdicts per run {verdicts} (predicted at most {VERDICT_CAP}), lines listed {listed} (at most {LISTED_CAP}),"
        f" precision {[e2.pct(p) for p in precision]} (at least {PRECISION_FLOOR:.0%}; by pattern, read with --lines)"
    )
    out.append("Q6 stamp harm: " + (", ".join(f"{r.fixture} {r.seed} {e2.pct(r.follows('F_A'))}" for r in stamp) or "no stamp runs")
               + f" (predicted at least {STAMP_HARM:.0%} each)")
    if notepad:
        out.append("   beside E2b's notepad harm: " + ", ".join(f"{r.fixture} {r.seed} {e2.pct(r.follows('F_A'))}" for r in notepad))
    leaked = {f"{r.fixture} {r.seed} {r.arm}": rv[id(r)].unconfirmed_delivered for r in [*oracle, *stamp] if rv[id(r)].unconfirmed_delivered}
    out.append(f"Q7 unconfirmed file claims in a packet: {leaked or 'none'} (predicted none)")
    out.append("")
    q1_runs = both
    adopt = q1_runs >= 5 and sum(t_a_wins) >= 5 and not over
    out.append(
        f"Rule adopt: Q1 in {q1_runs} of {n} runs (5 needed), oracle over gated-R1 on T_A in {sum(t_a_wins)} of {len(matched)}"
        f" pairs (5 needed), harm over {HARM_CAP:.0%} in {len(over)} runs (0 allowed) -> {'holds' if adopt else 'does not hold'}"
    )
    out.append(f"Rule miner cannot find the claims: T_A listed by s3 in {by_s3['T_A']} of {n} (fewer than 4 triggers it)"
               f" -> {'triggered' if by_s3['T_A'] < 4 else 'not triggered'}")
    many = sum(1 for x in listed if x > LISTED_CAP)
    low = sum(1 for p in precision if p is not None and p < PRECISION_LOW)
    out.append(f"Rule list asks too much: over {LISTED_CAP} lines in {many} runs, precision under {PRECISION_LOW:.0%} in {low}"
               f" (3 of either triggers it) -> {'triggered' if many >= 3 or low >= 3 else 'not triggered'}")
    out.append(f"Rule confirmed record not acted on: Q2 failed in {q2_fail} runs (3 triggers it)"
               f" -> {'triggered' if q2_fail >= 3 else 'not triggered'}")
    return out


def render(loaded: Loaded, notepad: list[e2.Run], show_lines: bool) -> str:
    lines = [f"route: {', '.join(f'{p} {m}' for p, m in sorted(loaded.routes))}", ""]
    order = sorted(loaded.runs, key=lambda r: (r.fixture, r.seed, ORDER.index(r.arm) if r.arm in ORDER else 9))
    for r in order:
        rv = loaded.reviews.get(id(r))
        lines += render_run(r, rv)
        if show_lines and rv is not None:
            lines += [f"    s{x['n']} {x['decision']:7} {x['item'] or '-':3} {x['record_id']}: {x['content']}" for x in rv.lines]
    return "\n".join([*lines, "", *predictions(loaded, notepad)])


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--e2c", nargs="*", default=sorted(glob.glob("workload-runs/e2c*/seed-*")))
    ap.add_argument("--notepad", nargs="*", default=[], help="E2b seed directories on the same model and level")
    ap.add_argument("--lines", action="store_true", help="print every listed line with the oracle's decision")
    args = ap.parse_args(argv)
    if not args.e2c:
        print("no E2c runs found")
        return 1
    routes = {route_key(a) for d in args.e2c for a in Path(d).glob("e2*-*") if (a / "report.json").exists()}
    if len(routes) > 1:
        print(f"refused: these runs used more than one provider, model or level: {sorted(routes)}")
        return 2
    loaded = load(args.e2c)
    notepad = [r for r in e2.load_runs(args.notepad) if r.arm == "notepad"] if args.notepad else []
    if notepad:
        theirs = {route_key(a) for d in args.notepad for a in Path(d).glob("e2*-notepad") if (a / "report.json").exists()}
        if theirs != loaded.routes:
            print(f"left out the notepad runs: they ran on {sorted(theirs)}, the E2c runs on {sorted(loaded.routes)}")
            notepad = []
    print(render(loaded, notepad, args.lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

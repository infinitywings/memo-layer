"""E2: gated memory against stated memory and a model-curated notepad
(docs/E2.md; design and predictions in RKA jrn_01M7R7ZSQR6BJNB68H29EEENEV).

Reads every E2 arm directory (a stream `e2-<fixture>` run per arm as
`e2-<fixture>-<arm>`) under the given run directories, default
workload-runs/*/. A fixture's seeds are its run directories in time order.
Prints per run (fixture, seed, arm), over the measured sessions s4 to s20:

- pass, the share whose user check passed, and harm, the share that
  followed F_A; the follow rates of U, T_X, T_A and F_A from the stream's
  observers;
- whether the agent read HANDOFF.md in s1 to s3, injected memory tokens,
  the cached share of prompt tokens, and tokens by role;
- in each session, which items the delivered memory held: gated and stated
  records, or notepad notes, matched by the item patterns below (print the
  notes with --notes to check a match by eye);
- gated: every record with trial coins and its status, and when the T_X
  record the runner proposed reached L2 and was first delivered;

then P1 to P10, with one-sided exact sign tests over the fixture-and-seed
pairs in which a tie counts against the prediction.

Usage: .venv/bin/python scripts/e2_analysis.py [--notes] [run_dir ...]
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from dataclasses import dataclass, field, replace
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rka_memory import config as config_mod  # noqa: E402
from rka_memory.domain import Actor, Event, Kind, Rung  # noqa: E402
from rka_memory.kernel import trials  # noqa: E402
from rka_memory.world import World, project  # noqa: E402

ARMS = ("stated", "gated", "notepad")
MEASURED = range(4, 21)
HANDOFF_SESSIONS = range(1, 4)
OBSERVERS = {"U": "U followed", "T_X": "T_X met", "T_A": "T_A met", "F_A": "F_A followed"}
LETTER = {"U": "U", "T_X": "X", "T_A": "A", "F_A": "F"}
NEGATED = r"\b(?:not|never|no|don't|avoid)\b\W+(?:[\w-]+\W+){0,2}?(?:blanket-)?"
# What a note or record must say to count as holding an item (docs/E2.md Section 4).
ITEMS = {
    "calc": {"U": r"docstring|usage example", "T_X": r"OPERATIONS", "T_A": r"\bbool", "F_A": r"\bround|decimal"},
    "textkit": {"U": r"type hint|annotat", "T_X": r"\bNone\b", "T_A": r"\blocale\b", "F_A": r"escap|&amp;|\bhtml"},
    "stockbook": {"U": r"test_<|test function", "T_X": r"numeric string|as strings?\b|\"12\"", "T_A": r"\baudit", "F_A": r"upper"},
}


@dataclass
class Session:
    n: int
    session_id: str | None
    observed: dict
    passed: bool | None = None
    memory: str = ""
    tokens: dict = field(default_factory=dict)
    packet_tokens: int = 0
    read_handoff: bool = False
    trial_omitted: int = 0


@dataclass
class Run:
    fixture: str
    arm: str
    run_dir: str
    sessions: list[Session]
    seed: int = 0
    miner_tokens: int = 0
    curator_tokens: int = 0
    trials: list[str] = field(default_factory=list)
    t_x_l2: int | None = None
    t_x_delivered: int | None = None
    experiment: str = "e2"
    # Every record with trial coins: id, content, status, coins and the
    # sessions of its first coin and of its promotion (E2b's Q1 to Q7).
    trial_info: list[dict] = field(default_factory=list)

    def measured(self) -> list[Session]:
        return [s for s in self.sessions if s.n in MEASURED]

    def rate(self, test) -> float | None:
        m = [s for s in self.measured() if s.session_id]
        return sum(1 for s in m if test(s)) / len(m) if m else None

    @property
    def pass_rate(self) -> float | None:
        return self.rate(lambda s: s.passed is True)

    def follows(self, item: str) -> float | None:
        return self.rate(lambda s: s.observed.get(OBSERVERS[item]) is True)

    def holds(self, item: str, n: int) -> bool:
        """A line names the item and does not negate it right before ("Do not
        blanket-round" reverses F_A). Read the notes with --notes to check."""
        s = next((s for s in self.sessions if s.n == n), None)
        pattern = ITEMS[self.fixture][item]
        return bool(s) and any(
            re.search(pattern, line, re.I) and not re.search(NEGATED + f"(?:{pattern})", line, re.I)
            for line in s.memory.splitlines()
        )

    def cached_share(self) -> float | None:
        read = sum(s.tokens.get("tokens_cache_read", 0) for s in self.sessions)
        prompt = read + sum(s.tokens.get("tokens_input", 0) + s.tokens.get("tokens_cache_write", 0) for s in self.sessions)
        return read / prompt if prompt else None


def _events(arm_dir: Path) -> list[Event]:
    path = arm_dir / "ledger" / "ledger.jsonl"
    return [Event.from_json(json.loads(line)) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _notes_by_session(arm_dir: Path, order: list[str | None]) -> dict[str, str]:
    """The notes each session received: the last version written after an earlier session."""
    versions = sorted((arm_dir / "ledger" / "notepad").glob("[0-9][0-9][0-9]-*.md"))
    out, earlier = {}, set()
    for sid in order:
        if sid is None:
            continue
        written = [v for v in versions if v.stem.split("-", 2)[2] in earlier]
        out[sid] = written[-1].read_text() if written else ""
        earlier.add(sid)
    return out


def summarize(arm_dir: Path) -> Run:
    manifest = json.loads((arm_dir / "manifest.json").read_text())
    # e2-<fixture>-<arm> or e2b-<fixture>-<arm>; E2b's route arm runs the
    # gated packet with the route's flags.
    experiment, rest = manifest["stream"].split("-", 1)
    fixture, arm = rest.rsplit("-", 1)
    rows = json.loads((arm_dir / "report.json").read_text())["sessions"]
    events = _events(arm_dir)
    world = project(events)
    sessions = [Session(i, row.get("session_id"), row.get("observed") or {}) for i, row in enumerate(rows, 1)]
    by_id = {s.session_id: s for s in sessions if s.session_id}
    for e in events:
        s = by_id.get(e.session_id)
        if s is None:
            continue
        if e.kind is Kind.CHECK and e.payload.get("check") == "outcome" and "passed" in e.payload:
            s.passed = bool(e.payload["passed"])
        elif e.kind is Kind.SESSION_END:
            s.tokens = {k: v for k, v in e.payload.items() if k.startswith("tokens_")}
        elif e.kind is Kind.PACKET_DELIVERED:
            s.trial_omitted += len(e.payload.get("trial_omitted") or ())
            if e.payload.get("channel") == "session_start":
                s.packet_tokens = int(e.payload.get("tokens") or 0)
        elif e.kind is Kind.TOOL_CALL and s.n in HANDOFF_SESSIONS and "HANDOFF" in json.dumps(dict(e.payload)):
            s.read_handoff = True
    run = Run(fixture, arm, arm_dir.parent.name, sessions, experiment=experiment)
    run.miner_tokens = sum(int(e.payload.get("tokens") or 0) for e in events if e.kind is Kind.COST and e.payload.get("component") == "miner")
    log = arm_dir / "ledger" / "notepad" / "log.jsonl"
    if log.exists():
        run.curator_tokens = sum(json.loads(line)["tokens"] for line in log.read_text().splitlines() if line.strip())
    if arm == "notepad":
        notes = _notes_by_session(arm_dir, [s.session_id for s in sessions])
        for s in sessions:
            s.memory = notes.get(s.session_id or "", "")
    else:
        for s in sessions:
            ids = {rid for d in world.deliveries if d.event.session_id == s.session_id for rid in d.record_ids}
            s.memory = "\n".join(world.records[r].spec.content for r in sorted(ids) if r in world.records)
    if arm in ("gated", "route"):
        _gated(run, world, arm_dir)
    return run


def _gated(run: Run, world: World, arm_dir: Path) -> None:
    cfg = config_mod.load(path=arm_dir / "config.yaml")
    cfg = replace(cfg, ledger_dir=arm_dir / "ledger")
    index = {s.session_id: s.n for s in run.sessions if s.session_id}
    ended = {world.position[s.ended.id]: index[sid] for sid, s in world.sessions.items() if s.ended is not None and sid in index}
    for rid in sorted({str(e.payload.get("record_id")) for e in world.trials}):
        if rid in world.records:
            rec = world.records[rid]
            t = trials.evaluate_sequential(rec, world, cfg)
            run.trials.append(f"{t.status} {t.treated}/{t.withheld} wealth {t.wealth:.2f}: {rec.spec.content[:70]}")
            coins = sorted(index[e.session_id] for e in world.trials if e.payload.get("record_id") == rid and e.session_id in index)
            run.trial_info.append({
                "record_id": rid, "content": rec.spec.content, "by": rec.proposed.actor.value,
                "proposed": index.get(rec.proposed.session_id), "status": t.status, "wealth": t.wealth,
                "first_coin": coins[0] if coins else None,
                "promoted": ended.get(t.completed_at) if t.status == "positive" else None,
            })
    for rid, rec in world.records.items():
        if rec.proposed.actor is not Actor.RUNNER or not re.search(ITEMS[run.fixture]["T_X"], rec.spec.content):
            continue
        l2 = [m for m in rec.history if m.to_rung is Rung.L2]
        if l2:
            # The gate logs its moves under its own session id: place the move
            # in the last stream session that had started by then.
            at = world.position[l2[0].event_id]
            started = [n for sid, n in index.items() if world.position[world.sessions[sid].started.id] <= at]
            run.t_x_l2 = max(started) if started else None
        delivered = [index[d.event.session_id] for d in world.deliveries if rid in d.record_ids and d.event.session_id in index]
        run.t_x_delivered = min(delivered) if delivered else None


def sign_p(wins: int, n: int) -> float:
    return sum(comb(n, k) for k in range(wins, n + 1)) / 2**n if n else 1.0


def pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def load_runs(paths: list[str]) -> list[Run]:
    dirs = [Path(p) for p in paths] or [Path(p) for p in sorted(glob.glob("workload-runs/*/"))]
    arm_dirs = sorted(a for d in dirs for a in (d.glob("e2*-*") if not d.name.startswith("e2") else [d]) if (a / "report.json").exists())
    runs = [summarize(a) for a in arm_dirs]
    for fixture in {r.fixture for r in runs}:
        stamps = sorted({r.run_dir for r in runs if r.fixture == fixture})
        for r in runs:
            if r.fixture == fixture:
                r.seed = stamps.index(r.run_dir) + 1
    order = ("stated", "gated", "route", "notepad")
    return sorted(runs, key=lambda r: (r.fixture, r.seed, order.index(r.arm) if r.arm in order else 9))


def pairs(runs: list[Run]) -> dict[tuple[str, int], dict[str, Run]]:
    out: dict[tuple[str, int], dict[str, Run]] = {}
    for r in runs:
        out.setdefault((r.fixture, r.seed), {})[r.arm] = r
    return out


def sign(name: str, wins: list[bool], predicted: str) -> str:
    return f"{name}: {sum(wins)} of {len(wins)} pairs, one-sided sign p = {sign_p(sum(wins), len(wins)):.3f} (predicted {predicted})"


def predictions(runs: list[Run]) -> list[str]:
    ps = pairs(runs)
    full = [p for p in ps.values() if all(a in p for a in ARMS)]
    gated = [r for r in runs if r.arm == "gated"]
    stated = [r for r in runs if r.arm == "stated"]
    out = []
    p1 = [r.t_x_l2 is not None and r.t_x_l2 <= 2 and r.t_x_delivered == 3 and (r.follows("T_X") or 0) >= 0.6 for r in gated]
    out.append(f"P1 T_X at L2 by s2, delivered from s3, followed >= 60%: {sum(p1)} of {len(p1)} gated runs (predicted all)")
    p2 = [max(r.follows("T_X") or 0, r.follows("T_A") or 0, r.pass_rate or 0) <= 0.1 for r in stated]
    out.append(f"P2 stated meets T_X, T_A and passes in at most 10%: {sum(p2)} of {len(p2)} runs (predicted all)")
    out.append(sign("P3 gated passes more than stated", [(p["gated"].pass_rate or 0) > (p["stated"].pass_rate or 0) for p in full], "6 of 6"))
    on_trial = [r for r in gated if any(re.search(ITEMS[r.fixture][i], t) for t in r.trials for i in ("T_A", "F_A"))]
    promoted_t_a = [r for r in gated if any(t.startswith("positive") and re.search(ITEMS[r.fixture]["T_A"], t) for t in r.trials)]
    harm_ok = [(r.follows("F_A") or 0) <= 0.12 for r in gated]
    out.append(
        f"P4 T_A or F_A on trial in {len(on_trial)} of {len(gated)} gated runs (predicted at most half); "
        f"T_A promoted in {len(promoted_t_a)} (predicted at most 1); gated harm <= 12% in {sum(harm_ok)} of {len(gated)}"
    )
    kept = {i: [(r.fixture, r.seed) for r in runs if r.arm == "notepad" and r.holds(i, 4)] for i in ("U", "T_X", "T_A", "F_A")}
    notepad_runs = sum(1 for r in runs if r.arm == "notepad")
    out.append("P5 the notepad held at s4: " + ", ".join(f"{i} in {len(v)} of {notepad_runs}" for i, v in kept.items()))
    f_kept = [p for k, p in ps.items() if k in kept["F_A"] and all(a in p for a in ARMS)]
    out.append(sign("P6 gated harm below notepad's where the notepad kept F_A", [(p["gated"].follows("F_A") or 0) < (p["notepad"].follows("F_A") or 0) for p in f_kept], "all"))
    out.append(sign("P7 gated passes more than the notepad where it kept F_A", [(p["gated"].pass_rate or 0) > (p["notepad"].pass_rate or 0) for p in f_kept], "all"))
    a_kept = [p for k, p in ps.items() if k in kept["T_A"] and all(a in p for a in ARMS)]
    out.append(sign("P8 the notepad meets T_A more than gated where it kept T_A", [(p["notepad"].follows("T_A") or 0) > (p["gated"].follows("T_A") or 0) for p in a_kept], "all"))
    p9 = [(r.follows("U") or 0) >= 0.9 for r in runs]
    out.append(f"P9 U followed in >= 90%: {sum(p9)} of {len(p9)} runs (predicted all)")
    diffs = [(p["gated"].cached_share(), p["notepad"].cached_share()) for p in full]
    diffs = [g - n for g, n in diffs if g is not None and n is not None]
    if diffs:
        mean = sum(diffs) / len(diffs)
        verdict = "no difference: drop advantage 4" if abs(mean) < 0.03 else ("gated higher" if mean > 0 else "notepad higher")
        out.append(f"P10 cached share, gated minus notepad: mean {mean:+.1%} over {len(diffs)} pairs ({verdict})")
    else:
        out.append("P10 cached share: the harness reported no cache counts")
    return out


def render(runs: list[Run], show_notes: bool) -> str:
    lines = ["run (fixture seed arm) | pass | harm (F_A) | U | T_X | T_A | HANDOFF read | packet tok | cached | agent/miner/curator tok"]
    for r in runs:
        agent = sum(s.tokens.get("tokens_total", 0) for s in r.sessions)
        packet = [s.packet_tokens for s in r.measured()]
        lines.append(
            f"{r.fixture} {r.seed} {r.arm} | {pct(r.pass_rate)} | {pct(r.follows('F_A'))} | {pct(r.follows('U'))} | "
            f"{pct(r.follows('T_X'))} | {pct(r.follows('T_A'))} | "
            f"{sum(s.read_handoff for s in r.sessions)}/3 | {sum(packet) / len(packet) if packet else 0:.0f} | "
            f"{pct(r.cached_share())} | {agent}/{r.miner_tokens}/{r.curator_tokens}"
        )
    for r in runs:
        held = " ".join(f"s{s.n}:" + "".join(LETTER[i] if r.holds(i, s.n) else "." for i in OBSERVERS) for s in r.sessions)
        lines.append(f"memory held (U X A F) {r.fixture} {r.seed} {r.arm}: {held}")
        if r.arm == "gated":
            lines.append(f"  T_X record: L2 at s{r.t_x_l2}, first delivered s{r.t_x_delivered}")
            lines += [f"  trial {t}" for t in r.trials]
        if show_notes and r.arm == "notepad":
            for s in r.sessions:
                lines.append(f"  notes at s{s.n}:\n" + "\n".join("    " + x for x in s.memory.splitlines()))
    return "\n".join([*lines, "", *predictions(runs)])


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notes", action="store_true", help="print the notes each notepad session received")
    ap.add_argument("runs", nargs="*")
    args = ap.parse_args(argv)
    runs = load_runs(args.runs)
    if not runs:
        print("no E2 runs found")
        return 1
    print(render(runs, args.notes))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

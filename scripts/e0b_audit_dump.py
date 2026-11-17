"""Input for experiment E0b: every record the kernel puts at L2 or L3 in the
saved live runs, with what two raters need to judge it and nothing that
states a verdict.

Pre-registered in RKA jrn_01M7NGP5QFYXE19PQEDYYYXDJM. Each ledger is
labelled with its own run's config.yaml at the time of its last event, as
scripts/label_dump.py does, with the kernel unchanged. For every record at
L2 or L3 it writes one JSON object per line to <out>/records.jsonl and one
line per record to <out>/index.txt: n, route, kind, and the first 100
characters of the content. The raters' instructions are in
<out>/RATER_GUIDE.md, written by hand.

A record's route is read from the kernel's own grounding (evidence.counted,
Grounding.affirms and the label's trial status), and checked against the
kernel's reason. Session indexes count the ledger's sessions in the order
they started, from 1, as the run's logs/sN.out files do. The L2 session is
the session holding the event at which the kernel dates the record's L2
stretch (Label.since): the miner links a session's evidence after the
session ends, so the record is at L2 from the next session on. The stream
file is the version in the repository's history whose session prompts
equal the prompts the ledger logged: the latest such version committed
before the run started, or the earliest one after it when the run used an
uncommitted file. Workspace commits are matched to sessions by the commit
messages that version has its drift and its user write.

Left out on purpose: the planted flag the runner writes on records it
plants, the miner's confidence, falsifier and cited quotes, rung changes,
withholding, the runner's checks, the session logs (the agent's reasoning
there speculates about planted records), and everything in report.json and
result.json.

Usage: .venv/bin/python scripts/e0b_audit_dump.py [--out DIR] [ledger.jsonl ...]
Without ledgers it reads every workload-runs/*/*/ledger/ledger.jsonl whose
run's manifest.json says the model was live, which leaves out scripted
probes saved beside them; DIR defaults to workload-runs/e0b. Counts go to
stderr. It writes only those two files, runs no model, and reads the
workspaces' git histories with hooks and fsmonitor off.
"""

from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from v2_replay import load  # noqa: E402

from rka_memory.domain import Actor, Event, Kind, Polarity, RecordKind, Rung, collapse, parse_ts  # noqa: E402
from rka_memory.kernel.evidence import Grounding, affirmation, counted, inside, statement  # noqa: E402
from rka_memory.kernel.labels import Label, label  # noqa: E402
from rka_memory.world import RecordState, World, project  # noqa: E402

TEXT_CAP = 2000
AT_LEAST_L2 = (Rung.L2, Rung.L3)
# The workspaces are repositories the agent owned: no hooks, no fsmonitor, no pager.
GIT = ["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
# What the kernel says for each route; an L3 record says how its skill held instead.
REASONS = {
    "standing_statement": ("the user stated it as a standing rule",),
    "affirmation": ("user confirmed", "written by the user"),
    "threshold": ("independent grounding in enough distinct sessions",),
    "trial": ("positive interventional contrast",),
}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run([*GIT, "-C", str(cwd), *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd}: {done.stderr.strip()[-300:]}")
    return done.stdout


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def cap(text: str) -> tuple[str, bool]:
    return (text[:TEXT_CAP], True) if len(text) > TEXT_CAP else (text, False)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def stream_version(source: str, started: datetime, prompts: list[str]) -> tuple[str, str | None, dict[str, Any]]:
    """The stream file's repository path, the commit holding the version the
    run used (None for the working tree), and that version parsed."""
    path = "workload/" + source.split("/workload/", 1)[1] if "/workload/" in source else source
    matches = []
    for line in git(ROOT, "log", "--format=%H %cI", "--", path).splitlines():
        commit, when = line.split()
        doc = yaml.safe_load(git(ROOT, "show", f"{commit}:{path}"))
        if [collapse(s.get("prompt") or "") for s in doc.get("sessions") or ()] == prompts:
            matches.append((datetime.fromisoformat(when), commit, doc))
    before = [m for m in matches if m[0] <= started]
    if before or matches:
        _, commit, doc = max(before, key=lambda m: m[0]) if before else min(matches, key=lambda m: m[0])
        return path, commit, doc
    warn(f"{path}: no committed version has the prompts the run logged; using the working tree")
    return path, None, yaml.safe_load((ROOT / path).read_text())


class Workspace:
    """The run's project repository and which commit each session started
    from and ended at, matched by the commit messages the stream file has
    its user (and its drift) write."""

    def __init__(self, path: Path | None, doc: dict[str, Any], arm: str) -> None:
        self.path = path
        self.root: str | None = None
        self.fresh = bool(doc.get("fresh_project"))
        self._placed: list[tuple[tuple[int, int], str]] = []
        if path is None or not (path / ".git").exists():
            self.history = "none"
            return
        commits = [line.split("\x1f", 1) for line in git(path, "log", "--reverse", "--format=%H%x1f%s").splitlines()]
        self.root = commits[0][0]
        if commits[0][1] != "fixture":
            warn(f"{arm}: the workspace's first commit is {commits[0][1]!r}, not the fixture")
        expected = [
            ((i, phase), str(action["commit"]))
            for i, sess in enumerate(doc.get("sessions") or (), 1)
            for phase, key in ((0, "drift"), (1, "user"))
            for action in sess.get(key) or ()
            if isinstance(action, dict) and "commit" in action
        ]
        at = 0
        for commit, subject in commits[1:]:
            k = next((x for x in range(at, len(expected)) if expected[x][1] == subject), None)
            if k is None:
                warn(f"{arm}: commit {commit[:10]} {subject!r} is none the stream file writes")
                continue
            self._placed.append((expected[k][0], commit))
            at = k + 1
        if self.fresh:
            self.history = "fresh_per_session"
        elif self._placed:
            self.history = "per_session_commits"
        else:
            self.history = "fixture_only"

    def _head(self, upto: tuple[int, int]) -> str | None:
        head = self.root
        for key, commit in self._placed:
            if key <= upto:
                head = commit
        return head

    def at_start(self, session: int | None) -> str | None:
        """HEAD when the session began, after any drift committed before it."""
        if session is None or self.root is None:
            return None
        if self.history == "per_session_commits":
            return self._head((session, 0))
        # A fresh project starts every session from the fixture; otherwise
        # only the first session's start is known.
        return self.root if self.fresh or session == 1 else None

    def at_end(self, session: int | None) -> str | None:
        """HEAD after the user's commit that closed the session."""
        if session is None or self.history != "per_session_commits":
            return None
        return self._head((session, 1))


def route_of(rec: RecordState, lab: Label, world: World, cfg) -> tuple[str, list[Grounding]]:
    """How the record reached L2, from the kernel's own grounding: the
    user's affirmations among its counted support (typed confirms and
    amendments, or standing statements of exactly its words), else a
    positive trial, else the support threshold."""
    support = [g for g in counted(rec, world, cfg, Polarity.SUPPORT) if inside(rec.scope, g)]
    affirms = [g for g in support if affirmation(g)]
    if any(g.event.kind in (Kind.USER_CONFIRM, Kind.USER_AMEND) for g in affirms):
        return "affirmation", affirms
    if affirms:
        return "standing_statement", affirms
    if lab.trial == "positive":
        return "trial", affirms
    return "threshold", affirms


def statements(rec: RecordState, affirms: list[Grounding], world: World, cfg, index, arm_of) -> list[dict[str, Any]]:
    """For each standing statement the kernel counted: the whole user
    message, and the quote and marker the miner's link carried."""
    out = []
    for g in affirms:
        if not g.affirms:
            continue
        for link in rec.links:
            if link.event_id != g.event.id or link.polarity is not Polarity.SUPPORT or link.via in world.defeated:
                continue
            made_by = world.events.get(link.via)
            if made_by is None or (made_by.actor is Actor.MODEL and made_by.session_id != g.event.session_id):
                continue
            said = statement(made_by, g.event, cfg)
            if said is None or not said[1] or collapse(said[0]) != collapse(rec.spec.content):
                continue
            out.append({
                "event_id": g.event.id,
                "session": index.get(g.event.session_id),
                "session_arm": arm_of.get(g.event.session_id),
                "message": str(g.event.payload.get("text") or ""),
                "quote": said[0],
                "marker": made_by.payload["statement"].get("marker"),
            })
            break
    return out


def event_view(ev: Event, calls: dict[tuple[str, str], Event], index, arm_of) -> dict[str, Any]:
    payload, cut = {}, []
    for key, value in ev.payload.items():
        if isinstance(value, str):
            value, was_cut = cap(value)
            if was_cut:
                cut.append(key)
        payload[key] = value
    out: dict[str, Any] = {
        "event_id": ev.id,
        "kind": ev.kind.value,
        "actor": ev.actor.value,
        "session": index.get(ev.session_id),
        "session_arm": arm_of.get(ev.session_id),
        "payload": payload,
    }
    if cut:
        out["truncated"] = cut
    call = calls.get((ev.session_id, str(ev.payload.get("tool_use_id") or "")))
    if call is not None:
        given = call.payload.get("input")
        text, was_cut = cap(given if isinstance(given, str) else json.dumps(given, ensure_ascii=False))
        out["tool_call"] = {"tool": call.payload.get("tool"), "input": text, **({"truncated": True} if was_cut else {})}
    return out


def live(path: Path) -> bool:
    manifest = path.parent.parent / "manifest.json"
    return manifest.exists() and json.loads(manifest.read_text()).get("model") == "live"


def dump_ledger(path: Path, tally: Counter, rungs: Counter, routes: Counter) -> list[dict[str, Any]]:
    events, cfg, _ = load(path)
    tally["ledgers"] += 1
    if not events:
        return []
    tally["ledgers with events"] += 1
    world = project(events)
    labels = label(world, cfg, parse_ts(events[-1].ts))
    ordered = list(world.events.values())
    rungs.update(lab.rung.value for lab in labels.by_id.values())

    arm_dir = path.parent.parent
    run = arm_dir.parent
    manifest = json.loads((arm_dir / "manifest.json").read_text())
    sessions = sorted((s for s in world.sessions.values() if s.started is not None), key=lambda s: world.position[s.started.id])
    index = {s.id: i for i, s in enumerate(sessions, 1)}
    arm_of = {}
    for d in world.deliveries:
        if d.channel == "session_start":
            arm_of.setdefault(d.event.session_id, d.arm)
    report = arm_dir / "report.json"
    if report.exists():
        rows = json.loads(report.read_text()).get("sessions") or []
        if [r.get("session_id") for r in rows] != [s.id for s in sessions]:
            warn(f"{rel(arm_dir)}: report.json lists sessions in another order than the ledger")
        for r in rows:
            if r.get("arm"):
                arm_of[r["session_id"]] = r["arm"]

    prompts = []
    for s in sessions:
        msg = next((e for e in s.events if e.kind is Kind.USER_MSG), None)
        prompts.append(collapse(str(msg.payload.get("text") or "")) if msg else "")
    stream_file, stream_commit, doc = stream_version(manifest["source"], datetime.fromisoformat(manifest["started_at"]), prompts)
    ws_path = next((p for p in (run / "workspaces" / arm_dir.name, arm_dir / "project") if p.is_dir()), None)
    ws = Workspace(ws_path, doc, rel(arm_dir))
    calls = {(e.session_id, str(e.payload.get("tool_use_id"))): e for e in ordered if e.kind is Kind.TOOL_CALL}
    fixture = doc.get("project")
    shared = len(cfg.arms) > 1

    out = []
    for rec in sorted(world.records.values(), key=lambda r: world.position[r.proposed.id]):
        lab = labels.by_id[rec.id]
        if lab.rung not in AT_LEAST_L2:
            continue
        route, affirms = route_of(rec, lab, world, cfg)
        if lab.rung is Rung.L2 and not lab.reason.startswith(REASONS[route]):
            warn(f"{rel(arm_dir)} {rec.id}: route {route} but the kernel says {lab.reason!r}")
            tally["route disagrees with the kernel's reason"] += 1
        l2_event = ordered[lab.since] if lab.since is not None else None
        l2_session = index.get(l2_event.session_id) if l2_event is not None else None
        fact = rec.spec.kind is RecordKind.FACT
        routes[(route, rec.spec.kind.value)] += 1
        out.append({
            "run": rel(run),
            "arm_dir": rel(arm_dir),
            "arm": "+".join(cfg.arms),
            "arms_share_ledger": shared,
            "stream": doc.get("name", manifest.get("stream")),
            "stream_file": stream_file,
            "stream_file_commit": stream_commit,
            "fixture": fixture,
            "fixture_dir": f"workload/fixtures/{fixture}" if fixture else None,
            "fixture_commit": ws.root,
            "workspace": rel(ws_path) if ws_path is not None else None,
            "workspace_history": ws.history,
            "record_id": rec.id,
            "kind": rec.spec.kind.value,
            "content": rec.spec.content,
            "scope": rec.scope.describe(),
            "rung": lab.rung.value,
            "reason": lab.reason,
            "route": route,
            "proposed_session": index.get(rec.proposed.session_id),
            "sessions_in_ledger": len(sessions),
            "l2_session": l2_session,
            "l2_event_id": l2_event.id if l2_event is not None else None,
            "l2_commit_start": ws.at_start(l2_session) if fact else None,
            "l2_commit_end": ws.at_end(l2_session) if fact else None,
            "statements": statements(rec, affirms, world, cfg, index, arm_of) if route == "standing_statement" else [],
            "support": [event_view(world.events[e], calls, index, arm_of) for e in lab.support],
        })
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "workload-runs" / "e0b")
    ap.add_argument("ledgers", nargs="*", type=Path)
    args = ap.parse_args(argv)
    paths = args.ledgers
    if not paths:
        found = [Path(p) for p in sorted(glob.glob(str(ROOT / "workload-runs/*/*/ledger/ledger.jsonl")))]
        paths = [p for p in found if live(p)]
        if len(paths) < len(found):
            print(f"skipped {len(found) - len(paths)} ledgers whose run was not live", file=sys.stderr)

    tally: Counter = Counter()
    rungs: Counter = Counter()
    routes: Counter = Counter()
    records = []
    for path in paths:
        records.extend(dump_ledger(path, tally, rungs, routes))

    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "records.jsonl").open("w", encoding="utf-8") as f:
        for n, row in enumerate(records, 1):
            f.write(json.dumps({"n": n, **row}, ensure_ascii=False) + "\n")
    with (args.out / "index.txt").open("w", encoding="utf-8") as f:
        for n, row in enumerate(records, 1):
            f.write(f"{n}\t{row['route']}\t{row['kind']}\t{collapse(row['content'])[:100]}\n")

    runs = defaultdict(set)
    ledgers = defaultdict(set)
    for row in records:
        key = collapse(row["content"])
        runs[key].add(row["run"])
        ledgers[key].add(row["arm_dir"])
    across_runs = [k for k, v in runs.items() if len(v) > 1]
    across_ledgers = [k for k, v in ledgers.items() if len(v) > 1]
    err = sys.stderr
    run_dirs = {p.parent.parent.parent for p in paths}
    print(f"ledgers: {tally['ledgers']} ({tally['ledgers with events']} with events) in {len(run_dirs)} run directories; records: {sum(rungs.values())}", file=err)
    print("rungs: " + ", ".join(f"{k} {v}" for k, v in sorted(rungs.items())), file=err)
    print(f"at L2 or L3: {len(records)}", file=err)
    for key in sorted(routes):
        print(f"  {key[0]:18} {key[1]:11} {routes[key]}", file=err)
    print(f"distinct contents: {len(runs)}", file=err)
    print(f"contents in more than one run: {len(across_runs)}, holding {sum(1 for r in records if collapse(r['content']) in set(across_runs))} records", file=err)
    print(f"contents in more than one ledger: {len(across_ledgers)}, holding {sum(1 for r in records if collapse(r['content']) in set(across_ledgers))} records", file=err)
    print(f"wrote {rel(args.out / 'records.jsonl')} and {rel(args.out / 'index.txt')}", file=err)
    return 1 if tally["route disagrees with the kernel's reason"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

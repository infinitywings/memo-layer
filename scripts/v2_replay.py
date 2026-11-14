"""Layer-1 and layer-2 gates of docs/DESIGN_V2.md over saved ledgers.

Labels every saved ledger with the v2 kernel and checks the predictions
registered before the kernel was written (P1-P6, RKA
jrn_01M7BN44F0GXVWJP7Z0ZCPFRMS), then renders the gated session-start view
for every recorded session state and checks the layer-2 predictions
registered before the views were written (V3 is P1-P6 again; V4: two renders
give the same bytes and no view holds a withdrawn record; RKA
jrn_01M7BP7B4QRV4ZCFNEVTWWC5MQ).

Usage: python scripts/v2_replay.py [ledger.jsonl ...]
Without arguments it reads every workload-runs/*/*/ledger/ledger.jsonl.
Each ledger is labelled with its own run's config.yaml at the time of its
last event, so retirement windows are judged as they were when it ended.
"""

from __future__ import annotations

import glob
import json
import sys
import time
from collections import Counter
from pathlib import Path

from rka_memory import config as config_mod
from rka_memory.domain import Event, RecordKind, Rung, parse_ts
from rka_memory.kernel import views
from rka_memory.kernel.labels import label
from rka_memory.world import project

PRESCRIPTIVE = {RecordKind.PROCEDURE, RecordKind.CONSTRAINT, RecordKind.PREFERENCE}
AT_LEAST = {Rung.L2, Rung.L3}


def load(path: Path):
    events = [Event.from_json(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
    run = path.parent.parent
    cfg = config_mod.load(path=run / "config.yaml") if (run / "config.yaml").exists() else config_mod.load()
    return events, cfg, run.name


def main(argv: list[str]) -> int:
    paths = [Path(p) for p in (argv or sorted(glob.glob("workload-runs/*/*/ledger/ledger.jsonl")))]
    tally: Counter = Counter()
    misses: dict[str, list[str]] = {**{f"P{i}": [] for i in range(1, 7)}, "V4": []}
    rendered = 0
    slowest = (0.0, "")
    for path in paths:
        events, cfg, name = load(path)
        world = project(events)
        now = max(parse_ts(e.ts) for e in events)
        t0 = time.perf_counter()
        labels = label(world, cfg, now)
        elapsed = time.perf_counter() - t0
        slowest = max(slowest, (elapsed, name))
        again = label(project(events), cfg, now)
        if again.by_id != labels.by_id:
            misses["P5"].append(name)
        planted = {e.payload["record_id"] for e in events if e.kind.value == "record_proposed" and e.payload.get("planted")}
        for rec in world.records.values():
            v1, v2 = rec.rung, labels.by_id[rec.id].rung
            if rec.id in planted:
                tally[("planted", "all", "v2>=L2" if v2 in AT_LEAST else "v2<L2")] += 1
                if v2 in AT_LEAST:
                    misses["P1"].append(f"{name}: {rec.id}")
                continue
            if v1 not in AT_LEAST:
                continue
            if labels.by_id[rec.id].affirmed:
                tally[("affirmed", rec.spec.kind.value, v2.value)] += 1
                if v2 not in AT_LEAST:
                    misses["P2"].append(f"{name}: {rec.id} {v2.value} ({labels.by_id[rec.id].reason})")
                continue
            tally[("mined", rec.spec.kind.value, v2.value)] += 1
            if rec.spec.kind in PRESCRIPTIVE and v2 in AT_LEAST:
                misses["P4"].append(f"{name}: {rec.id} {v2.value} ({labels.by_id[rec.id].reason})")

        for sess in world.sessions.values():
            if sess.started is None:
                continue
            first = views.packet(world, labels, cfg, sess.state, arm="gated", channel="session_start")
            again = views.packet(project(events), label(project(events), cfg, now), cfg, sess.state, arm="gated", channel="session_start")
            rendered += 1
            if first.render() != again.render():
                misses["V4"].append(f"{name}: {sess.id} renders differently")
            if set(first.record_ids) & labels.withdrawn:
                misses["V4"].append(f"{name}: {sess.id} delivers {sorted(set(first.record_ids) & labels.withdrawn)}")

    facts = {k[2]: n for k, n in tally.items() if k[0] == "mined" and k[1] == "fact"}
    kept = facts.get("L2", 0) + facts.get("L3", 0)
    total = sum(facts.values())
    if total and kept / total < 0.7:
        misses["P3"].append(f"{kept}/{total} facts stay at L2")
    if slowest[0] >= 5.0:
        misses["P6"].append(f"{slowest[1]}: {slowest[0]:.2f}s")

    print(f"ledgers: {len(paths)}")
    for key in sorted(tally):
        print(f"  {key[0]:9} {key[1]:11} {key[2]:7} {tally[key]}")
    print(f"mined facts staying at L2 or L3: {kept}/{total}")
    print(f"slowest labelling: {slowest[0]:.2f}s ({slowest[1]})")
    print(f"session-start views rendered twice: {rendered}")
    for p, found in misses.items():
        print(f"{p}: {'held' if not found else 'MISSED'}" + ("".join(f"\n    {m}" for m in found[:12])))
    return 1 if misses["P1"] or misses["P2"] or misses["P5"] or misses["V4"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

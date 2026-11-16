"""Check a gated-only rerun of the unseen-requirement stream against the
predictions for failure-gated withholding, registered in f82e102 (RKA
jrn_01M7JBXETAHQ19JR83XP24NX9C), W3 to W5.

W3 from the session after a record about calc.OPERATIONS is first
   delivered, the user's check passes in at least 80% of sessions.
W4 a trial on a calc.OPERATIONS record does not end positive if an
   OPERATIONS fact was delivered in one of its withheld sessions.
W5 no user_repeat event.
Reported with no threshold: the session of the first OPERATIONS delivery,
record-sessions withheld by a pending restriction, and tokens.

Usage: .venv/bin/python scripts/withholding_live_gate.py workload-runs/<run>
Exit 0 when every prediction held.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from unseen_requirement_gate import about_operations, after, arm, share  # noqa: E402
from v2_replay import load  # noqa: E402

from rka_memory.domain import Kind, RecordKind, parse_ts  # noqa: E402
from rka_memory.kernel.labels import label  # noqa: E402
from rka_memory.world import project  # noqa: E402


def withheld_at_starts(events, cfg, world) -> list[tuple[int, str, tuple[str, ...]]]:
    starts = sorted((world.position[s.started.id], sid) for sid, s in world.sessions.items() if s.started is not None)
    out = []
    for i, (pos, _) in enumerate(starts, 1):
        labs = label(project(events[:pos]), cfg, parse_ts(events[pos].ts))
        out += [(i, rid, lab.withheld_by) for rid, lab in labs.by_id.items() if lab.withheld_by]
    return out


def main(argv: list[str]) -> int:
    run = Path(argv[0])
    d = next(p for p in run.iterdir() if p.name.endswith("-gated"))
    events, cfg, _ = load(d / "ledger" / "ledger.jsonl")
    world = project(events)
    gated = arm(run, "gated")
    rows = gated["rows"]
    index = {r["id"]: r["s"] for r in rows}
    print(f"user's check passed {share(rows)}; hidden 'registered in OPERATIONS' {sum(r['hidden'] is True for r in rows)}/{len(rows)}")
    print("check by session:      " + "".join({True: "+", False: "-", None: "."}[r["check"]] for r in rows))
    print("OPERATIONS delivered:  " + "".join("D" if r["ops_delivered"] else "." for r in rows))
    for rec in gated["ops_records"]:
        print(f"record {rec['id']} {rec['rung']}: {rec['reason']} | {rec['content'][:100]}")
    for t in gated["trials"]:
        print(f"trial {t['id']} {t['trial']}: {t['reason']} | {t['content'][:80]}")

    ops_facts = {r.id for r in world.records.values() if r.spec.kind is RecordKind.FACT and about_operations(r.spec.content)}
    delivered_in: dict[str, set[str]] = {}
    for dl in world.deliveries:
        delivered_in.setdefault(dl.event.session_id, set()).update(dl.record_ids)
    confounded = []
    for t in (t for t in gated["trials"] if t["ops"] and t["completed_at"] is not None):
        withheld = [e.session_id for e in world.trials if e.payload.get("record_id") == t["id"] and e.payload.get("arm") == "withheld"
                    and world.sessions[e.session_id].ended is not None and world.position[world.sessions[e.session_id].ended.id] <= t["completed_at"]]
        hit = [index.get(s) for s in withheld if delivered_in.get(s, set()) & ops_facts]
        confounded.append((t, hit))
        print(f"trial {t['id'][-6:]}: withheld sessions {sorted(index.get(s) for s in set(withheld))}, an OPERATIONS fact delivered in {hit}")

    first = next((r["s"] for r in rows if r["ops_delivered"]), None)
    later = after(rows, first)
    results = {
        "W3 check passes in >= 80% after the first OPERATIONS delivery": (
            bool(later) and sum(r["check"] is True for r in later) >= 0.8 * len(later), f"first delivered in s{first}; {share(later)} after"),
        "W4 no positive OPERATIONS trial whose withheld arm had an OPERATIONS fact": (
            not any(t["trial"] == "positive" and hit for t, hit in confounded), f"{sum(1 for _, hit in confounded if hit)} such trials completed"),
        "W5 no user_repeat": (gated["repeats"] == 0, gated["repeats"]),
    }
    withheld = withheld_at_starts(events, cfg, world)
    print()
    for name, (ok, detail) in results.items():
        print(f"{'held  ' if ok else 'MISSED'} {name}: {detail}")
    print(f"record-sessions withheld by a pending restriction: {len(withheld)} {[(f's{i}', rid[-6:], [b[-6:] for b in by]) for i, rid, by in withheld]}")
    agent = sum(s["tokens_total"] for s in json.loads((d / "report.json").read_text())["sessions"])
    miner = sum(e.payload.get("tokens", 0) for e in events if e.kind is Kind.COST and e.payload.get("component") == "miner")
    print(f"tokens: agent {agent}, miner {miner}, total {agent + miner}")
    return 0 if all(ok for ok, _ in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

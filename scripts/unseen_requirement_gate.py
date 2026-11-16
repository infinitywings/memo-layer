"""Check the unseen-requirement stream against the predictions registered
in 527fffd (RKA jrn_01M7HG23P2VHYFPVNZGKRQ0Y25), P3 to P7, from one run
directory holding its gated, ungated and none arms.

Usage: .venv/bin/python scripts/unseen_requirement_gate.py workload-runs/<run>
Exit 0 when every prediction held.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from v2_replay import load  # noqa: E402

from rka_memory.domain import Kind, parse_ts  # noqa: E402
from rka_memory.kernel import trials  # noqa: E402
from rka_memory.kernel.labels import label  # noqa: E402
from rka_memory.world import project  # noqa: E402


def about_operations(content: str) -> bool:
    return "OPERATIONS" in content


def arm(run: Path, name: str) -> dict:
    d = next(p for p in run.iterdir() if p.name.endswith(f"-{name}"))
    events, cfg, _ = load(d / "ledger" / "ledger.jsonl")
    world = project(events)
    labels = label(world, cfg, max(parse_ts(e.ts) for e in events))
    report = json.loads((d / "report.json").read_text())
    rows = []
    for i, s in enumerate(report["sessions"], 1):
        sess = world.sessions.get(s["session_id"])
        verdicts = [e.payload for e in sess.events if e.kind is Kind.CHECK and e.payload.get("check") == "outcome"] if sess else []
        delivered = {rid for e in (sess.events if sess else ()) if e.kind is Kind.PACKET_DELIVERED for rid in e.payload.get("record_ids", ())}
        rows.append({
            "s": i,
            "id": s["session_id"],
            "check": verdicts[-1].get("passed") if verdicts else None,
            "hidden": (s.get("observed") or {}).get("registered in OPERATIONS"),
            "ops_delivered": any(about_operations(world.records[r].spec.content) for r in delivered if r in world.records),
            "end": world.position[sess.ended.id] if sess and sess.ended else None,
        })
    promoted = []
    for rid, lab in labels.by_id.items():
        if lab.trial is None:
            continue
        rec = world.records[rid]
        tr = trials.evaluate(rec, world, cfg)
        promoted.append({
            "id": rid, "content": rec.spec.content, "rung": lab.rung.value, "trial": lab.trial, "reason": lab.reason,
            "completed_at": tr.completed_at, "ops": about_operations(rec.spec.content),
        })
    ops_records = [{"id": r.id, "content": r.spec.content, "rung": labels.by_id[r.id].rung.value, "reason": labels.by_id[r.id].reason}
                   for r in world.records.values() if about_operations(r.spec.content)]
    repeats = sum(1 for e in events if e.kind is Kind.USER_REPEAT)
    return {"rows": rows, "trials": promoted, "ops_records": ops_records, "repeats": repeats}


def after(rows: list[dict], first: int | None) -> list[dict]:
    return [] if first is None else [r for r in rows if r["s"] > first]


def share(rows: list[dict]) -> str:
    return f"{sum(r['check'] is True for r in rows)}/{len(rows)}"


def main(argv: list[str]) -> int:
    run = Path(argv[0])
    arms = {name: arm(run, name) for name in ("gated", "ungated", "none")}
    for name, a in arms.items():
        print(f"== {name}: user's check passed {share(a['rows'])}; hidden 'registered in OPERATIONS' {sum(r['hidden'] is True for r in a['rows'])}/{len(a['rows'])}; user_repeat {a['repeats']}")
        print("   check by session: " + "".join({True: "+", False: "-", None: "."}[r["check"]] for r in a["rows"]))
        print("   OPERATIONS delivered: " + "".join("D" if r["ops_delivered"] else "." for r in a["rows"]))
        for rec in a["ops_records"]:
            print(f"   record {rec['id']} {rec['rung']}: {rec['reason']} | {rec['content'][:100]}")
        for t in a["trials"]:
            print(f"   trial {t['id']} {t['trial']}: {t['reason']} | {t['content'][:80]}")
    results = {}
    none = arms["none"]["rows"]
    results["P3 none: check passes in at most 2 of 20"] = (sum(r["check"] is True for r in none) <= 2, share(none))
    gated = arms["gated"]
    positive = [t for t in gated["trials"] if t["trial"] == "positive"]
    ops_positive = [t for t in positive if t["ops"]]
    others = [t for t in positive if not t["ops"]]
    promoted_in = None
    if ops_positive:
        at = min(t["completed_at"] for t in ops_positive)
        promoted_in = next(r["s"] for r in gated["rows"] if r["end"] == at)
    results["P4 gated: an OPERATIONS record promoted by a positive trial, no other"] = (bool(ops_positive) and not others, f"promoted in s{promoted_in}; other positive trials {len(others)}")
    later = after(gated["rows"], promoted_in)
    results["P5 gated: check passes in >= 80% after promotion"] = (bool(later) and sum(r["check"] is True for r in later) >= 0.8 * len(later), share(later))
    ungated = arms["ungated"]["rows"]
    first = next((r["s"] for r in ungated if r["ops_delivered"]), None)
    later = after(ungated, first)
    results["P6 ungated: check passes in >= 80% after first delivery"] = (bool(later) and sum(r["check"] is True for r in later) >= 0.8 * len(later), f"first delivered in s{first}; {share(later)} after")
    results["P7 no user_repeat in any arm"] = (all(a["repeats"] == 0 for a in arms.values()), {k: a["repeats"] for k, a in arms.items()})
    print()
    for name, (ok, detail) in results.items():
        print(f"{'held  ' if ok else 'MISSED'} {name}: {detail}")
    print(f"sessions-to-promotion (gated): {promoted_in}")
    return 0 if all(ok for ok, _ in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

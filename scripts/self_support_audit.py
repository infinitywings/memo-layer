"""Layer-4 live gate, LG4: how much validated memory supported itself.

For every run ledger given (or every workload-runs/*/*/ledger/ledger.jsonl),
take the records at L2 or L3 at the end of the run that the user did not
affirm, and count those with any supporting event produced in a session
where a similar record was already in the agent's context: delivered
before the event in that session, or a decision made in it before.
"Similar" is token-Jaccard similarity of at least 0.5 over a tokenizer of
this script's own, deliberately not the kernel's reach, identity classes or
dedup threshold, so the audit does not grade the rule by its own
definition.

Rungs come from the run's own engine: for a v1 run, the rung its engine
recorded and the support it counted; for a v2 run, the kernel's label and
the support it counted. Usage:

    python scripts/self_support_audit.py [ledger.jsonl ...]
"""

from __future__ import annotations

import glob
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from v2_replay import load  # noqa: E402

from rka_memory.domain import Polarity, RecordKind, Rung, parse_ts  # noqa: E402
from rka_memory.kernel.evidence import affirmation, counted  # noqa: E402
from rka_memory.kernel.labels import label  # noqa: E402
from rka_memory.world import World, project  # noqa: E402

THRESHOLD = 0.5
WORD = re.compile(r"[a-z0-9]+")
VALID = (Rung.L2, Rung.L3)


def words(text: str) -> frozenset[str]:
    return frozenset(WORD.findall(text.lower()))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def in_context(world: World, session_id: str, before: int) -> set[str]:
    out = {rid for d in world.deliveries if d.event.session_id == session_id and world.position[d.event.id] < before for rid in d.record_ids}
    out |= {r.id for r in world.records.values() if r.spec.kind is RecordKind.DECISION and r.proposed.session_id == session_id and world.position[r.proposed.id] < before}
    return out


def audit(path: Path) -> dict:
    events, cfg, name = load(path)
    world = project(events)
    planted = {e.payload["record_id"] for e in events if e.kind.value == "record_proposed" and e.payload.get("planted")}
    v2 = any(e.kind.value == "rung_change" and e.payload.get("derived") == "kernel" for e in events)
    if v2:
        labels = label(world, cfg, max(parse_ts(e.ts) for e in events))
        valid = {rid: set(labels.by_id[rid].support) for rid, lab in labels.by_id.items() if lab.valid and not lab.affirmed}
    else:
        valid = {}
        for rec in world.records.values():
            if rec.rung not in VALID:
                continue
            support = counted(rec, world, cfg, Polarity.SUPPORT)
            if any(affirmation(g) for g in support):
                continue
            valid[rec.id] = {g.event.id for g in support}
    toks = {rid: words(r.spec.content) for rid, r in world.records.items()}
    selfish = []
    for rid, support in valid.items():
        for eid in support:
            ev = world.events.get(eid)
            if ev is None:
                continue
            near = [o for o in in_context(world, ev.session_id, world.position[eid]) if o in toks and jaccard(toks[rid], toks[o]) >= THRESHOLD]
            if near:
                selfish.append(rid)
                break
    return {
        "run": path.parent.parent.parent.name,
        "arm": name,
        "engine": "v2" if v2 else "v1",
        "valid_unaffirmed": len(valid),
        "self_supported": len(selfish),
        "planted_valid": sorted(set(valid) & planted),
    }


def main(argv: list[str]) -> int:
    paths = [Path(p) for p in (argv or sorted(glob.glob("workload-runs/*/*/ledger/ledger.jsonl")))]
    rows = [audit(p) for p in paths]
    by = defaultdict(lambda: [0, 0])
    for r in rows:
        by[(r["engine"], r["arm"])][0] += r["self_supported"]
        by[(r["engine"], r["arm"])][1] += r["valid_unaffirmed"]
        print(json.dumps(r))
    for (engine, arm), (s, n) in sorted(by.items()):
        print(f"{engine} {arm:28} self-supported {s}/{n}" + (f" = {s / n:.0%}" if n else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

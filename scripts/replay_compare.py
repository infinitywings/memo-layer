"""Compare miner_replay reports: relation counts, and every proposed relation
with the text at both ends, for judging by hand.

Usage: python scripts/replay_compare.py <name>=<report.json>[,<report.json>...] ...
Each name may combine several reports (the parallel parts of one replay).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RELATIONS = ("refines", "depends_on", "supersedes")


def load(spec: str) -> tuple[str, dict]:
    name, _, paths = spec.partition("=")
    rows: dict = {}
    for path in paths.split(","):
        rows.update(json.loads(Path(path).read_text()))
    return name, rows


def text_of(ref: str, row: dict) -> str:
    for r in row.get("listed", []):
        if r["id"] == ref:
            return f"[{r['kind']}] {r['content']}"
    for r in row.get("kept", []):
        if r["id"] == ref:
            return f"[new] {r['content']}"
    return f"<{ref}>"


def main() -> int:
    reports = [load(a) for a in sys.argv[1:]]
    for name, rows in reports:
        cands = [c for r in rows.values() for c in r.get("candidates", [])]
        kept = [c for r in rows.values() for c in r.get("kept", [])]
        counts = {k: sum(1 for c in cands if c.get(k)) for k in RELATIONS}
        kept_counts = {k: sum(1 for c in kept if c.get(k)) for k in RELATIONS}
        tokens = sum(r.get("tokens", 0) for r in rows.values())
        print(f"== {name}: {len(rows)} sessions, {len(cands)} candidates, proposed {counts}, kept {kept_counts}, tokens {tokens}")
        for label, r in rows.items():
            for c in r.get("kept", []):
                for k in RELATIONS:
                    targets = c.get(k) or []
                    for t in [targets] if isinstance(targets, str) else targets:
                        print(f"  {label}: {k}\n      {c['content'][:110]}\n   -> {text_of(t, r)[:110]}")
            for d in r.get("dropped", []):
                if any(k in d["item"] for k in RELATIONS):
                    print(f"  {label}: dropped {d['item'][:80]} ({d['reason']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

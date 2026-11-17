"""E0b: combine the two raters' verdicts on the records at L2 (RKA
jrn_01M7NGP5QFYXE19PQEDYYYXDJM).

Reads workload-runs/e0b/records.jsonl (from scripts/e0b_audit_dump.py) and
the raters' raterA_part*.jsonl and raterB_part*.jsonl, and prints, per route,
each rater's counts, Cohen's kappa, the records the raters disagree on, and
fact precision under three readings: rater A, rater B, and strict (false if
either rater says false).

Usage: .venv/bin/python scripts/e0b_agreement.py [dir]
"""

from __future__ import annotations

import glob
import json
import sys
from collections import Counter
from pathlib import Path


def load(pattern: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for path in sorted(glob.glob(pattern)):
        for line in Path(path).read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                out[row["n"]] = row
    return out


def kappa(pairs: list[tuple[str, str]]) -> float | None:
    if not pairs:
        return None
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    left, right = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(left[k] * right[k] for k in set(left) | set(right)) / (n * n)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def main(argv: list[str]) -> int:
    base = Path(argv[0] if argv else "workload-runs/e0b")
    records = {r["n"]: r for r in map(json.loads, (base / "records.jsonl").read_text().splitlines())}
    a, b = load(str(base / "raterA_part*.jsonl")), load(str(base / "raterB_part*.jsonl"))
    missing = set(records) - set(a) | set(records) - set(b)
    if missing:
        print(f"missing verdicts for n={sorted(missing)}")
        return 1
    routes = sorted({r["route"] for r in records.values()})
    for question in ("truth", "rederivable", "as_rule"):
        print(f"\n## {question}")
        for route in routes:
            ns = [n for n in sorted(records) if records[n]["route"] == route and a[n].get(question) is not None]
            if not ns:
                continue
            pairs = [(str(a[n][question]), str(b[n][question])) for n in ns]
            k = kappa(pairs)
            print(f"{route} ({len(ns)}): A {dict(Counter(p for p, _ in pairs))}  B {dict(Counter(p for _, p in pairs))}"
                  f"  agree {sum(x == y for x, y in pairs)}/{len(ns)}  kappa {'n/a' if k is None else f'{k:.2f}'}")
            for n, (x, y) in zip(ns, pairs):
                if x != y:
                    print(f"  n={n} A={x} B={y} :: {records[n]['content'][:110]}")
    facts = [n for n in sorted(records) if records[n]["route"] == "threshold"]
    for name, false in (("rater A", lambda n: a[n]["truth"] == "false"),
                        ("rater B", lambda n: b[n]["truth"] == "false"),
                        ("strict", lambda n: "false" in (a[n]["truth"], b[n]["truth"]))):
        bad = [n for n in facts if false(n)]
        print(f"\nfact precision, {name}: {len(facts) - len(bad)}/{len(facts)} = {(len(facts) - len(bad)) / len(facts):.1%}  false: {bad}")
    for name, rater in (("rater A", a), ("rater B", b)):
        no = [n for n in facts if rater[n]["rederivable"] == "no"]
        print(f"facts rederivable, {name}: {len(facts) - len(no)}/{len(facts)}  not: {no}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

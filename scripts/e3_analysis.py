#!/usr/bin/env python3
"""E3's measures and predictions (docs/E3.md Sections 5 to 7) from one run
directory of scripts/e3_bench.py.

    scripts/e3_analysis.py workload-runs/e3-<model>-<level>-judge-<judge>/pilot [--items]
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ARMS = ("in-context", "none", "gated", "sourced", "ungated", "notepad")
SCORED = ("procedural_memory", "classical_conditioning")
SHORT = {"procedural_memory": "procedural", "classical_conditioning": "conditioning", "priming": "priming"}


def load(run: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    meta = json.loads((run / "run.json").read_text())
    items = [json.loads(p.read_text()) for p in sorted((run / "items").glob("*/*.json"))]
    for doc in items:
        got = (doc["agent_model"], doc["effort"], doc["judge_model"])
        if got != (meta["model"], meta["reasoning"], meta["judge"]):
            raise SystemExit(f"refused: {doc['key']} ran on {got}, the run on {meta}")
    return meta, items


def mcnemar(a: Sequence[bool], b: Sequence[bool]) -> tuple[int, int, float]:
    """Discordant pairs (a only, b only) and the two-sided exact p."""
    only_a = sum(x and not y for x, y in zip(a, b, strict=True))
    only_b = sum(y and not x for x, y in zip(a, b, strict=True))
    n = only_a + only_b
    if n == 0:
        return 0, 0, 1.0
    k = min(only_a, only_b)
    p = 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return only_a, only_b, min(1.0, p)


def wilcoxon(a: Sequence[float], b: Sequence[float]) -> float:
    """Two-sided Wilcoxon signed-rank p for paired scores, zeros dropped:
    exact for up to 20 pairs, a normal approximation above."""
    d = [x - y for x, y in zip(a, b, strict=True) if x != y]
    n = len(d)
    if n == 0:
        return 1.0
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    w = sum(r for r, x in zip(ranks, d, strict=True) if x > 0)
    total = n * (n + 1) / 2
    if n <= 20:
        dist = Counter(sum(c) for c in itertools.product(*[(0.0, r) for r in ranks]))
        extreme = min(w, total - w)
        return min(1.0, 2 * sum(v for s, v in dist.items() if s <= extreme + 1e-9) / 2**n)
    mean, var = total / 2, n * (n + 1) * (2 * n + 1) / 24
    z = (abs(w - mean) - 0.5) / math.sqrt(var)
    return min(1.0, math.erfc(z / math.sqrt(2)))


def correct(items: list[dict[str, Any]], family: str, arm: str) -> list[bool]:
    return [bool(d["verdicts"][arm]["correct"]) for d in items if d["family"] == family]


def scores(items: list[dict[str, Any]], arm: str) -> list[float]:
    return [float(d["scores"][arm]["score"]) for d in items if d["family"] == "priming"]


def pct(xs: Sequence[bool]) -> float:
    return 100.0 * sum(xs) / len(xs) if xs else float("nan")


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def contexts(d: dict[str, Any]) -> list[dict[str, Any]]:
    return list(d["contexts"].values())


def memory_lines(items: list[dict[str, Any]]) -> list[str]:
    out = []
    for family in ("procedural_memory", "classical_conditioning", "priming"):
        fam = [d for d in items if d["family"] == family]
        if not fam:
            continue
        ctxs = [c for d in fam for c in contexts(d)]
        recs = [r for c in ctxs for r in c["records"]]
        rungs = Counter(r["rung"] for r in recs)
        kinds = Counter(r["kind"] for r in recs)
        with_rec = sum(bool(c["records"]) for c in ctxs)
        nonempty = {a: sum(bool(c["arms"][a]["packet"]) for c in ctxs) for a in ("gated", "sourced", "ungated", "notepad")}
        reasons = Counter(r["why"] for r in recs if r["rung"] != "L1")
        unmined = sum(c["mined"].get("status") != "mined" for c in ctxs)
        uncurated = Counter(s.get("error") or "?" for c in ctxs for s in c["curated"] if not s.get("ok"))
        out.append(f"  miner calls failed: {unmined}; curator calls without notes: {dict(uncurated) or 0}")
        out.append(f"{SHORT[family]}: {len(ctxs)} sessions mined, {with_rec} with a record; {len(recs)} records "
                   f"{dict(sorted(rungs.items()))}, kinds {dict(sorted(kinds.items()))}")
        out.append(f"  packets not empty: {nonempty}")
        if reasons:
            out.append(f"  not L1: {dict(reasons.most_common(4))}")
        l2 = [r for r in recs if r["rung"] in ("L2", "L3")]
        for r in l2[:5]:
            out.append(f"  L2: {r['content'][:90]} ({r['why']})")
    return out


def usage(run: Path, items: list[dict[str, Any]]) -> list[str]:
    calls: Counter[str] = Counter()
    tokens: Counter[str] = Counter()
    for d in items:
        for c in contexts(d):
            for u in c["usage"].values():
                calls["agent"] += 1
                tokens["agent"] += u.get("input", 0)
            for s in c.get("curated", []):
                calls["curator"] += 1
                tokens["curator (total)"] += int(s.get("tokens", 0))
        judged = d.get("verdicts") or d.get("scores") or {}
        for v in judged.values():
            if "usage" in v:
                calls["judge"] += 1
                tokens["judge"] += v["usage"].get("input", 0)
    for ledger in (run / "ledgers").glob("*/*/*/rka/ledger.jsonl"):
        for line in ledger.read_text().splitlines():
            e = json.loads(line)
            if e["kind"] == "cost" and e["payload"].get("component") == "miner":
                calls["miner+extractor"] += 1
                tokens["miner+extractor (total)"] += int(e["payload"].get("tokens", 0))
    return [f"usage: calls {dict(calls)}; tokens {dict(tokens)}"]


def predictions(items: list[dict[str, Any]]) -> list[str]:
    out = []
    acc = {f: {a: correct(items, f, a) for a in ARMS} for f in SCORED if any(d["family"] == f for d in items)}
    pri = {a: scores(items, a) for a in ARMS} if any(d["family"] == "priming" for d in items) else {}

    def diff(f: str, a: str, b: str) -> float:
        return pct(acc[f][a]) - pct(acc[f][b])

    def test(f: str, a: str, b: str) -> str:
        x, y, p = mcnemar(acc[f][a], acc[f][b])
        return f"{a} {pct(acc[f][a]):.0f}% vs {b} {pct(acc[f][b]):.0f}% ({x}:{y} discordant, p = {p:.3f})"

    for f in acc:
        q1 = diff(f, "in-context", "none")
        out.append(f"Q1 {SHORT[f]}: {test(f, 'in-context', 'none')}; predicted +30 or more -> {'holds' if q1 >= 30 else 'fails'}")
    q2 = []
    for f in acc:
        q2.append(abs(diff(f, "gated", "none")) <= 10)
        out.append(f"Q2 {SHORT[f]}: {test(f, 'gated', 'none')}")
    if pri:
        q2.append(abs(mean(pri["gated"]) - mean(pri["none"])) <= 10)
        out.append(f"Q2 priming: gated {mean(pri['gated']):.1f} vs none {mean(pri['none']):.1f}")
    out.append(f"Q2 gated within 10 of none in every family -> {'holds' if q2 and all(q2) else 'fails'}")
    f = "classical_conditioning"
    if f in acc:
        for a in ("sourced", "ungated"):
            out.append(f"Q3 {test(f, a, 'none')}")
        q3 = all(diff(f, a, "none") >= 15 for a in ("sourced", "ungated"))
        out.append(f"Q3 sourced and ungated +15 or more over none in conditioning -> {'holds' if q3 else 'fails'}")
    f = "procedural_memory"
    if f in acc:
        out.append(f"Q4 {test(f, 'notepad', 'sourced')}; sourced vs none {diff(f, 'sourced', 'none'):+.0f}")
        q4 = diff(f, "notepad", "sourced") >= 20 and abs(diff(f, "sourced", "none")) <= 10
        out.append(f"Q4 notepad +20 over sourced, sourced within 10 of none -> {'holds' if q4 else 'fails'}")
    if pri:
        line = ", ".join(f"{a} {mean(pri[a]):.1f}" for a in ARMS)
        out.append(f"Q5 priming influence: {line}")
        for a in ("gated", "sourced", "ungated", "notepad"):
            out.append(f"Q5 {a} vs none: Wilcoxon p = {wilcoxon(pri[a], pri['none']):.3f}")
        q5 = all(mean(pri[a]) - mean(pri["none"]) <= 5 for a in ("gated", "sourced", "ungated")) and mean(pri["notepad"]) > mean(pri["gated"])
        out.append(f"Q5 rka arms within 5 of none, notepad above gated -> {'holds' if q5 else 'fails'}")
    harm = [(SHORT[f], a, diff(f, a, "none")) for f in acc for a in ("gated", "sourced", "ungated", "notepad") if diff(f, a, "none") < -5]
    out.append(f"Q6 memory arms more than 5 below none: {harm or 'none'} -> {'holds' if not harm else 'fails'}")
    if "classical_conditioning" in acc:
        rule = "a miner fault or a kernel rule: read the records" if diff("classical_conditioning", "sourced", "none") < 5 else "not triggered"
        out.append(f"Rule miner fault or kernel rule: {rule}")
    if pri:
        out.append(f"Rule priming as a hazard: notepad - gated = {mean(pri['notepad']) - mean(pri['gated']):+.1f} "
                   f"-> {'triggered' if mean(pri['notepad']) - mean(pri['gated']) >= 10 else 'not triggered'}")
    return out


def table(items: list[dict[str, Any]]) -> list[str]:
    out = ["family        n  " + "  ".join(f"{a:>10}" for a in ARMS)]
    for f in ("procedural_memory", "classical_conditioning", "priming"):
        fam = [d for d in items if d["family"] == f]
        if not fam:
            continue
        if f == "priming":
            vals = [f"{mean(scores(items, a)):10.1f}" for a in ARMS]
        else:
            vals = [f"{pct(correct(items, f, a)):9.0f}%" for a in ARMS]
        out.append(f"{SHORT[f]:<13}{len(fam):3d}  " + "  ".join(vals))
    return out


def judge_health(items: list[dict[str, Any]]) -> list[str]:
    bad = defaultdict(int)
    for d in items:
        for v in (d.get("verdicts") or d.get("scores") or {}).values():
            if v.get("parsed") is False:
                bad[d["family"]] += 1
    return [f"judge replies without JSON: {dict(bad) or 'none'}"]


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("--items", action="store_true", help="print each item's answers and verdicts per arm")
    args = ap.parse_args(argv)
    meta, items = load(args.run)
    print(f"run: {meta['model']} at {meta['reasoning']}, judge {meta['judge']}, data {meta['manifest_commit'][:8]}; "
          f"{len(items)} items; errors {len(list((args.run / 'errors').glob('*.txt'))) if (args.run / 'errors').exists() else 0}")
    for line in [*table(items), "", *predictions(items), "", *memory_lines(items), "", *judge_health(items), *usage(args.run, items)]:
        print(line)
    if args.items:
        for d in items:
            print(f"\n## {d['family']} {d['key']} ({d['task']})")
            for name, c in d["contexts"].items():
                for a in ARMS:
                    v = (d.get("verdicts") or {}).get(a) or (d.get("scores") or {}).get(a) or {}
                    mark = v.get("correct", v.get("score"))
                    print(f"  [{name}] {a}: {mark} | {c['arms'][a]['answer'][:160]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

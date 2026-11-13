"""Step 1 of the plan recorded in the RKA project (jrn_01M7B7YHTPRFGN97QZ5V0K7HW1):
does the gated arm, which also delivers the miner's lessons, save anything
over the stated arm, which delivers only what the user affirmed?

Usage: python scripts/stated_vs_gated.py <stream result dir>...
Each directory holds the report.json of one per-arm live stream, such as
<run>/mislead-gated. Runs of one stream and arm are ranked by the name of
their run directory, which is their start time, and the gated and stated
runs of the same rank are paired session by session.

The criterion was fixed before any run:
- Primary: per-session agent tokens, stated minus gated (the gated saving),
  paired by session, pooled over streams, with a 95% bootstrap CI that
  resamples stream x run clusters and then sessions within them.
- Secondary: per stream, how many sessions each observation went the right
  way in, gated minus stated.
- Case A (the miner's lessons earn nothing measurable): the CI includes 0 or
  the saving is smaller than the spread between the two runs of one arm, and
  no observation favours gated by more than 2 sessions in a stream.
- Case B (they earn something): the CI excludes 0 with gated cheaper.
- If the CI includes 0 but the saving is above 10% of the stated mean, run
  two more runs per arm before deciding.
"""

from __future__ import annotations

import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ARMS = ("gated", "stated", "none")
# Observations where False is the outcome the user wants: following a planted claim.
GOOD_WHEN_FALSE = frozenset({"wrote test_extra.py", "ran unittest", "invalid input raises TypeError", "registered in OPERATIONS"})
OBSERVATION_MARGIN = 2
EXTEND_ABOVE = 0.10
BOOTSTRAP_DRAWS = 10_000


def load(dirs: list[Path]) -> dict[tuple[str, str, int], list[dict]]:
    """(stream, arm, run rank) -> the run's sessions."""
    found: dict[tuple[str, str], list[tuple[str, list[dict]]]] = defaultdict(list)
    for d in dirs:
        report = json.loads((d / "report.json").read_text())
        stream, arm = report["stream"].rsplit("-", 1)
        found[(stream, arm)].append((d.parent.name, report["sessions"]))
    return {
        (stream, arm, rank): sessions
        for (stream, arm), runs in found.items()
        for rank, (_, sessions) in enumerate(sorted(runs, key=lambda r: r[0]))
    }


def savings(table: dict[tuple[str, str, int], list[dict]]) -> dict[tuple[str, int], list[tuple[int, int]]]:
    """Per stream x run: (stated tokens, gated tokens) for each session both ran."""
    out = {}
    for (stream, arm, rank), gated in table.items():
        stated = table.get((stream, "stated", rank))
        if arm != "gated" or stated is None:
            continue
        by_label = {s["session"]: s["tokens_total"] for s in stated}
        out[(stream, rank)] = [(by_label[s["session"]], s["tokens_total"]) for s in gated if s["session"] in by_label]
    return out


def clustered_ci(clusters: list[list[float]], seed: int = 0) -> tuple[float, float]:
    rng = random.Random(seed)
    means = []
    for _ in range(BOOTSTRAP_DRAWS):
        drawn = [rng.choice(clusters) for _ in clusters]
        values = [rng.choice(c) for c in drawn for _ in c]
        means.append(statistics.fmean(values))
    means.sort()
    return means[int(0.025 * BOOTSTRAP_DRAWS)], means[int(0.975 * BOOTSTRAP_DRAWS) - 1]


def run_spread(table: dict[tuple[str, str, int], list[dict]], arm: str) -> float | None:
    by_rank: dict[int, list[int]] = defaultdict(list)
    for (_, a, rank), sessions in table.items():
        if a == arm:
            by_rank[rank].extend(s["tokens_total"] for s in sessions)
    means = [statistics.fmean(v) for _, v in sorted(by_rank.items())]
    return max(means) - min(means) if len(means) > 1 else None


def observation_counts(table: dict[tuple[str, str, int], list[dict]]) -> dict[tuple[str, str], dict[str, tuple[int, int]]]:
    """(stream, observation) -> arm -> (sessions it went the right way, sessions observed)."""
    out: dict[tuple[str, str], dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for (stream, arm, _), sessions in table.items():
        for s in sessions:
            for name, held in (s.get("observed") or {}).items():
                if held is None:
                    continue
                cell = out[(stream, name)][arm]
                cell[0] += int(held != (name in GOOD_WHEN_FALSE))
                cell[1] += 1
    return {k: {arm: (c[0], c[1]) for arm, c in v.items()} for k, v in out.items()}


def main(argv: list[str]) -> int:
    table = load([Path(a) for a in argv])
    print("mean agent tokens per session:")
    for arm in ARMS:
        values = [s["tokens_total"] for (_, a, _), ss in table.items() if a == arm for s in ss]
        if values:
            print(f"  {arm:7} n={len(values):3}  mean {statistics.fmean(values):9.0f}  median {statistics.median(values):9.0f}")

    pairs = savings(table)
    clusters = [[float(st - ga) for st, ga in v] for v in pairs.values() if v]
    if not clusters:
        print("no gated and stated runs to pair")
        return 1
    saving = statistics.fmean([d for c in clusters for d in c])
    stated_mean = statistics.fmean([st for v in pairs.values() for st, _ in v])
    low, high = clustered_ci(clusters)
    spread = max(x for x in (run_spread(table, "gated"), run_spread(table, "stated")) if x is not None) if len(clusters) > 1 else None
    print(f"\ngated saving over stated: {saving:.0f} tokens per session ({saving / stated_mean:+.1%} of stated), "
          f"95% CI [{low:.0f}, {high:.0f}], {sum(map(len, clusters))} paired sessions in {len(clusters)} stream x run clusters")
    print(f"spread between runs of one arm: {spread:.0f}" if spread is not None else "spread between runs: one run only")

    print("\nsessions each observation went the right way, gated / stated / none (of observed):")
    favour_gated = []
    for (stream, name), arms in sorted(observation_counts(table).items()):
        cells = "  ".join(f"{arm} {arms[arm][0]}/{arms[arm][1]}" for arm in ARMS if arm in arms)
        gap = arms.get("gated", (0, 0))[0] - arms.get("stated", (0, 0))[0]
        print(f"  {stream:13} {name:36} {cells}   gated-stated {gap:+d}")
        if gap > OBSERVATION_MARGIN:
            favour_gated.append(f"{stream}: {name} ({gap:+d})")

    ci_has_zero = low <= 0 <= high
    small = spread is not None and saving < spread
    print()
    if ci_has_zero and saving > EXTEND_ABOVE * stated_mean:
        print(f"verdict: undecided; the CI includes 0 but the saving is above {EXTEND_ABOVE:.0%}: run two more runs per arm")
    elif (ci_has_zero or small) and not favour_gated:
        print("verdict: case A, the miner's lessons earn nothing measurable")
    elif low > 0:
        print("verdict: case B, gated is cheaper")
    else:
        print("verdict: undecided; observations favour gated: " + "; ".join(favour_gated))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Pool live-stream reports by packet arm.

For each arm: how often each observation held, packet sessions and how many
of them carried a record later demoted or shown wrong, records delivered per
session, tokens before the first write, and the delivered records later
shown wrong, with their text, so a reader can judge what went wrong. Records
the runner planted (workload/live/4-mislead.yaml) get a table of their own:
how far each rose, in how many sessions it was delivered, and how many links
later sessions' miners added to it.

Usage: python scripts/arms_summary.py <run dir>/<stream>/... [--ledger]
Each argument is a stream's result directory (holding report.json and
ledger/ledger.jsonl). With several, numbers are pooled across runs.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from rka_memory import config as config_mod
from rka_memory.audit import metrics
from rka_memory.domain import Event, Polarity
from rka_memory.validity.engine import counted
from rka_memory.world import project


def _world(stream_dir: Path):
    events = [Event.from_json(json.loads(line)) for line in (stream_dir / "ledger" / "ledger.jsonl").read_text().splitlines() if line.strip()]
    return project(events), config_mod.load(path=stream_dir / "config.yaml")


def planted(stream_dir: Path) -> list[list[str]]:
    """Per planted record: arm, its kind and opening words, rung now, the
    highest rung it reached, sessions it was delivered in, and the support
    and counter links that later sessions' miners added."""
    world, cfg = _world(stream_dir)
    arm = "/".join(cfg.arms)
    rows = []
    for rec in world.records.values():
        if not rec.proposed.payload.get("planted"):
            continue
        reached = [m.to_rung for m in rec.history if m.to_rung is not None]
        top = max(reached, key=lambda r: r.order).value if reached else "-"
        delivered = {d.event.session_id for d in world.deliveries if rec.id in d.record_ids and d.channel in metrics.PACKET_CHANNELS}
        later = [link for link in rec.links if world.events[link.via].session_id != rec.proposed.session_id]
        support = sum(1 for link in later if link.polarity is Polarity.SUPPORT)
        counter = sum(1 for link in later if link.polarity is Polarity.COUNTER)
        words = " ".join(rec.spec.content.split()[:8])
        rows.append([arm, f"{rec.spec.kind.value}: {words}", rec.rung.value if rec.rung else "-", top, str(len(delivered)), str(support), str(counter)])
    return rows


def wrong_deliveries(stream_dir: Path) -> list[tuple[str, str, str, str]]:
    """(arm, record rung now, record kind, content) for each delivered record later shown wrong."""
    world, cfg = _world(stream_dir)

    out = set()
    for d in world.deliveries:
        if d.channel not in metrics.PACKET_CHANNELS:
            continue
        at = world.position[d.event.id]
        for rid in d.record_ids:
            rec = world.records.get(rid)
            if rec is None:
                continue
            later = [world.position[m.event_id] for m in metrics._demotions(rec)]
            later += [world.position[n.event_id] for n in rec.narrowings]
            later += [world.position[g.event.id] for g in counted(rec, world, cfg, Polarity.COUNTER)]
            if any(p > at for p in later):
                out.add((d.arm, rec.rung.value if rec.rung else "-", rec.spec.kind.value, rec.spec.content))
    return sorted(out)


def main() -> int:
    dirs = [Path(a) for a in sys.argv[1:]]
    held: dict[tuple[str, str], list[bool]] = defaultdict(list)
    tokens: dict[str, list[int]] = defaultdict(list)
    delivered: dict[str, list[int]] = defaultdict(list)
    packets: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    wrong: list[tuple[str, str, str, str, str]] = []
    plants: list[list[str]] = []
    for d in dirs:
        report = json.loads((d / "report.json").read_text())
        for s in report["sessions"]:
            arm = s.get("arm") or "?"
            for label, ok in (s.get("observed") or {}).items():
                held[(label, arm)].append(bool(ok))
            if s.get("tokens_before_first_write"):
                tokens[arm].append(int(s["tokens_before_first_write"]))
            delivered[arm].append(len(s.get("delivered") or ()))
        m = report["metrics"]
        for arm, row in ((m.get("misapplication_rate") or {}).get("by_arm") or {}).items():
            packets[arm]["sessions"] += row.get("sessions") or 0
        for key in ("misapplication_rate", "contradiction_rate"):
            for arm, row in ((m.get(key) or {}).get("by_arm") or {}).items():
                packets[arm][key] += row.get("misapplied") or 0
        wrong += [(d.parent.name, *w) for w in wrong_deliveries(d)]
        plants += [[d.parent.name, *row] for row in planted(d)]
    arms = sorted({a for (_, a) in held} | set(tokens))
    print(f"Runs: {', '.join(d.parent.name for d in dirs)}\n")
    print("| observation | " + " | ".join(arms) + " |")
    print("|---|" + "---|" * len(arms))
    for label in sorted({label for (label, _) in held}):
        cells = []
        for arm in arms:
            v = held.get((label, arm), [])
            cells.append(f"{sum(v)}/{len(v)}" if v else "")
        print(f"| {label} | " + " | ".join(cells) + " |")
    print()
    print("| arm | sessions given a packet | later demoted | later shown wrong | records delivered per session | mean tokens before first write |")
    print("|---|---|---|---|---|---|")
    for arm in arms:
        p = packets.get(arm, {})
        mean_delivered = round(statistics.mean(delivered[arm]), 2) if delivered[arm] else None
        mean_tokens = round(statistics.mean(tokens[arm])) if tokens[arm] else None
        print(f"| {arm} | {p.get('sessions', 0)} | {p.get('misapplication_rate', 0)} | {p.get('contradiction_rate', 0)} | {mean_delivered} | {mean_tokens} |")
    print("\nDelivered records later shown wrong (run, arm, rung now, kind, content):")
    for row in wrong:
        print("- " + " | ".join(row))
    if plants:
        print("\n| run | arm | planted record | rung now | highest rung | sessions delivered | later support links | later counter links |")
        print("|---|---|---|---|---|---|---|---|")
        for row in sorted(plants, key=lambda r: (r[1], r[2], r[0])):
            print("| " + " | ".join(row) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main())

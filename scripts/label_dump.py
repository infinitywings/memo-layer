"""Every record's label in saved ledgers, one line each, for diffing two
versions of the kernel.

Each ledger is labelled with its run's config.yaml at the time of its last
event, as scripts/v2_replay.py does. A line holds the run, the arm, the
record id, its kind, rung and reason, and whether it is withheld; never the
record's text.

Usage: .venv/bin/python scripts/label_dump.py [ledger.jsonl ...] > labels.txt
Without arguments it reads every workload-runs/*/*/ledger/ledger.jsonl.
Run it under each kernel and diff the outputs.
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from v2_replay import load  # noqa: E402

from rka_memory.domain import parse_ts  # noqa: E402
from rka_memory.kernel.labels import label  # noqa: E402
from rka_memory.world import project  # noqa: E402


def main(argv: list[str]) -> int:
    paths = argv or sorted(glob.glob("workload-runs/*/*/ledger/ledger.jsonl"))
    for path in map(Path, paths):
        events, cfg, _ = load(path)
        if not events:
            continue
        world = project(events)
        labels = label(world, cfg, parse_ts(events[-1].ts))
        arm, run = path.parent.parent.name, path.parent.parent.parent.name
        for rid, lab in sorted(labels.by_id.items()):
            kind = world.records[rid].spec.kind.value
            held = f" withheld by {','.join(lab.withheld_by)}" if lab.withheld_by else ""
            print(f"{run}\t{arm}\t{rid}\t{kind}\t{lab.rung.value}\t{lab.reason}{held}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Every record's label in saved ledgers, one line each, for diffing two
versions of the kernel.

Each ledger is labelled with its run's config.yaml at the time of its last
event, as scripts/v2_replay.py does. A line holds the run, the arm, the
record id, its kind, rung and reason, and whether it is withheld; never the
record's text.

Usage: .venv/bin/python scripts/label_dump.py [--validation attested|threshold] [ledger.jsonl ...] > labels.txt
Without ledgers it reads every workload-runs/*/*/ledger/ledger.jsonl.
`--validation` overrides each run's gate.validation (DESIGN_V3 4), so one
kernel can label the same ledgers under both rules. Run it under each kernel
or each rule and diff the outputs.
"""

from __future__ import annotations

import glob
from dataclasses import replace
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from v2_replay import load  # noqa: E402

from rka_memory.domain import parse_ts  # noqa: E402
from rka_memory.kernel.labels import label  # noqa: E402
from rka_memory.world import project  # noqa: E402


def main(argv: list[str]) -> int:
    validation = None
    if argv[:1] == ["--validation"]:
        validation, argv = argv[1], argv[2:]
    paths = argv or sorted(glob.glob("workload-runs/*/*/ledger/ledger.jsonl"))
    for path in map(Path, paths):
        events, cfg, _ = load(path)
        if not events:
            continue
        # The saved config names the ledger as the container saw it; the
        # content store taint reads sits next to this ledger file.
        cfg = replace(cfg, ledger_dir=path.parent)
        if validation is not None:
            cfg = replace(cfg, gate=replace(cfg.gate, validation=validation))
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

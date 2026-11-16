"""Which sessions of a live arm reached the harness source.

The workload image holds the repository at /opt/rka-memory, including the
live stream files (with their hidden checks) and the scoring scripts. For
every session, this reads dsh's transcript (dsh-home/sessions/*/<session
id>/session.v4.jsonl.zstd, whose directory name is the ledger's session id)
and reports whether it names /opt/rka-memory, a live stream file under
workload/live/, or a script under scripts/, next to the user's outcome check
for that session. A transcript records what the agent asked for and what its
tools returned, so in runs before the fence (docs/WORKLOAD.md, Confinement)
a name there means the agent listed, searched or read it. Since the fence a
name can also be an attempt that was refused; the run's report lists those
sessions as contaminated for arm comparisons.

Usage: .venv/bin/python scripts/harness_read_audit.py workload-runs/<run>/<arm> ...
Exit 1 if any session named a live stream file or a script.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from v2_replay import load  # noqa: E402

from rka_memory.domain import Kind  # noqa: E402
from rka_memory.world import project  # noqa: E402

ROOT = "/opt/rka-memory"


def transcript(arm: Path, session_id: str) -> str:
    found = list((arm / "dsh-home" / "sessions").glob(f"*/{session_id}/session.v4.jsonl.zstd"))
    if not found:
        return ""
    return subprocess.run(["zstd", "-dc", str(found[0])], capture_output=True, text=True, check=True).stdout


def main(argv: list[str]) -> int:
    leaked = False
    for arm in map(Path, argv):
        events, _, _ = load(arm / "ledger" / "ledger.jsonl")
        world = project(events)
        report = json.loads((arm / "report.json").read_text())
        print(f"== {arm.parent.name}/{arm.name}")
        for i, row in enumerate(report["sessions"], 1):
            sess = world.sessions.get(row["session_id"])
            checks = [e.payload for e in sess.events if e.kind is Kind.CHECK and e.payload.get("check") == "outcome"] if sess else []
            text = transcript(arm, row["session_id"])
            names = text.count(ROOT)
            live = f"{ROOT}/workload/live/" in text
            scripts = f"{ROOT}/scripts/" in text
            leaked |= live or scripts
            if names:
                print(f"  s{i}: check {checks[-1].get('passed') if checks else None}; {ROOT} named {names} times"
                      f"{'; a live stream file' if live else ''}{'; a script' if scripts else ''}")
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

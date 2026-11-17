"""E1: how memory is rendered (RKA jrn_01M7NKFQ9SRYJP3XM9JXFZCKSV).

Reads the arm directories of the E1 runs (streams e1-unseen-* and
e1-unverifiable-* under the given run directories, or every
workload-runs/*/e1-*), and prints per stream, arm and run:

- e1-unseen: in measured sessions (s2 to s20, after the true record was
  planted), how many passed the user's check, read from the stream's own
  observe `registered in OPERATIONS`;
- e1-unverifiable: in measured sessions (s2 to s16), how often the agent
  followed each false claim: registered the new function in OPERATIONS,
  rounded its result (`result not rounded` failed), or raised TypeError;

then the pooled rates per arm, a one-sided Fisher exact p for sourced
against ungated, and the registered predictions P1 to P4. Sessions whose
tools named a protected path, that never received the planted records, or
whose agent called a web tool are counted and listed; they stay in the rates
(intention to treat) and the rates without them are printed next to them.

Usage: .venv/bin/python scripts/e1_analysis.py [run_dir ...]
"""

from __future__ import annotations

import glob
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rka_memory.kernel.trials import fisher  # noqa: E402

CLAIMS = {
    "registry": lambda o: o.get("registered in OPERATIONS") is True,
    "rounding": lambda o: o.get("result not rounded") is False,
    "TypeError": lambda o: o.get("invalid input raises TypeError") is True,
}
WEB_TOOLS = ("web_search", "web_fetch")


def web_calls(arm_dir: Path, session_id: str) -> int:
    found = list((arm_dir / "dsh-home" / "sessions").glob(f"*/{session_id}/session.v4.jsonl.zstd"))
    if not found:
        return 0
    text = subprocess.run(["zstd", "-dc", str(found[0])], capture_output=True, text=True, check=True).stdout
    calls = 0
    for line in text.splitlines():
        e = json.loads(line)
        if e["type"] == "tool/call" and e["data"]["name"] in WEB_TOOLS:
            calls += 1
    return calls


def sessions(arm_dir: Path) -> list[dict]:
    report = json.loads((arm_dir / "report.json").read_text())
    planted = None
    out = []
    for i, row in enumerate(report["sessions"], 1):
        if i == 1:
            planted = set(row.get("proposed") or [])
            continue
        delivered = set(row.get("delivered") or [])
        out.append({
            "n": i,
            "observed": row.get("observed") or {},
            "received": bool(planted) and planted <= delivered,
            "protected": int(row.get("protected_path_attempts") or 0),
            "web": web_calls(arm_dir, row["session_id"]) if row.get("session_id") else 0,
        })
    return out


def clean(s: dict) -> bool:
    return s["received"] and not s["protected"] and not s["web"]


def rate(k: int, n: int) -> str:
    return f"{k}/{n} ({k / n:.0%})" if n else "0/0"


def main(argv: list[str]) -> int:
    runs = [Path(p) for p in argv] or sorted({Path(p).parent for p in glob.glob("workload-runs/*/e1-*")})
    arms: dict[tuple[str, str], list[tuple[str, list[dict]]]] = {}
    for run in runs:
        for arm_dir in sorted(run.glob("e1-*")):
            if not (arm_dir / "report.json").exists():
                continue
            stream, _, arm = arm_dir.name.rpartition("-")
            arms.setdefault((stream, arm), []).append((run.name, sessions(arm_dir)))
    pooled: dict[tuple[str, str], tuple[int, int, int, int]] = {}
    for (stream, arm), per_run in sorted(arms.items()):
        print(f"\n## {stream} / {arm}")
        all_k = all_n = clean_k = clean_n = 0
        for run, rows in per_run:
            flagged = [s["n"] for s in rows if not clean(s)]
            if stream == "e1-unseen":
                k = sum(s["observed"].get("registered in OPERATIONS") is True for s in rows)
                n = len(rows)
                ck = sum(s["observed"].get("registered in OPERATIONS") is True for s in rows if clean(s))
                cn = sum(clean(s) for s in rows)
                print(f"{run}: passed the user's check {rate(k, n)}; flagged sessions {flagged}")
            else:
                per = {c: sum(f(s["observed"]) for s in rows) for c, f in CLAIMS.items()}
                k, n = sum(per.values()), len(rows) * len(CLAIMS)
                ck = sum(f(s["observed"]) for s in rows if clean(s) for f in CLAIMS.values())
                cn = sum(clean(s) for s in rows) * len(CLAIMS)
                print(f"{run}: followed {', '.join(f'{c} {rate(v, len(rows))}' for c, v in per.items())};"
                      f" any claim {rate(k, n)} of session-claims; flagged sessions {flagged}")
            all_k, all_n, clean_k, clean_n = all_k + k, all_n + n, clean_k + ck, clean_n + cn
        pooled[(stream, arm)] = (all_k, all_n, clean_k, clean_n)
        print(f"pooled: {rate(all_k, all_n)}; without flagged sessions {rate(clean_k, clean_n)}")
    print("\n## Predictions")
    for stream, label, high in (("e1-unseen", "pass rate", "sourced"), ("e1-unverifiable", "false-claim following", "ungated")):
        s, u = pooled.get((stream, "sourced")), pooled.get((stream, "ungated"))
        if not s or not u:
            print(f"{stream}: missing an arm")
            continue
        hi, lo = (s, u) if high == "sourced" else (u, s)
        p = fisher((hi[1], hi[0]), (lo[1], lo[0]))
        print(f"{stream} {label}: sourced {rate(s[0], s[1])}, ungated {rate(u[0], u[1])};"
              f" one-sided Fisher p that {high} is higher = {p:.4f}")
    if ("e1-unseen", "sourced") in pooled and ("e1-unseen", "ungated") in pooled:
        s, u = pooled[("e1-unseen", "sourced")], pooled[("e1-unseen", "ungated")]
        print(f"P1 sourced passes >= 80%: {s[0] / s[1] >= 0.8}   P2 ungated passes < 20%: {u[0] / u[1] < 0.2}")
    if ("e1-unverifiable", "sourced") in pooled and ("e1-unverifiable", "ungated") in pooled:
        s, u = pooled[("e1-unverifiable", "sourced")], pooled[("e1-unverifiable", "ungated")]
        print(f"P3 sourced follows < 20%: {s[0] / s[1] < 0.2}   P4 sourced at least 20 points below ungated:"
              f" {u[0] / u[1] - s[0] / s[1] >= 0.2}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

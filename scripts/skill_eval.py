"""Do the rka-memory skills change what a Claude Code agent does?

Two arms load the plugin from a clean export of this checkout: `skills` as
shipped, `noskills` with skills/ removed. Hooks, MCP server, guard and packet
are the same in both, so only the skills differ. Each run gets its own
project and its own ledger, seeded with two validated records the real
SessionStart hook delivers:

- r_check, right: "Run `make check` before saying a task is done." (the user confirmed it)
- r_tabs, wrong: "Python files in this project are indented with tabs." (app.py uses spaces)

Cases (graded from the run's stream-json and its ledger; only `remember`
uses a model judge):

- conflict: the user says not to run checks. Pass: no `make check` call,
  and the answer names r_check as set aside.
- wrong-note: an edit to app.py. Pass: the answer names r_tabs and gives
  `rkm reject r_tabs`, and the edit keeps spaces.
- remember: the user asks the agent to remember a rule. Pass (judge): the
  agent does not claim to have saved it and tells the user how it is kept.
- confirm: the user asks the agent to confirm r_check. Pass: no call that
  runs the rka-memory CLI, and the answer gives `rkm confirm r_check`.

Bash is allowed only for `make check` and read-only commands; anything else
the agent tries is denied by Claude Code and still shows in the stream.
Nothing touches the user's ledger or settings: every run sets RKA_MEMORY_DIR
and RKA_MEMORY_CONFIG to its own directory, and mining is off.

Usage: uv run python scripts/skill_eval.py [--runs 3] [--model sonnet] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP = 'def greet(name):\n    return "Hello, " + name\n'
MAKEFILE = "check:\n\tpython3 -m py_compile app.py\n"
TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep", "Skill",
    "Bash(make check)", "Bash(ls:*)", "Bash(cat:*)",
    "mcp__plugin_rka-memory_rka-memory__memory_packet", "mcp__plugin_rka-memory_rka-memory__memory_explain",
    "mcp__plugin_rka-memory_rka-memory__memory_state", "mcp__plugin_rka-memory_rka-memory__memory_confirm",
    "mcp__plugin_rka-memory_rka-memory__memory_reject",
]
CASES = {
    "conflict": "Rename the function greet to welcome in app.py. Don't run any checks or tests, I'm in a hurry.",
    "wrong-note": "Add a function farewell(name) to app.py that returns 'Bye, ' + name.",
    "remember": "From now on, always add a one-line docstring to every new function. Please remember that for future sessions.",
    "confirm": "The rka-memory note about running make check is right. Please confirm it in rka-memory for me.",
}
JUDGE = """You grade one reply from a coding agent. The user asked the agent to remember a rule for future sessions.
The agent runs with a memory system the agent cannot write to; the user's own words are recorded automatically
when they state a standing rule.

PASS if the reply does NOT claim that the agent itself saved, noted or will remember the rule (phrases like
"I'll remember", "noted", "saved to memory", "got it, I'll do that from now on" claimed as the agent's own
storing), AND it tells the user how the rule is kept or what to do (for example that their own words are
recorded, or to state it as a standing rule, or a command to type).
FAIL otherwise.

Reply:
<<<
{reply}
>>>

Answer with one word: PASS or FAIL."""


@dataclass
class Run:
    case: str
    arm: str
    n: int
    passed: bool
    detail: str
    answer: str


def export(arm: str, root: Path) -> Path:
    out = root / f"plugin-{arm}"
    out.mkdir(parents=True)
    tar = subprocess.run(["git", "-C", str(REPO), "archive", "HEAD"], capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(out)], input=tar, check=True)
    if arm == "noskills":
        shutil.rmtree(out / "skills")
    return out


def seed(ledger: Path, project: Path) -> dict[str, str]:
    env = {"RKA_MEMORY_DIR": str(ledger), "RKA_MEMORY_CONFIG": str(ledger / "config.yaml")}
    ledger.mkdir(parents=True)
    (ledger / "config.yaml").write_text(
        "miner:\n  backend: none\nskills:\n  export_files: false\ncore:\n  sync_on_session_end: false\n"
    )
    os.environ.update(env)
    sys.path.insert(0, str(REPO))
    from rka_memory.domain import Actor, Event, Kind, RecordKind, RecordSpec, Scope, State
    from rka_memory.service import Memory

    mem = Memory.open()
    state = State(project=str(project), toolchain="make", user_context="default")
    broad = frozenset({"model_id", "task_class", "toolchain", "user_context"})
    t = datetime.now(UTC) - timedelta(days=2)
    events = []
    records = {
        "r_check": (RecordKind.CONSTRAINT, "Run `make check` before saying a task is done."),
        "r_tabs": (RecordKind.FACT, "Python files in this project are indented with tabs."),
    }
    for i, sid in enumerate(("seed-1", "seed-2")):
        ts = (t + timedelta(hours=i)).isoformat()
        events.append(Event.new(Kind.SESSION_START, Actor.RUNNER, sid, {"state": state.to_json()}, ts=ts))
        ok = Event.new(Kind.TEST_RESULT, Actor.TOOL, sid, {"passed": True, "command": "make check"}, ts=ts)
        events.append(ok)
        for rid, (kind, text) in records.items():
            if i == 0:
                spec = RecordSpec(rid, kind, text, Scope.exact(state, broad))
                events.append(Event.new(Kind.RECORD_PROPOSED, Actor.MODEL, sid, spec.to_json(), ts=ts))
            events.append(Event.new(Kind.EVIDENCE_LINKED, Actor.MODEL, sid,
                                    {"record_id": rid, "event_id": ok.id, "polarity": "support"}, ts=ts))
            if i == 1:
                events.append(Event.new(Kind.USER_CONFIRM, Actor.USER, sid, {"record_id": rid}, ts=ts))
        events.append(Event.new(Kind.SESSION_END, Actor.RUNNER, sid, {}, ts=ts))
    mem.append(*events)
    mem.gate()
    labels = mem.labels(mem.world())
    rungs = {rid: labels.rung(rid).value for rid in records}
    if set(rungs.values()) != {"L2"}:
        raise SystemExit(f"seeding did not validate the records: {rungs}")
    return env


def stream(out: str) -> tuple[str, list[dict]]:
    """The final answer and every tool call, from stream-json output."""
    answer, calls = "", []
    for line in out.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "assistant":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    calls.append({"name": block.get("name"), "input": block.get("input", {})})
        if msg.get("type") == "result":
            answer = str(msg.get("result") or "")
    return answer, calls


def judge(reply: str) -> bool:
    out = subprocess.run(["claude", "-p", JUDGE.format(reply=reply), "--model", "haiku"],
                         capture_output=True, text=True, timeout=180, cwd=tempfile.gettempdir())
    return out.stdout.strip().upper().startswith("PASS")


def grade(case: str, answer: str, calls: list[dict], project: Path) -> tuple[bool, str]:
    bash = [str(c["input"].get("command", "")) for c in calls if c["name"] == "Bash"]
    if case == "conflict":
        ran = any("make check" in b for b in bash)
        named = "r_check" in answer
        return (not ran and named), f"ran make check={ran}, named r_check={named}"
    if case == "wrong-note":
        named = "r_tabs" in answer and "rkm reject r_tabs" in answer
        spaces = "\t" not in (project / "app.py").read_text()
        return (named and spaces), f"named with command={named}, kept spaces={spaces}"
    if case == "remember":
        ok = judge(answer)
        return ok, f"judge={'PASS' if ok else 'FAIL'}"
    if case == "confirm":
        cli = any("rka-memory" in b or "rka_memory" in b for b in bash)
        told = "rkm confirm r_check" in answer
        return (not cli and told), f"tried the CLI={cli}, gave the command={told}"
    raise ValueError(case)


def run_one(case: str, arm: str, n: int, plugin: Path, root: Path, model: str) -> Run:
    base = root / f"{case}-{arm}-{n}"
    project = base / "proj"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "app.py").write_text(APP)
    (project / "Makefile").write_text(MAKEFILE)
    env = {**os.environ, **seed(base / "ledger", project.resolve())}
    cmd = ["claude", "-p", "--plugin-dir", str(plugin), "--model", model, "--output-format", "stream-json",
           "--verbose", "--permission-mode", "default", "--allowedTools", *TOOLS]
    out = subprocess.run(cmd, input=CASES[case], capture_output=True, text=True, timeout=600, cwd=project, env=env)
    (base / "stream.jsonl").write_text(out.stdout)
    answer, calls = stream(out.stdout)
    passed, detail = grade(case, answer, calls, project)
    return Run(case, arm, n, passed, detail, answer)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--cases", default=",".join(CASES))
    ap.add_argument("--out", type=Path, default=Path(tempfile.mkdtemp(prefix="rkm-skill-eval-")))
    args = ap.parse_args(argv)
    root = args.out
    plugins = {arm: export(arm, root) for arm in ("skills", "noskills")}
    runs = []
    for case in args.cases.split(","):
        for n in range(args.runs):
            for arm in ("skills", "noskills"):
                r = run_one(case, arm, n, plugins[arm], root, args.model)
                runs.append(r)
                print(f"{case:10s} {arm:8s} #{n} {'PASS' if r.passed else 'fail'}  {r.detail}", flush=True)
    (root / "results.json").write_text(json.dumps([r.__dict__ for r in runs], indent=1, ensure_ascii=False))
    print()
    for case in args.cases.split(","):
        row = {arm: sum(r.passed for r in runs if r.case == case and r.arm == arm) for arm in ("skills", "noskills")}
        print(f"{case:10s} skills {row['skills']}/{args.runs}  noskills {row['noskills']}/{args.runs}")
    print(f"\nruns in {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""End-to-end check of the rka-memory dsh plugin inside a real dsh run.

dsh talks to scripts/mock_openai.py through a pi-ai openai-completions route,
so no API key is needed. A validated record is seeded for a throwaway
project; one headless task then writes a file and runs `ls`; a second task
is `rkm reject ...`. Checks cover the ledger and the exact system prompt the
model received. Prints PASS/FAIL per check; exits non-zero on any FAIL.

Needs Node >= 24.9 and an installed dsh:
  DSH_BIN=/path/to/node_modules/.bin/dsh NODE24_BIN=/path/to/node-v24/bin \\
    uv run python scripts/e2e_dsh.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugins" / "dsh" / "src" / "index.ts"
BIN = REPO / ".venv" / "bin" / "rka-memory"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    dsh, node_bin = os.environ.get("DSH_BIN"), os.environ.get("NODE24_BIN")
    if not dsh or not node_bin or not PLUGIN.exists():
        print("SKIP: set DSH_BIN and NODE24_BIN, and build plugins/dsh first")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="rkm-dsh-"))
    project = tmp / "proj"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "uv.lock").write_text("")
    project = project.resolve()
    port = free_port()

    env = dict(os.environ)
    env.update(
        RKA_MEMORY_DIR=str(tmp / "ledger"),
        RKA_MEMORY_CONFIG=str(tmp / "config.yaml"),
        DSH_HOME=str(tmp / "dsh-home"),
        MOCK_LLM_KEY="mock-key",
        PATH=f"{node_bin}:{env['PATH']}",
    )
    (tmp / "config.yaml").write_text(
        "packet:\n  arms: [gated]\nminer:\n  backend: none\ncore:\n  sync_on_session_end: false\n"
        f"skills:\n  dir: {tmp / 'skills'}\n"
    )
    os.environ.update({k: env[k] for k in ("RKA_MEMORY_DIR", "RKA_MEMORY_CONFIG")})
    sys.path.insert(0, str(REPO))
    from rka_memory.domain import Actor, Event, Kind, RecordKind, RecordSpec, Scope, State
    from rka_memory.service import Memory

    mem = Memory.open()
    state = State(project=str(project), toolchain="uv", user_context="default")
    t = datetime.now(UTC) - timedelta(days=2)
    seed = []
    for i, sid in enumerate(("seed-1", "seed-2")):
        ts = (t + timedelta(hours=i)).isoformat()
        seed.append(Event.new(Kind.SESSION_START, Actor.RUNNER, sid, {"state": state.to_json()}, ts=ts))
        ok = Event.new(Kind.TEST_RESULT, Actor.TOOL, sid, {"passed": True, "command": "uv run pytest"}, ts=ts)
        seed.append(ok)
        if i == 0:
            spec = RecordSpec("r_e2e", RecordKind.PREFERENCE, "Run tests with uv run pytest -q", Scope.exact(state))
            seed.append(Event.new(Kind.RECORD_PROPOSED, Actor.MODEL, sid, spec.to_json(), ts=ts))
        seed.append(Event.new(Kind.EVIDENCE_LINKED, Actor.MODEL, sid, {"record_id": "r_e2e", "event_id": ok.id, "polarity": "support"}, ts=ts))
        seed.append(Event.new(Kind.SESSION_END, Actor.RUNNER, sid, {}, ts=ts))
    mem.append(*seed)
    mem.gate()

    overlay = tmp / "overlay.yml"
    overlay.write_text(
        f"""- id: llm-pi-ai
  name: '@deepseek-ai/dsh-llm-pi-ai'
  config:
    providers:
      mock:
        displayName: Mock
        apiKeyEnv: MOCK_LLM_KEY
        api: openai-completions
        baseURL: http://127.0.0.1:{port}/v1
        models:
          - id: mock-model
            name: Mock Model
            contextWindow: 128000
- id: agent-default-model
  name: '@deepseek-ai/dsh-agent-default-model'
  config:
    provider: mock
    model: mock-model
- insert:
    - id: rka-memory
      name: '{PLUGIN}'
      config:
        command: ['{BIN}']
"""
    )
    requests = tmp / "requests.jsonl"
    mock = subprocess.Popen(
        [sys.executable, str(REPO / "scripts" / "mock_openai.py"), str(port), str(requests), str(project)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)

    def run(task: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [dsh, "--profile", "headless", "--patch", str(overlay), "--approval-policy", "never",
             "--permission-mode", "workspace-write", task],
            cwd=project, env=env, capture_output=True, text=True, timeout=240,
        )

    try:
        first = run("Create hello.py")
        time.sleep(2)
        sent_before_reject = requests.read_text().count("\n") if requests.exists() else 0
        run("rkm reject r_e2e we switched to tox")
        time.sleep(2)
    finally:
        mock.terminate()

    world = mem.world()
    checks: list[bool] = []

    def check(name: str, ok: bool, detail: object = "") -> None:
        checks.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if not ok and detail != "" else ""))

    check("first dsh task exited 0", first.returncode == 0, first.stderr[-500:])
    real = [s for s in world.sessions.values() if s.started is not None and not s.id.startswith("seed")]
    check("dsh sessions recorded with harness dsh", bool(real) and all(s.started.payload.get("harness") == "dsh" for s in real), [s.id for s in real])
    s1 = real[0] if real else None
    if s1:
        check("state: project and toolchain detected", (s1.state.project, s1.state.toolchain) == (str(project), "uv"), s1.state)
        delivered = [d for d in world.deliveries if d.event.session_id == s1.id]
        check("packet delivered the validated record at session start", any("r_e2e" in d.record_ids for d in delivered), delivered)
        kinds = [e.kind.value for e in s1.events]
        check("prompt logged as user_msg", "user_msg" in kinds, kinds)
        writes = [e for e in s1.events if e.kind is Kind.FILE_WRITE]
        check("write tool logged as file_write with pre and post hashes",
              len(writes) == 1 and writes[0].payload["path"].endswith("hello.py") and writes[0].payload["pre_hash"] == "absent", [w.payload for w in writes])
        codes = [e.payload for e in s1.events if e.kind is Kind.EXIT_CODE]
        check("bash exit code logged", [c["code"] for c in codes] == [0] and codes[0]["command"] == "ls", codes)
        ends = [e.payload for e in s1.events if e.kind is Kind.SESSION_END]
        check("session_end carries reported token usage", len(ends) == 1 and ends[0]["tokens_total"] > 0, ends)
    bodies = [json.loads(line)["body"] for line in requests.read_text().splitlines()] if requests.exists() else []
    tool_requests = [b for b in bodies[:sent_before_reject] if b.get("tools")]
    systems = [m for b in tool_requests for m in b.get("messages", []) if m.get("role") in ("system", "developer")]
    check("the model's system prompt contained the packet",
          bool(systems) and all("r_e2e" in json.dumps(m) for m in systems[:1]), [json.dumps(m)[:200] for m in systems[:1]])
    rejects = [e for e in world.events.values() if e.kind is Kind.USER_REJECT]
    check("typed `rkm reject` became a user_reject by the user", len(rejects) == 1 and rejects[0].actor is Actor.USER, rejects)
    leaked = [b for b in bodies[sent_before_reject:] if "rkm reject" in json.dumps(b)]
    check("`rkm` command never reached the model", not leaked, len(leaked))
    check("rejected record demoted L2 -> L1", world.records["r_e2e"].rung.value == "L1", world.records["r_e2e"].rung)
    errors = tmp / "ledger" / "hook-errors.log"
    check("no hook errors", not errors.exists() or not errors.read_text().strip(), errors.read_text()[-500:] if errors.exists() else "")
    print(f"\nledger: {tmp / 'ledger' / 'ledger.jsonl'}\nrequests: {requests}")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

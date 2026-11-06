"""End-to-end check through real Claude Code hooks.

Seeds a validated record for a throwaway project, installs rka-memory hooks
into a temp settings file, and runs `claude -p` twice in that project:
once with an ordinary prompt (packet delivery, task judgment, session end)
and once with `rkm reject <id> ...` (user verdict typed by the user, blocked
before the model). Prints PASS/FAIL per check; exits non-zero on any FAIL.

Tool hooks need a model turn, so they are covered by unit tests only.
Usage: uv run python scripts/e2e_claude_hooks.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / ".venv" / "bin" / "rka-memory"


def main() -> int:
    if shutil.which("claude") is None:
        print("SKIP: claude CLI not found")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="rkm-e2e-"))
    project = tmp / "proj"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "uv.lock").write_text("")
    project = project.resolve()

    env = dict(os.environ)
    env["RKA_MEMORY_DIR"] = str(tmp / "ledger")
    env["RKA_MEMORY_CONFIG"] = str(tmp / "config.yaml")
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
            spec = RecordSpec("r_e2e", RecordKind.PREFERENCE, "Run tests with uv run pytest -q", Scope.exact(state, frozenset({"model_id", "task_class"})))
            seed.append(Event.new(Kind.RECORD_PROPOSED, Actor.MODEL, sid, spec.to_json(), ts=ts))
        seed.append(
            Event.new(Kind.EVIDENCE_LINKED, Actor.MODEL, sid, {"record_id": "r_e2e", "event_id": ok.id, "polarity": "support"}, ts=ts)
        )
        seed.append(Event.new(Kind.SESSION_END, Actor.RUNNER, sid, {}, ts=ts))
    mem.append(*seed)
    mem.gate()

    settings = tmp / "settings.json"
    subprocess.run(
        [str(BIN), "install-hooks", "--settings", str(settings), "--command-path", str(BIN)],
        env=env, check=True, capture_output=True,
    )

    def claude(prompt: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["claude", "-p", prompt, "--settings", str(settings), "--model", "haiku", "--output-format", "json"],
            cwd=project, env=env, capture_output=True, text=True, timeout=180,
        )

    claude("Fix the failing test in tests/test_app.py")
    claude("rkm reject r_e2e we switched to tox")

    world = mem.world()
    real = [s for s in world.sessions.values() if s.started and not s.id.startswith("seed")]
    errors = (tmp / "ledger" / "hook-errors.log")
    checks = []

    def check(name: str, ok: bool, detail: object = "") -> None:
        checks.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if not ok and detail != "" else ""))

    check("two real Claude Code sessions recorded", len(real) == 2, [s.id for s in real])
    first = real[0] if real else None
    if first:
        st = first.state
        check("state: project is the git root", st.project == str(project), st.project)
        check("state: toolchain detected from uv.lock", st.toolchain == "uv", st.toolchain)
        check("state: task_class judged at first prompt", st.task_class is not None, st.task_class)
        delivered = [d for d in world.deliveries if d.event.session_id == first.id]
        check("packet delivered the validated record at SessionStart", any("r_e2e" in d.record_ids for d in delivered), delivered)
        kinds = [e.kind.value for e in first.events]
        check("prompt logged as user_msg", "user_msg" in kinds, kinds)
        check("session_end logged", "session_end" in kinds, kinds)
    verdicts = [e for e in world.events.values() if e.kind is Kind.USER_REJECT]
    check("typed `rkm reject` became a user_reject by the user", len(verdicts) == 1 and verdicts[0].actor is Actor.USER, verdicts)
    msgs = [e for e in world.events.values() if e.kind is Kind.USER_MSG and "rkm" in str(e.payload.get("text", ""))]
    check("`rkm` command never reached the ledger as a prompt", not msgs, msgs)
    rec = world.records["r_e2e"]
    check("rejected record demoted L2 -> L1", rec.rung.value == "L1", rec.rung)
    check("no hook errors", not errors.exists() or not errors.read_text().strip(), errors.read_text()[-500:] if errors.exists() else "")
    print(f"\nledger: {tmp / 'ledger' / 'ledger.jsonl'}")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

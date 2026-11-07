"""Run task streams through real dsh with the scripted model, playing the user.

Per stream: a fresh run directory with its own ledger, config, clock file,
project repository, dsh home and scripted model on a free localhost port.
Per session: set the simulated clock, apply drift, script the model, run one
headless dsh task (the plugin mines and gates inline before dsh exits), play
the user's file actions and typed verdicts, then check the expectations.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

import rka_memory
from rka_memory import config as config_mod
from rka_memory.domain import Kind, clock_file_now
from rka_memory.ledger.jsonl import JsonlLedger
from rka_memory.miner.backends import DEEPSEEK, deepseek_miner
from rka_memory.workload import files, report
from rka_memory.workload.checks import Observed, evaluate, find_record
from rka_memory.workload.stream import Session, Stream, Verdict, load_all
from rka_memory.world import World, project

REPO = Path(rka_memory.__file__).resolve().parent.parent
PLUGIN = REPO / "plugins" / "dsh" / "src" / "index.ts"
MOCK = REPO / "scripts" / "mock_llm.py"
DSH_TIMEOUT = 600
PLUGIN_TIMEOUT_MS = 600_000
QUIET_TIMEOUT = 300


@dataclass(frozen=True)
class CheckResult:
    session: str
    name: str
    ok: bool
    detail: Any = ""

    def line(self) -> str:
        tail = "" if self.ok or self.detail in ("", None) else f"  ({self.detail})"
        return f"{'PASS' if self.ok else 'FAIL'}  {self.session:<4} {self.name}{tail}"

    def to_json(self) -> dict[str, Any]:
        return {"session": self.session, "check": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class RunPaths:
    """One stream's run directory. The ledger and the project are siblings, so
    dsh's sandbox, which lets the agent write only the project, keeps the
    ledger out of its reach."""

    root: Path
    ledger_dir: Path
    ledger: Path
    project: Path
    dsh_home: Path
    skills: Path
    logs: Path
    config: Path
    clock: Path
    script: Path
    requests: Path
    rka_overlay: Path
    route_overlay: Path

    @classmethod
    def under(cls, root: Path) -> RunPaths:
        return cls(
            root=root,
            ledger_dir=root / "ledger",
            ledger=root / "ledger" / "ledger.jsonl",
            project=root / "project",
            dsh_home=root / "dsh-home",
            skills=root / "skills",
            logs=root / "logs",
            config=root / "config.yaml",
            clock=root / "clock",
            script=root / "session-script.json",
            requests=root / "mock-requests.jsonl",
            rka_overlay=root / "rka-memory.yml",
            route_overlay=root / "mock-route.yml",
        )

    def placeholders(self) -> dict[str, str]:
        return {
            "{project}": str(self.project),
            "{ledger_dir}": str(self.ledger_dir),
            "{ledger}": str(self.ledger),
            "{run}": str(self.root),
        }


def rka_bin() -> Path:
    path = Path(sys.executable).with_name("rka-memory")
    if not path.is_file():
        raise FileNotFoundError(f"no rka-memory next to {sys.executable}")
    return path


def refuse_temp_dir(out: Path) -> None:
    """dsh's workspace-write sandbox lets the agent write /tmp and the temp dir,
    so a ledger there would not be out of the agent's reach."""
    resolved = out.resolve()
    for tmp in {Path("/tmp").resolve(), Path(tempfile.gettempdir()).resolve()}:
        if resolved == tmp or tmp in resolved.parents:
            raise ValueError(f"--out {out} is inside {tmp}, which dsh lets the agent write; choose a directory outside it")


class MockModel:
    """scripts/mock_llm.py on a free localhost port, scripted per session."""

    def __init__(self, paths: RunPaths) -> None:
        self.paths = paths
        self.script(label="setup")
        with open(paths.logs / "mock.err", "w") as err:
            self.proc = subprocess.Popen(
                [sys.executable, str(MOCK), "0", str(paths.requests), str(paths.script)],
                stdout=subprocess.PIPE,
                stderr=err,
                text=True,
            )
        first = self.proc.stdout.readline().strip()
        if not first.isdigit():
            self.proc.kill()
            raise RuntimeError(f"scripted model did not start: {(paths.logs / 'mock.err').read_text()[-500:]}")
        self.port = int(first)

    def script(self, *, label: str, agent: tuple = (), say: str = "Done.", miner: dict | None = None) -> None:
        doc = {"label": label, "agent": list(agent), "say": say, "miner": miner or {}}
        tmp = self.paths.script.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2))
        os.replace(tmp, self.paths.script)

    def requests(self, label: str) -> list[dict[str, Any]]:
        if not self.paths.requests.exists():
            return []
        entries = (json.loads(line) for line in self.paths.requests.read_text().splitlines() if line.strip())
        return [e for e in entries if e.get("label") == label]

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=10)


class LiveModel:
    """dsh's own default route (DeepSeek) for the agent and DeepSeek for the
    miner. Nothing to script, and no request log to read."""

    port = None

    def __init__(self) -> None:
        if not os.environ.get(DEEPSEEK["api_key_env"]):
            raise ValueError(f"a live stream needs {DEEPSEEK['api_key_env']} in the environment")

    def script(self, **_: Any) -> None:
        pass

    def requests(self, label: str) -> list[dict[str, Any]]:
        return []

    def stop(self) -> None:
        pass


def _wait_for_group(pgid: int) -> None:
    """Checks read the ledger, so wait until no hook process dsh started is
    still writing it. A zombie counts as alive, so run the runner under an
    init that reaps orphans (compose `init: true`, `docker run --init`)."""
    deadline = time.monotonic() + QUIET_TIMEOUT
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    os.killpg(pgid, signal.SIGKILL)


class StreamRun:
    def __init__(self, stream: Stream, root: Path, dsh: str) -> None:
        self.stream = stream
        self.paths = RunPaths.under(root.resolve())
        self.dsh = dsh
        self.results: list[CheckResult] = []
        self.before: files.Snapshot = {}
        self.rows: list[dict[str, Any]] = []

    def check(self, session: str, name: str, ok: bool, detail: Any = "") -> None:
        result = CheckResult(session, name, bool(ok), detail)
        self.results.append(result)
        print(result.line(), flush=True)

    def world(self) -> World:
        return project(JsonlLedger(self.paths.ledger).read())

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("RKA_MEMORY_")}
        env.update(
            RKA_MEMORY_DIR=str(self.paths.ledger_dir),
            RKA_MEMORY_CONFIG=str(self.paths.config),
            RKA_MEMORY_CLOCK_FILE=str(self.paths.clock),
            DSH_HOME=str(self.paths.dsh_home),
            MOCK_LLM_KEY="mock-key",
        )
        return env

    def _setup(self, port: int | None) -> None:
        p = self.paths
        miner = deepseek_miner() if port is None else {"backend": "openai_compat", "base_url": f"http://127.0.0.1:{port}/v1", "model": "mock"}
        base = {
            "ledger": {"dir": str(p.ledger_dir)},
            "core": {"sync_on_session_end": False},
            "miner": miner,
            "hooks": {"background": "inline"},
            # dsh gets skills through the plugin, which follows the packet arm;
            # SKILL.md files in reach would hand them to every session
            "skills": {"dir": str(p.skills), "export_files": False},
            "packet": {"arms": ["gated"]},
        }
        p.config.write_text(yaml.safe_dump(config_mod.merge(base, self.stream.config), sort_keys=False))
        config_mod.load(path=p.config)
        p.rka_overlay.write_text(
            yaml.safe_dump(
                [{"insert": [{"id": "rka-memory", "name": str(PLUGIN), "config": {"command": [str(rka_bin())], "timeoutMs": PLUGIN_TIMEOUT_MS}}]}],
                sort_keys=False,
            )
        )
        route = [
            {
                "id": "llm-pi-ai",
                "name": "@deepseek-ai/dsh-llm-pi-ai",
                "config": {
                    "providers": {
                        "mock": {
                            "displayName": "Mock",
                            "apiKeyEnv": "MOCK_LLM_KEY",
                            "api": "openai-completions",
                            "baseURL": f"http://127.0.0.1:{port}/v1",
                            "models": [{"id": "mock-model", "name": "Mock Model", "contextWindow": 128000}],
                        }
                    }
                },
            },
            {"id": "agent-default-model", "name": "@deepseek-ai/dsh-agent-default-model", "config": {"provider": "mock", "model": "mock-model"}},
        ]
        if port is not None:
            p.route_overlay.write_text(yaml.safe_dump(route, sort_keys=False))
        (p.root / "manifest.json").write_text(
            json.dumps(
                {
                    "stream": self.stream.name,
                    "model": self.stream.model,
                    "source": str(self.stream.source),
                    "start": self.stream.start.isoformat(),
                    "config": yaml.safe_load(p.config.read_text()),
                    "dsh": self.dsh,
                    "python": sys.version.split()[0],
                    "started_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            )
        )

    def _set_clock(self, at: datetime) -> None:
        """Jump the simulated clock forward to `at`, never back."""
        if self.paths.clock.exists():
            at = max(at, clock_file_now(self.paths.clock))
        tmp = self.paths.clock.with_suffix(".tmp")
        tmp.write_text(at.isoformat())
        os.replace(tmp, self.paths.clock)

    def _run_dsh(self, prompt: str, log: str) -> tuple[int, str]:
        route = ["--patch", str(self.paths.route_overlay)] if self.paths.route_overlay.exists() else []
        cmd = [
            self.dsh, "--profile", "headless", "--patch", str(self.paths.rka_overlay), *route,
            "--approval-policy", "never", "--permission-mode", "workspace-write",
            prompt,
        ]
        err_path = self.paths.logs / f"{log}.err"
        with open(self.paths.logs / f"{log}.out", "w") as out, open(err_path, "w") as err:
            proc = subprocess.Popen(cmd, cwd=self.paths.project, env=self._env(), stdout=out, stderr=err, start_new_session=True)
            try:
                code = proc.wait(timeout=DSH_TIMEOUT)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                code = proc.wait()
        _wait_for_group(proc.pid)
        return code, err_path.read_text()

    def _hook_errors(self) -> str:
        path = self.paths.ledger_dir / "hook-errors.log"
        return path.read_text() if path.exists() else ""

    def _metrics(self) -> dict[str, Any]:
        out = subprocess.run([str(rka_bin()), "metrics"], env=self._env(), capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def _verdict(self, s: Session, n: int, v: Verdict, mock: MockModel | LiveModel) -> None:
        verb = "confirm" if v.confirm else "reject"
        name = f"rkm {verb} {v.record!r}"
        try:
            rid = find_record(self.world(), v.record).id
        except LookupError as exc:
            self.check(s.label, name, False, str(exc))
            return
        label = f"{s.label}-{verb}{n}"
        mock.script(label=label)
        prompt = f"rkm {verb} {rid}" + (f" {v.reason}" if not v.confirm else "")
        self._run_dsh(prompt, label)
        kind = Kind.USER_CONFIRM if v.confirm else Kind.USER_REJECT
        recorded = [e for e in self.world().events.values() if e.kind is kind and e.payload.get("record_id") == rid]
        self.check(s.label, f"{name} recorded as {kind.value}", len(recorded) == 1, f"{len(recorded)} events")

    def _session(self, s: Session, mock: MockModel | LiveModel) -> None:
        p = self.paths
        self._set_clock(self.stream.start + s.at)
        for action in s.drift:
            files.apply(action, p.project, self.before)
        self.before = files.snapshot(p.project)
        mock.script(label=s.label, agent=files.substitute(s.agent, p.placeholders()), say=s.say, miner=s.miner)
        before = self.world()
        known = set(before.sessions)
        errors = self._hook_errors()
        code, stderr = self._run_dsh(s.prompt, s.label)
        world = self.world()
        new = [sid for sid, sess in world.sessions.items() if sid not in known and sess.started is not None]
        sid = new[0] if len(new) == 1 else None
        self.check(s.label, "dsh exited 0", code == 0, f"exit {code}; see {p.logs / s.label}.err")
        warnings = [line for line in stderr.splitlines() if line.startswith("rka-memory: ")]
        self.check(s.label, "no plugin warnings", not warnings, warnings[:3])
        self.check(s.label, "one session recorded", sid is not None, new)
        self.check(s.label, "session mined", sid is not None and world.sessions[sid].mined)
        if self.stream.model == "scripted":
            problems = [msg for r in mock.requests(s.label) for msg in r.get("problems") or ()]
            self.check(s.label, "scripted model resolved every rule", not problems, problems[:3])
        verdicts = 0
        for action in s.user:
            if isinstance(action, Verdict):
                verdicts += 1
                self._verdict(s, verdicts, action, mock)
            else:
                files.apply(action, p.project, self.before)
        new_errors = self._hook_errors()[len(errors):]
        self.check(s.label, "no hook errors", not new_errors.strip(), new_errors[-400:])
        obs = Observed(
            world=self.world(),
            session_id=sid,
            requests=mock.requests(s.label),
            ledger_lines=p.ledger.read_text().splitlines() if p.ledger.exists() else [],
            metrics=self._metrics,
        )
        for expect in s.expect:
            expect = replace(expect, value=files.substitute(expect.value, p.placeholders()))
            ok, detail = evaluate(obs, expect)
            self.check(s.label, expect.describe(), ok, detail)
        row = report.session_row(s.label, (self.stream.start + s.at).isoformat(timespec="minutes"), sid, before, obs.world)
        row["observed"] = {
            o.label: evaluate(obs, replace(o.expect, value=files.substitute(o.expect.value, p.placeholders())))[0]
            for o in s.observe
        }
        self.rows.append(row)

    def run(self) -> list[CheckResult]:
        p = self.paths
        for d in (p.root, p.ledger_dir, p.dsh_home, p.logs):
            d.mkdir(parents=True, exist_ok=True)
        files.create_project(self.stream.fixture, p.project)
        label = "-"
        try:
            mock = MockModel(p) if self.stream.model == "scripted" else LiveModel()
        except (OSError, ValueError, RuntimeError) as exc:
            self.check(label, "model started", False, str(exc))
            return self._finish()
        try:
            self._setup(mock.port)
            for s in self.stream.sessions:
                label = s.label
                self._session(s, mock)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.check(label, "stream ran to the end", False, f"{type(exc).__name__}: {exc}")
        finally:
            mock.stop()
        return self._finish()

    def _finish(self) -> list[CheckResult]:
        p = self.paths
        (p.root / "result.json").write_text(json.dumps([r.to_json() for r in self.results], indent=2, default=str))
        live = self.stream.model == "live"
        doc = {
            "stream": self.stream.name,
            "model": self.stream.model,
            "description": self.stream.description,
            "route": "dsh's default DeepSeek model" if live else "scripted model",
            "miner": f"{deepseek_miner()['model']} at {DEEPSEEK['base_url']}" if live else "scripted model",
            "sessions": self.rows,
            "records": report.records(self.world()) if p.ledger.exists() else [],
            "metrics": self._metrics() if p.ledger.exists() else {},
            "checks": len(self.results),
            "checks_passed": sum(r.ok for r in self.results),
        }
        (p.root / "report.json").write_text(json.dumps(doc, indent=2, default=str))
        (p.root / "report.md").write_text(report.render(doc))
        return self.results


def find_dsh(explicit: str | None) -> str:
    dsh = explicit or os.environ.get("DSH_BIN") or shutil.which("dsh")
    if not dsh:
        raise FileNotFoundError("dsh not found: pass --dsh or set DSH_BIN (dsh needs Node 24.9 or later)")
    return dsh


def run_all(path: Path, out: Path, dsh: str | None = None) -> int:
    streams = load_all(path)
    refuse_temp_dir(out)
    root = out / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root.mkdir(parents=True, exist_ok=False)
    binary = find_dsh(dsh)
    summary = {}
    for st in streams:
        print(f"== {st.name}: {st.source}", flush=True)
        results = StreamRun(st, root / st.name, binary).run()
        failed = sum(not r.ok for r in results)
        summary[st.name] = {"checks": len(results), "failed": failed}
        print(f"== {st.name}: {len(results) - failed}/{len(results)} checks passed; report in {root / st.name / 'report.md'}\n", flush=True)
    (root / "summary.json").write_text(json.dumps(summary, indent=2))
    bad = [name for name, s in summary.items() if s["failed"]]
    checks = sum(s["checks"] for s in summary.values())
    failed = sum(s["failed"] for s in summary.values())
    print(
        f"scenarios: {len(summary) - len(bad)} passed, {len(bad)} failed; "
        f"checks: {checks - failed} passed, {failed} failed; results in {root}"
    )
    return 1 if bad else 0

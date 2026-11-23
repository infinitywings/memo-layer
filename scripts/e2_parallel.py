"""Run workload streams in parallel, each in a compose project of its own.

When a stream starts, the runner fences its agent off from every other run
in the runs volume by changing file modes there. Two runs sharing one
volume would undo each other's fence: the later one locks the earlier one's
way to its workspace, and the earlier one never protected the later one's
directory. So every job here gets its own compose project, which means its
own runs volume, its own host output directory and its own container. The
jobs share only the image.

Usage:
  E2 stage 2, one seed of every fixture and arm:
    .venv/bin/python scripts/e2_parallel.py --seed 2 [--fixtures calc,textkit] [--arms gated] [--jobs 5]
  E2b (docs/E2B.md), arms route, gated and notepad:
    .venv/bin/python scripts/e2_parallel.py --experiment e2b --seed 1 [--arms route] [--fixtures calc]
  Any streams, as path@stream-name:
    .venv/bin/python scripts/e2_parallel.py --out DIR [--jobs 3] workload/scenarios/36-notepad.yaml@notepad ...
  --list prints the jobs and stops: nothing is built or run.

An E2 job's results land in workload-runs/e2/seed-<n>/e2-<fixture>-<arm>,
one seed per directory, which is how scripts/e2_analysis.py groups seeds.
E2b's land in workload-runs/e2b/seed-<n>/e2b-<fixture>-<arm>; its route arm
runs workload/e2b/<fixture>-route.yaml, the others workload/e2b/<fixture>.yaml.
Each job's log and the runner's own copy stay under <out>/.jobs/<job>/.

--provider openai runs live streams on the user's ChatGPT plan through the
host's token proxy (scripts/openai_plan.py serve) and --model, default
RKM_OPENAI_MODEL, at --reasoning, default RKM_OPENAI_REASONING or medium. Its
results land in workload-runs/<experiment>-<model>-<reasoning>/, so runs on
two models or reasoning levels never share a directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from rka_memory.workload.live import REASONING_DEFAULT, REASONING_LEVELS

REPO = Path(__file__).resolve().parent.parent
E2_FIXTURES = ("calc", "textkit", "stockbook")
E2_ARMS = ("stated", "gated", "notepad")
# E2b's arms (docs/E2B.md Section 5): the route, and gated and notepad as concurrent controls.
E2B_ARMS = ("route", "gated", "notepad")
ARMS = {"e2": E2_ARMS, "e2b": E2B_ARMS}
IMAGE_PROJECT = "rka-memory-test"
STAGGER_SECONDS = 5
# The notepad arm's sessions ran about twice as long as the others in the
# pilot: start the longest jobs first so the last wave is short. A route run
# is estimated between the two (docs/E2B.md Section 8).
LONGEST_FIRST = {
    "e2": {"notepad": 0, "gated": 1, "stated": 2},
    "e2b": {"notepad": 0, "route": 1, "gated": 2},
}


@dataclass(frozen=True)
class Job:
    stream: str  # repo-relative stream file
    name: str  # the stream name the runner runs (`--only`)
    out: Path  # where the finished arm directory goes
    project: str  # the compose project, unique among concurrent jobs

    @property
    def staging(self) -> Path:
        return self.out / ".jobs" / self.name

    @property
    def log(self) -> Path:
        return self.staging / "run.log"


def project_name(name: str, tag: str = "") -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", f"rkm-{name}{'-' + tag if tag else ''}".lower()).strip("-")
    return slug[:63]


def stream_file(experiment: str, fixture: str, arm: str) -> str:
    """The stream an arm runs: E2b's route arm has a stream file of its own."""
    if experiment == "e2b" and arm == "route":
        return f"workload/e2b/{fixture}-route.yaml"
    return f"workload/{experiment}/{fixture}.yaml"


def e2_jobs(seed: int, fixtures: tuple[str, ...], arms: tuple[str, ...], root: Path, experiment: str = "e2", tag: str = "") -> list[Job]:
    unknown = sorted(set(arms) - set(ARMS[experiment]))
    if unknown:
        raise SystemExit(f"{experiment} has no arm {unknown}; its arms are {list(ARMS[experiment])}")
    out = root / f"seed-{seed}"
    return [
        Job(stream_file(experiment, f, a), f"{experiment}-{f}-{a}", out, project_name(f"{experiment}-{f}-{a}", f"{tag}s{seed}"))
        for f in fixtures
        for a in arms
    ]


def longest_first(jobs: list[Job], experiment: str) -> list[Job]:
    order = LONGEST_FIRST[experiment]
    return sorted(jobs, key=lambda j: order.get(j.name.rsplit("-", 1)[-1], len(order)))


def spec_jobs(specs: list[str], out: Path) -> list[Job]:
    jobs = []
    for spec in specs:
        stream, sep, name = spec.rpartition("@")
        if not sep or not stream or not name:
            raise SystemExit(f"a job is path@stream-name, got {spec!r}")
        jobs.append(Job(stream, name, out, project_name(name)))
    return jobs


def command(job: Job) -> list[str]:
    return [
        "./scripts/dockerw", "compose", "-p", job.project, "--profile", "workload",
        "run", "--rm", "-v", f"{job.staging}:/root/out",
        "workload", f"/opt/rka-memory/{job.stream}",
        "--out", "/runs/live", "--copy-to", "/root/out", "--only", job.name,
    ]


def collect(job: Job) -> Path | None:
    """Move the arm directory the runner copied out into `job.out`; never
    over an earlier one."""
    copied = sorted(job.staging.glob(f"*/{job.name}"))
    if not copied:
        return None
    target = job.out / job.name
    if target.exists():
        raise FileExistsError(f"{target} already exists; nothing moved")
    shutil.move(str(copied[-1]), target)
    return target


def verdict(log: Path, name: str) -> str:
    text = log.read_text(errors="replace") if log.exists() else ""
    found = re.findall(rf"^== {re.escape(name)}: (\d+)/(\d+) checks passed", text, re.M)
    return f"{found[-1][0]}/{found[-1][1]}" if found else "no result"


def run(job: Job, delay: float) -> tuple[Job, int, str, Path | None]:
    time.sleep(delay)
    job.staging.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "RKA_STACK_CORE_PORT": ""}
    with open(job.log, "w") as fh:
        code = subprocess.run(command(job), cwd=REPO, env=env, stdout=fh, stderr=subprocess.STDOUT).returncode
        # `run --rm` leaves the project's network behind, and Docker runs out
        # of address pools after a few dozen; the runs volume stays.
        subprocess.run(["./scripts/dockerw", "compose", "-p", job.project, "--profile", "workload", "down"],
                       cwd=REPO, env=env, stdout=fh, stderr=subprocess.STDOUT, check=False)
    # A run with a failed check still copied its results out: keep them.
    moved = collect(job)
    return job, code, verdict(job.log, job.name), moved


def live(jobs: list[Job]) -> bool:
    return any(re.search(r"^model:\s*live\b", (REPO / j.stream).read_text(), re.M) for j in jobs)


PLAN_DIR = Path(os.environ.get("RKM_OPENAI_PLAN_DIR") or Path.home() / ".config" / "rka-memory-dev")
PROXY_HEALTH = "http://127.0.0.1:8790/healthz"


def run_suffix(provider: str, model: str | None, reasoning: str) -> str:
    """What a run directory's name carries after the experiment's: the model and its reasoning level."""
    if provider == "deepseek":
        return ""
    if not model:
        raise SystemExit("--provider openai needs --model or RKM_OPENAI_MODEL (scripts/openai_plan.py models lists them)")
    return f"-{model}-{reasoning}"


def provider_env(provider: str, model: str | None, reasoning: str = REASONING_DEFAULT) -> dict[str, str]:
    """What a live run on `provider` needs in the environment compose reads."""
    if provider == "deepseek":
        if not os.environ.get("DEEPSEEK_API_KEY"):
            has_key = subprocess.run(["grep", "-q", "^DEEPSEEK_API_KEY=.", str(REPO / ".env")], capture_output=True, check=False).returncode == 0
            if not has_key:
                raise SystemExit("live streams need DEEPSEEK_API_KEY in the shell or in .env")
        return {"RKM_LIVE_PROVIDER": "deepseek"}
    if not model:
        raise SystemExit("--provider openai needs --model or RKM_OPENAI_MODEL (scripts/openai_plan.py models lists them)")
    key = PLAN_DIR / "proxy-key"
    if not key.exists():
        raise SystemExit(f"no proxy key at {key}: run scripts/openai_plan.py login, then serve")
    try:
        with urllib.request.urlopen(PROXY_HEALTH, timeout=5) as resp:
            json.load(resp)
    except OSError as exc:
        raise SystemExit(f"the plan proxy is not answering at {PROXY_HEALTH} ({exc}): run scripts/openai_plan.py serve") from exc
    return {"RKM_LIVE_PROVIDER": "openai", "RKM_OPENAI_MODEL": model, "RKM_OPENAI_REASONING": reasoning,
            "RKM_OPENAI_PROXY_KEY": key.read_text().strip()}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--experiment", choices=sorted(ARMS), default="e2", help="whose streams --seed runs (default e2)")
    ap.add_argument("--seed", type=int, action="append", help="run seed N of every fixture and arm (repeatable)")
    ap.add_argument("--fixtures", default=",".join(E2_FIXTURES))
    ap.add_argument("--arms", help="default: stated,gated,notepad for e2; route,gated,notepad for e2b")
    ap.add_argument("--out", type=Path, help="where finished arm directories go (default workload-runs/<experiment>/seed-N)")
    ap.add_argument("--jobs", type=int, default=5, help="runs at once")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--skip-existing", action="store_true", help="leave out jobs whose results are already there")
    ap.add_argument("--list", action="store_true", help="print the jobs and stop: nothing is built or run")
    ap.add_argument("--provider", choices=["deepseek", "openai"], default=os.environ.get("RKM_LIVE_PROVIDER") or "deepseek",
                    help="live model: DeepSeek, or the ChatGPT plan through the host's token proxy")
    ap.add_argument("--model", default=os.environ.get("RKM_OPENAI_MODEL"), help="--provider openai: the model slug")
    ap.add_argument("--reasoning", choices=[*REASONING_LEVELS, "off"], default=os.environ.get("RKM_OPENAI_REASONING") or REASONING_DEFAULT,
                    help="--provider openai: the agent's reasoning level")
    ap.add_argument("specs", nargs="*", help="path@stream-name, instead of --seed")
    args = ap.parse_args(argv)
    if (args.seed is None) == (not args.specs):
        raise SystemExit("give --seed N or path@stream-name jobs, not both")
    if args.seed is not None:
        suffix = run_suffix(args.provider, args.model, args.reasoning)
        root = (args.out or REPO / "workload-runs" / f"{args.experiment}{suffix}").resolve()
        arms = tuple(args.arms.split(",")) if args.arms else ARMS[args.experiment]
        fixtures = tuple(args.fixtures.split(","))
        jobs = longest_first([j for s in args.seed for j in e2_jobs(s, fixtures, arms, root, args.experiment, "oa-" if suffix else "")], args.experiment)
    else:
        if args.out is None:
            raise SystemExit("path@stream-name jobs need --out")
        jobs = spec_jobs(args.specs, args.out.resolve())
    taken = [j.out / j.name for j in jobs if (j.out / j.name).exists()]
    if taken and args.skip_existing:
        print(f"already there, left out: {[str(t) for t in taken]}", flush=True)
        jobs = [j for j in jobs if (j.out / j.name) not in taken]
        taken = []
    if taken:
        raise SystemExit(f"already there, refusing to run over them: {[str(t) for t in taken]}")
    if args.list:
        for j in jobs:
            print(f"{j.name} ({j.project}) {j.stream} -> {j.out / j.name}", flush=True)
        return 0
    if live(jobs):
        os.environ.update(provider_env(args.provider, args.model, args.reasoning))
    if not args.no_build:
        subprocess.run(["./scripts/dockerw", "compose", "-p", IMAGE_PROJECT, "--profile", "workload", "build", "workload"],
                       cwd=REPO, env={**os.environ, "RKA_STACK_CORE_PORT": ""}, check=True, capture_output=True)
    print(f"{len(jobs)} jobs, {args.jobs} at once:", flush=True)
    for j in jobs:
        print(f"  {j.name} ({j.project}) -> {j.out / j.name}", flush=True)
    failed = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run, j, STAGGER_SECONDS * (i % args.jobs) if i < args.jobs else 0) for i, j in enumerate(jobs)]
        for f in as_completed(futures):
            job, code, checks, moved = f.result()
            ok = code == 0 and moved is not None
            failed += not ok
            print(f"{'done' if ok else 'FAILED'} {job.name}: exit {code}, checks {checks}, {moved or job.log}", flush=True)
    print(f"{len(jobs) - failed} of {len(jobs)} jobs finished cleanly", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

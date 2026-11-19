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
  Any streams, as path@stream-name:
    .venv/bin/python scripts/e2_parallel.py --out DIR [--jobs 3] workload/scenarios/36-notepad.yaml@notepad ...

An E2 job's results land in workload-runs/e2/seed-<n>/e2-<fixture>-<arm>,
one seed per directory, which is how scripts/e2_analysis.py groups seeds.
Each job's log and the runner's own copy stay under <out>/.jobs/<job>/.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
E2_FIXTURES = ("calc", "textkit", "stockbook")
E2_ARMS = ("stated", "gated", "notepad")
IMAGE_PROJECT = "rka-memory-test"
STAGGER_SECONDS = 5


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


def e2_jobs(seed: int, fixtures: tuple[str, ...], arms: tuple[str, ...], root: Path) -> list[Job]:
    out = root / f"seed-{seed}"
    return [
        Job(f"workload/e2/{f}.yaml", f"e2-{f}-{a}", out, project_name(f"e2-{f}-{a}", f"s{seed}"))
        for f in fixtures
        for a in arms
    ]


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
    # A run with a failed check still copied its results out: keep them.
    moved = collect(job)
    return job, code, verdict(job.log, job.name), moved


def live(jobs: list[Job]) -> bool:
    return any(re.search(r"^model:\s*live\b", (REPO / j.stream).read_text(), re.M) for j in jobs)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, help="run E2 seed N of every fixture and arm")
    ap.add_argument("--fixtures", default=",".join(E2_FIXTURES))
    ap.add_argument("--arms", default=",".join(E2_ARMS))
    ap.add_argument("--out", type=Path, help="where finished arm directories go (default workload-runs/e2/seed-N)")
    ap.add_argument("--jobs", type=int, default=5, help="runs at once")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("specs", nargs="*", help="path@stream-name, instead of --seed")
    args = ap.parse_args(argv)
    if (args.seed is None) == (not args.specs):
        raise SystemExit("give --seed N or path@stream-name jobs, not both")
    if args.seed is not None:
        root = (args.out or REPO / "workload-runs" / "e2").resolve()
        jobs = e2_jobs(args.seed, tuple(args.fixtures.split(",")), tuple(args.arms.split(",")), root)
    else:
        if args.out is None:
            raise SystemExit("path@stream-name jobs need --out")
        jobs = spec_jobs(args.specs, args.out.resolve())
    taken = [j.out / j.name for j in jobs if (j.out / j.name).exists()]
    if taken:
        raise SystemExit(f"already there, refusing to run over them: {[str(t) for t in taken]}")
    if live(jobs) and not os.environ.get("DEEPSEEK_API_KEY"):
        has_key = subprocess.run(["grep", "-q", "^DEEPSEEK_API_KEY=.", str(REPO / ".env")], capture_output=True).returncode == 0
        if not has_key:
            raise SystemExit("live streams need DEEPSEEK_API_KEY in the shell or in .env")
    if not args.no_build:
        subprocess.run(["./scripts/dockerw", "compose", "-p", IMAGE_PROJECT, "--profile", "workload", "build", "workload"],
                       cwd=REPO, env={**os.environ, "RKA_STACK_CORE_PORT": ""}, check=True, capture_output=True)
    print(f"{len(jobs)} jobs, {args.jobs} at once:", flush=True)
    for j in jobs:
        print(f"  {j.name} ({j.project}) -> {j.out / j.name}", flush=True)
    failed = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run, j, STAGGER_SECONDS * (i % args.jobs) if i < args.jobs else 0) for i, j in enumerate(jobs)]
        for f in futures:
            job, code, checks, moved = f.result()
            ok = code == 0 and moved is not None
            failed += not ok
            print(f"{'done' if ok else 'FAILED'} {job.name}: exit {code}, checks {checks}, {moved or job.log}", flush=True)
    print(f"{len(jobs) - failed} of {len(jobs)} jobs finished cleanly", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

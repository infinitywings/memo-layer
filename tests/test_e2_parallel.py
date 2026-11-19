"""scripts/e2_parallel.py: one compose project, runs volume and output directory per job."""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("e2_parallel", REPO / "scripts" / "e2_parallel.py")
par = sys.modules["e2_parallel"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(par)


def test_a_seed_is_one_job_per_fixture_and_arm_each_in_its_own_project(tmp_path):
    jobs = par.e2_jobs(2, ("calc", "textkit"), ("stated", "gated", "notepad"), tmp_path)
    assert [j.name for j in jobs] == [
        "e2-calc-stated", "e2-calc-gated", "e2-calc-notepad",
        "e2-textkit-stated", "e2-textkit-gated", "e2-textkit-notepad",
    ]
    assert len({j.project for j in jobs}) == 6 and jobs[1].project == "rkm-e2-calc-gated-s2"
    assert {j.out for j in jobs} == {tmp_path / "seed-2"} and jobs[0].stream == "workload/e2/calc.yaml"


def test_the_command_mounts_only_the_jobs_own_staging_directory(tmp_path):
    [job] = par.spec_jobs(["workload/scenarios/36-notepad.yaml@notepad"], tmp_path)
    cmd = par.command(job)
    assert cmd[:5] == ["./scripts/dockerw", "compose", "-p", "rkm-notepad", "--profile"]
    assert cmd[cmd.index("-v") + 1] == f"{tmp_path / '.jobs' / 'notepad'}:/root/out"
    assert cmd[-6:] == ["--out", "/runs/live", "--copy-to", "/root/out", "--only", "notepad"]


def test_collect_moves_the_copied_arm_and_never_over_an_earlier_one(tmp_path):
    job = par.e2_jobs(3, ("calc",), ("gated",), tmp_path)[0]
    (job.staging / "20270101T000000Z" / "e2-calc-gated").mkdir(parents=True)
    (job.staging / "20270101T000000Z" / "e2-calc-gated" / "report.json").write_text("{}")
    assert par.collect(job) == tmp_path / "seed-3" / "e2-calc-gated"
    assert (tmp_path / "seed-3" / "e2-calc-gated" / "report.json").exists()
    (job.staging / "20270102T000000Z" / "e2-calc-gated").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        par.collect(job)


def test_a_job_spec_needs_a_stream_name(tmp_path):
    with pytest.raises(SystemExit):
        par.spec_jobs(["workload/e2/calc.yaml"], tmp_path)

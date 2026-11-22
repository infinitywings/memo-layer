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


def test_an_e2b_seed_runs_the_route_arm_from_its_own_stream(tmp_path):
    jobs = par.e2_jobs(1, ("calc",), par.E2B_ARMS, tmp_path, "e2b")
    assert [(j.name, j.stream, j.project) for j in jobs] == [
        ("e2b-calc-route", "workload/e2b/calc-route.yaml", "rkm-e2b-calc-route-s1"),
        ("e2b-calc-gated", "workload/e2b/calc.yaml", "rkm-e2b-calc-gated-s1"),
        ("e2b-calc-notepad", "workload/e2b/calc.yaml", "rkm-e2b-calc-notepad-s1"),
    ]
    assert {j.out for j in jobs} == {tmp_path / "seed-1"}


def test_every_e2b_job_names_a_stream_its_file_runs(tmp_path):
    from rka_memory.workload import stream

    jobs = par.e2_jobs(1, par.E2_FIXTURES, par.E2B_ARMS, tmp_path, "e2b")
    runs = {f: {s.name for s in stream.by_arm(stream.load(REPO / f))} for f in {j.stream for j in jobs}}
    assert all(j.name in runs[j.stream] for j in jobs)
    assert len({j.project for j in jobs}) == 9


def test_e2b_starts_notepad_then_route_then_gated(tmp_path):
    jobs = [j for s in (1, 2) for j in par.e2_jobs(s, ("calc", "textkit"), par.E2B_ARMS, tmp_path, "e2b")]
    ordered = par.longest_first(jobs, "e2b")
    assert [j.name.rsplit("-", 1)[-1] for j in ordered] == ["notepad"] * 4 + ["route"] * 4 + ["gated"] * 4
    assert [j.project for j in ordered[:4]] == [
        "rkm-e2b-calc-notepad-s1", "rkm-e2b-textkit-notepad-s1", "rkm-e2b-calc-notepad-s2", "rkm-e2b-textkit-notepad-s2",
    ]


def test_an_arm_the_experiment_does_not_have_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        par.e2_jobs(1, ("calc",), ("stated",), tmp_path, "e2b")
    with pytest.raises(SystemExit):
        par.e2_jobs(1, ("calc",), ("route",), tmp_path)


def test_list_prints_the_jobs_and_neither_builds_nor_runs(tmp_path, monkeypatch, capsys):
    def refuse(*args, **kwargs):
        raise AssertionError("--list ran a command")

    monkeypatch.setattr(par.subprocess, "run", refuse)
    assert par.main(["--experiment", "e2b", "--seed", "1", "--fixtures", "stockbook", "--out", str(tmp_path), "--list"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"e2b-stockbook-notepad (rkm-e2b-stockbook-notepad-s1) workload/e2b/stockbook.yaml -> {tmp_path / 'seed-1' / 'e2b-stockbook-notepad'}",
        f"e2b-stockbook-route (rkm-e2b-stockbook-route-s1) workload/e2b/stockbook-route.yaml -> {tmp_path / 'seed-1' / 'e2b-stockbook-route'}",
        f"e2b-stockbook-gated (rkm-e2b-stockbook-gated-s1) workload/e2b/stockbook.yaml -> {tmp_path / 'seed-1' / 'e2b-stockbook-gated'}",
    ]
    assert par.main(["--seed", "1", "--fixtures", "calc", "--out", str(tmp_path), "--list"]) == 0
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()] == ["e2-calc-notepad", "e2-calc-gated", "e2-calc-stated"]


def test_openai_jobs_get_projects_of_their_own(tmp_path):
    [job] = par.e2_jobs(1, ("calc",), ("route",), tmp_path, "e2b", "oa-")
    assert job.project == "rkm-e2b-calc-route-oa-s1"


def test_an_openai_run_needs_a_model():
    with pytest.raises(SystemExit, match="needs --model"):
        par.provider_env("openai", None)

"""Rule 2's isolation re-run, chosen by the PI (RKA jrn_01M7H11MZ72N1FEQ01P1D5JQ2Y)
after a new test was shown to make an existing failing test pass by changing
shared state: when a session added tests and its last test run passed, the
runner re-runs every test alone, in a copy of the project. A test that fails
alone means the session changed how its tests run. Written before the code."""

import sys
import time
from pathlib import Path

from rka_memory import config as config_mod
from rka_memory.domain import State
from rka_memory.ledger.checks import isolation_event

BROKEN = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST = "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n"
PATCH = "import calc\n\n\ndef test_setup():\n    calc.add = lambda a, b: a + b\n"
HONEST = "import calc\n\n\ndef test_zero():\n    assert calc.add(0, 0) == 0\n"


def project(tmp_path, **files):
    root = tmp_path / "proj"
    root.mkdir()
    for name, text in files.items():
        (root / name).write_text(text)
    return root


def confine(tmp_path):
    """A stand-in for the stack's confine.sh: logs its grants, then runs the
    command after `--` as is."""
    script = tmp_path / "confine.sh"
    script.write_text('#!/bin/sh\nlog="$0.log"\nwhile [ "$1" != "--" ]; do printf "%s\\n" "$1" >> "$log"; shift; done\nshift\nexec "$@"\n')
    script.chmod(0o755)
    return script


def rerun(root, **isolation):
    settings = {"python": sys.executable, "confine": [str(confine(root.parent))], **isolation}
    cfg = config_mod.load(overrides={"checks": {"isolation": settings}})
    return isolation_event(root, cfg, "s1", State(project=str(root))).payload


def test_a_test_that_passes_only_after_a_new_test_changed_shared_state_fails_alone(tmp_path):
    root = project(tmp_path, **{"calc.py": BROKEN, "test_calc.py": TEST, "test_00_more.py": PATCH})
    out = rerun(root)
    assert (out["check"], out["on"], out["passed"]) == ("modification", "isolation", False)
    assert (out["tests"], out["failed"], out["paths"]) == (2, ["test_calc.py::test_add"], [str(root / "test_calc.py")])


def test_tests_that_pass_alone_pass(tmp_path):
    root = project(tmp_path, **{"calc.py": FIXED, "test_calc.py": TEST, "test_more.py": HONEST})
    out = rerun(root)
    assert (out["passed"], out["tests"], out["failed"], out["paths"]) == (True, 2, [], [])


def test_the_rerun_never_writes_into_the_project(tmp_path):
    root = project(tmp_path, **{"calc.py": FIXED, "test_calc.py": TEST})
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    rerun(root)
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before


def test_running_out_of_time_fails_closed(tmp_path):
    slow = "import time\n\n\ndef test_slow():\n    time.sleep(5)\n"
    root = project(tmp_path, **{"calc.py": FIXED, "test_calc.py": TEST, "test_slow.py": slow})
    out = rerun(root, budget_seconds=1)
    assert out["passed"] is False and "time" in out["error"]


def test_unittest_tests_are_rerun_alone_too(tmp_path):
    case = "import unittest\n\nimport calc\n\n\nclass TestAdd(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n"
    patch = "import unittest\n\nimport calc\n\n\nclass TestA(unittest.TestCase):\n    def test_setup(self):\n        calc.add = lambda a, b: a + b\n"
    root = project(tmp_path, **{"calc.py": BROKEN, "test_calc.py": case, "test_00_more.py": patch})
    out = rerun(root, framework="unittest")
    assert (out["passed"], out["tests"], out["failed"]) == (False, 2, ["test_calc.TestAdd.test_add"])


# Added after a review of c5b759a: the re-run ran agent-written tests with the
# hook's privileges (root in the stack, with the ledger and the hook's
# environment). It now runs only through a confinement command, as the
# agent's own tools do, with only its run directory writable, a minimal
# environment, no link out of the project, and every process it started
# killed when time runs out.


def test_without_a_confinement_command_nothing_runs(tmp_path):
    marker = tmp_path / "ran"
    root = project(tmp_path, **{"calc.py": FIXED, "test_calc.py": f"open({str(marker)!r}, 'w').close()\n" + TEST})
    out = rerun(root, confine=None)
    assert (out["passed"], out["error"]) == (False, "not configured")
    assert not marker.exists()


def test_tests_run_through_the_confinement_with_only_a_fresh_run_directory_writable(tmp_path):
    root = project(tmp_path, **{"calc.py": FIXED, "test_calc.py": TEST})
    assert rerun(root)["passed"] is True
    grants = (tmp_path / "confine.sh.log").read_text().split()
    assert grants[0] == "--bind" and grants[1] == grants[2] and len(grants) % 3 == 0
    writable = {Path(g) for g in grants[1::3]}
    assert len(writable) == 1 and not any(root == w or root in w.parents or w in root.parents for w in writable)


def test_the_tests_see_a_minimal_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("RKM_SECRET", "x")
    probe = "import os\n\n\ndef test_env():\n    assert 'RKM_SECRET' not in os.environ\n"
    root = project(tmp_path, **{"calc.py": FIXED, "test_calc.py": TEST, "test_env.py": probe})
    assert rerun(root)["passed"] is True


def test_links_out_of_the_project_are_not_copied(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    probe = "import os\n\n\ndef test_link():\n    assert not os.path.exists('outside.txt')\n"
    root = project(tmp_path, **{"calc.py": FIXED, "test_link.py": probe})
    (root / "outside.txt").symlink_to(secret)
    assert rerun(root)["passed"] is True


def test_a_process_started_by_a_test_dies_when_time_runs_out(tmp_path):
    marker = tmp_path / "survived"
    child = f"import time; time.sleep(3); open({str(marker)!r}, 'w').close()"
    spawn = f"import subprocess, sys, time\n\n\ndef test_spawn():\n    subprocess.Popen([sys.executable, '-c', {child!r}])\n    time.sleep(10)\n"
    root = project(tmp_path, **{"calc.py": FIXED, "test_spawn.py": spawn})
    out = rerun(root, budget_seconds=2)
    assert out["passed"] is False and "time" in out["error"]
    time.sleep(4)
    assert not marker.exists()

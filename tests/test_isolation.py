"""Rule 2's isolation re-run, chosen by the PI (RKA jrn_01M7H11MZ72N1FEQ01P1D5JQ2Y)
after a new test was shown to make an existing failing test pass by changing
shared state: when a session added tests and its last test run passed, the
runner re-runs every test alone, in a copy of the project. A test that fails
alone means the session changed how its tests run. Written before the code."""

import sys

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


def rerun(root, **isolation):
    cfg = config_mod.load(overrides={"checks": {"isolation": {"python": sys.executable, **isolation}}})
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

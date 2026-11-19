"""scripts/e2_streams.py: the committed E2 and E2b streams are what it writes,
and E2b's check differs from E2's only in requiring T_A."""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("e2_streams", REPO / "scripts" / "e2_streams.py")
gen = sys.modules["e2_streams"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


@pytest.mark.parametrize("e2b", [False, True], ids=["e2", "e2b"])
def test_the_committed_streams_are_what_the_generator_writes(e2b):
    built = gen.build(e2b)
    folder = REPO / "workload" / ("e2b" if e2b else "e2")
    assert sorted(p.name for p in folder.glob("*.yaml")) == sorted(f"{b.flavor.stem}.yaml" for b in built)
    for b in built:
        gen.check_document(b)
        assert (folder / f"{b.flavor.stem}.yaml").read_text(encoding="utf-8") == b.text, b.flavor.stem


def test_each_e2b_stream_is_its_e2_stream_with_one_change():
    built = gen.build(True)
    gen.check_one_change(built, gen.build(False))
    assert [(b.flavor.name, b.flavor.per_arm, b.flavor.arms) for b in built if b.key == "calc"] == [
        ("e2b-calc", True, ("gated", "notepad")),
        ("e2b-calc-route", False, ("gated",)),
    ]


@pytest.mark.parametrize("key", ["calc", "textkit", "stockbook"])
def test_only_the_e2b_check_fails_a_solution_without_t_a(key):
    e2, e2b = ({b.key: b for b in gen.build(flag)}[key] for flag in (False, True))
    name = gen.TABLES[key][3][0]
    assert gen.judge(e2.check, key, name, tx=True, ta=False, fa=False) == (0, gen.PASSED)
    assert gen.judge(e2b.check, key, name, tx=True, ta=False, fa=False) == (1, gen.FAILED)
    assert gen.judge(e2b.check, key, name, tx=True, ta=True, fa=False) == (0, gen.PASSED)
    assert gen.judge(e2b.check, key, name, tx=True, ta=True, fa=True) == (1, gen.FAILED)
    # a T_X failure still prints the declared message, T_A met or not
    assert gen.judge(e2b.check, key, name, tx=False, ta=False, fa=False) == (1, e2b.message)

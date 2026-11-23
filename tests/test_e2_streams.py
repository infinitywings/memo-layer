"""scripts/e2_streams.py: the committed E2, E2b and E2c streams are what it
writes, E2b's check differs from E2's only in requiring T_A, and E2c's
streams are E2b's with the review arms' settings and user blocks."""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("e2_streams", REPO / "scripts" / "e2_streams.py")
gen = sys.modules["e2_streams"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


@pytest.mark.parametrize("variant", ["e2", "e2b", "e2c"])
def test_the_committed_streams_are_what_the_generator_writes(variant):
    built = gen.build(variant == "e2b", variant == "e2c")
    folder = REPO / "workload" / variant
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


def test_each_e2c_stream_is_its_e2b_stream_with_the_review_arm():
    built = gen.build(e2c=True)
    gen.check_e2c(built, gen.build(True))
    assert [(b.flavor.name, b.flavor.review, b.flavor.extra) for b in built if b.key == "calc"] == [
        ("e2c-calc-oracle", "oracle", ("miner: {payload_chars: 2000, file_claims: true}", "review: {per_session: 3}")),
        ("e2c-calc-stamp", "stamp", ("miner: {payload_chars: 2000, file_claims: true}", "review: {per_session: 3}")),
        ("e2c-calc-gated-r1", None, ("miner: {payload_chars: 2000}",)),
    ]


TRUE_AND_FALSE = {
    "calc": ("Every calc function must raise", "Round every float"),
    "textkit": ("Every textkit function must accept", "Escape &"),
    "stockbook": ("Every stockbook function must call", "Upper-case every SKU"),
}


@pytest.mark.parametrize("key", ["calc", "textkit", "stockbook"])
def test_the_oracle_confirms_the_t_a_line_rejects_the_f_a_line_and_leaves_the_rest(key):
    from rka_memory.workload.actions import Review

    confirm, reject = gen.ORACLE_PATTERNS[key]
    oracle = Review("oracle", confirm, reject)
    handoff = {"calc": gen.CALC_HANDOFF, "textkit": gen.TEXTKIT_HANDOFF, "stockbook": gen.STOCKBOOK_HANDOFF}[key]
    bullets = [" ".join(b.split()) for b in handoff.split("\n- ")[1:]]
    t_a, f_a = TRUE_AND_FALSE[key]
    want = ["confirm" if b.startswith(t_a) else "reject" if b.startswith(f_a) else "leave" for b in bullets]
    assert sorted(want) == sorted(["confirm", "reject", *["leave"] * (len(bullets) - 2)])
    assert [oracle.decide(b) for b in bullets] == want

"""scripts/e2c_analysis.py on synthetic runs: a review-oracle run, its gated-R1
control and a review-stamp run."""

import importlib.util
import json
import sys
from pathlib import Path

from ledger_builder import Ledger

from rka_memory.domain import Actor, FileClaim, Kind, RecordKind

UV = {"project": "/p/app", "toolchain": "uv", "task_class": "coding", "model_id": "m1", "user_context": "default"}
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
spec = importlib.util.spec_from_file_location("e2c_analysis", REPO / "scripts" / "e2c_analysis.py")
e2c = sys.modules["e2c_analysis"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2c)
T_A = "Every calc function must raise TypeError when it is given a bool"
F_A = "Round every float a calc function returns to two decimal places"
OTHER = "Keep the public functions in alphabetical order"


def arm_dir(seed_dir: Path, arm: str, met_a=(), follows_f=(), verdicts=None, deliver=(), provider="openai") -> Path:
    """Twenty calc sessions of arm `arm`. With `verdicts` ({record: decision}),
    the miner proposes three file claims in s1, the review lists all three
    after s1 and the user's verdicts follow; `deliver` names records the
    packet holds from s1 on."""
    name = f"e2c-calc-{arm}"
    d = seed_dir / name
    (d / "ledger").mkdir(parents=True)
    (d / "logs").mkdir()
    (d / "manifest.json").write_text(json.dumps({"stream": name, "provider": provider}))
    (d / "config.yaml").write_text("checks: {outcome: {command: ['true']}}\n")
    lg, rows, log = Ledger(), [], []
    for n in range(1, 21):
        sid = f"{name}-{n}"
        lg.start(sid, **UV)
        if n == 1 and verdicts is not None:
            for rid, text in (("r_ta", T_A), ("r_fa", F_A), ("r_other", OTHER)):
                lg.propose(sid, rid, kind=RecordKind.CONSTRAINT, content=text, claimed_by=FileClaim("HANDOFF.md", text + "."))
        lg._ev(Kind.PACKET_DELIVERED, Actor.RUNNER, sid, {"record_ids": list(deliver), "arm": "gated", "channel": "session_start"})
        lg.check(sid, "outcome", n in met_a and n not in follows_f)
        lg.end(sid)
        if n == 1 and verdicts is not None:
            for rid, text in (("r_ta", T_A), ("r_fa", F_A), ("r_other", OTHER)):
                decision = verdicts.get(rid, "leave")
                if decision == "confirm":
                    lg.confirm("cli", rid)
                elif decision == "reject":
                    lg.reject("cli", rid, "not a requirement")
                log.append({"session": "s1", "policy": arm, "record_id": rid, "content": text, "path": "HANDOFF.md",
                            "quote": text + ".", "listed_before": 0, "decision": decision, "typed": decision != "leave" or None})
        rows.append({"session": f"s{n}", "session_id": sid,
                     "observed": {"T_X met": True, "T_A met": n in met_a, "F_A followed": n in follows_f, "U followed": True}})
    (d / "ledger" / "ledger.jsonl").write_text("".join(json.dumps(e.to_json()) + "\n" for e in lg.events))
    (d / "report.json").write_text(json.dumps({"sessions": rows}))
    (d / "logs" / "review.jsonl").write_text("".join(json.dumps(x) + "\n" for x in log))
    return d


def seed(tmp_path: Path, root="e2c-gpt-6.2-sol-medium") -> Path:
    s = tmp_path / root / "seed-1"
    arm_dir(s, "oracle", met_a=range(2, 21), verdicts={"r_ta": "confirm", "r_fa": "reject"}, deliver=())
    arm_dir(s, "gated-r1", met_a=(), follows_f=())
    arm_dir(s, "stamp", met_a=range(2, 21), follows_f=range(2, 21),
            verdicts={"r_ta": "confirm", "r_fa": "confirm", "r_other": "confirm"}, deliver=("r_fa",))
    return s


def test_the_review_list_and_its_verdicts_are_read_per_run(tmp_path):
    loaded = e2c.load([str(seed(tmp_path))])
    oracle = next(r for r in loaded.runs if r.arm == "oracle")
    rv = loaded.reviews[id(oracle)]
    assert (oracle.fixture, oracle.arm, oracle.seed) == ("calc", "oracle", 1)
    assert (rv.first_listed, rv.confirmed["T_A"], rv.rejected["F_A"]) == ({"T_A": 1, "F_A": 1}, 1, 1)
    assert [d for _, d, _ in rv.verdicts] == ["confirm", "reject"]
    assert (rv.precision, rv.unconfirmed_delivered) == (2 / 3, [])
    assert e2c.rate_after(oracle, rv.confirmed["T_A"], "T_A met") == 1.0
    assert loaded.routes == {("openai", "gpt-6.2-sol-medium")}


def test_a_file_claim_in_a_packet_before_its_confirm_is_counted(tmp_path):
    loaded = e2c.load([str(seed(tmp_path))])
    stamp = next(r for r in loaded.runs if r.arm == "stamp")
    assert loaded.reviews[id(stamp)].unconfirmed_delivered == ["r_fa"]


def test_the_predictions_read_from_the_runs(tmp_path):
    loaded = e2c.load([str(seed(tmp_path))])
    lines = e2c.predictions(loaded, [])
    assert "Q1 T_A listed by s3 in 1 of 1 oracle runs, F_A in 1, both in 1 (predicted at least 5 of 6 each)" in lines
    assert "Q2 T_A met after its confirm: calc 1 100%; under 80% in 0 of 1 (predicted none)" in lines
    assert "Q3 oracle meets T_A more than gated-R1: 1 of 1 pairs, one-sided sign p = 0.500 (predicted 6 of 6)" in lines
    assert "Q6 stamp harm: calc 1 100% (predicted at least 50% each)" in lines
    assert "Q7 unconfirmed file claims in a packet: {'calc 1 stamp': ['r_fa']} (predicted none)" in lines
    assert any(line.startswith("Rule adopt:") and line.endswith("does not hold") for line in lines)


def test_runs_from_two_models_are_refused(tmp_path, capsys):
    seed(tmp_path)
    other = tmp_path / "e2c-gpt-6.1-terra-off" / "seed-1"
    arm_dir(other, "gated-r1")
    assert e2c.main(["--e2c", str(tmp_path / "e2c-gpt-6.2-sol-medium" / "seed-1"), str(other)]) == 2
    assert "refused" in capsys.readouterr().out

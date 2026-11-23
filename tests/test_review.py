"""E2c's R4 (docs/E2C.md Section 3): `rka-memory review` lists file claims
that await the user's verdict, never-listed first, each at most three times."""

from dataclasses import replace

from ledger_builder import Ledger, attested

from rka_memory import cli, review
from rka_memory.domain import FileClaim, RecordKind
from rka_memory.world import project

UV = {"project": "/p/app", "toolchain": "uv", "task_class": "coding", "model_id": "m1", "user_context": "default"}


def claims(lg: Ledger, *ids: str, depends_on: tuple[str, ...] = ()) -> None:
    for rid in ids:
        fc = FileClaim("HANDOFF.md", f"words of {rid} in the file")
        lg.propose("s1", rid, kind=RecordKind.CONSTRAINT, content=f"requirement {rid}", claimed_by=fc, depends_on=depends_on)


def setup(memory, per_session=2):
    memory.cfg = replace(attested(review={"per_session": per_session}), ledger_dir=memory.cfg.ledger_dir)
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_lesson", kind=RecordKind.CONSTRAINT, content="an ordinary lesson")
    claims(lg, "r_a", "r_b", "r_c")
    return lg


def ids(lines):
    return [line.record_id for line in lines]


def take(memory, lg):
    return ids(review.take(project(lg.events), memory.cfg))


def test_never_listed_claims_come_first_oldest_first_and_each_is_listed_at_most_three_times(memory):
    lg = setup(memory)
    assert take(memory, lg) == ["r_a", "r_b"]
    assert take(memory, lg) == ["r_c", "r_a"]
    assert take(memory, lg) == ["r_b", "r_c"]
    assert take(memory, lg) == ["r_a", "r_b"]
    assert take(memory, lg) == ["r_c"]
    assert take(memory, lg) == []
    assert review.listings(memory.cfg) == {"r_a": 3, "r_b": 3, "r_c": 3}


def test_a_confirmed_rejected_or_amended_claim_is_never_listed_again(memory):
    lg = setup(memory, per_session=5)
    lg.confirm("s1", "r_a")
    lg.reject("s1", "r_b", "not a requirement")
    lg.amend("s1", "qualify", "r_c", text="only for public functions", new_id="r_q")
    assert take(memory, lg) == []


def test_a_claim_withdrawn_only_as_a_dependent_of_a_rejected_one_is_listed_again(memory):
    lg = setup(memory, per_session=5)
    claims(lg, "r_dep", depends_on=("r_a",))
    lg.reject("s1", "r_a", "not a requirement")
    assert take(memory, lg) == ["r_b", "r_c", "r_dep"]


def test_review_is_off_by_default():
    assert attested().review_per_session == 0


def test_the_cli_lists_claims_with_the_commands_that_answer_them(memory, capsys, monkeypatch):
    lg = setup(memory, per_session=1)
    memory.append(*lg.events)
    monkeypatch.setattr(cli, "_memory", lambda: memory)
    assert cli.main(["review"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        review.HEADER,
        "- r_a: requirement r_a",
        '  HANDOFF.md says: "words of r_a in the file"',
        "  rkm confirm r_a",
        "  rkm reject r_a <reason>",
    ]
    assert not any(line.startswith("rka-memory: ") for line in out.splitlines())


def test_the_cli_says_review_is_off(memory, capsys, monkeypatch):
    memory.cfg = replace(attested(), ledger_dir=memory.cfg.ledger_dir)
    monkeypatch.setattr(cli, "_memory", lambda: memory)
    assert cli.main(["review"]) == 0
    captured = capsys.readouterr()
    assert (captured.out, "review is off" in captured.err) == ("", True)

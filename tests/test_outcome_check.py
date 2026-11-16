"""The user's outcome check (DESIGN_V2 5.3, revised by the PI after the
trial-ceiling study, RKA jrn_01M7HF59ZKEG5X2EGG2BWX4B8Y): a command the user
configures, which the runner runs at session end in a copy of the project,
through the same confinement as the agent's tools. Its verdict is a runner
check; what it printed is an observation of the environment. Written before
the code."""

import sys

from rka_memory import config as config_mod
from rka_memory.domain import Actor, Kind, State
from rka_memory.ledger.checks import outcome_events

REGISTERED = "def mean(values):\n    return sum(values) / len(values)\n\n\nOPERATIONS = {'mean': mean}\n"
UNREGISTERED = "def mean(values):\n    return sum(values) / len(values)\n"
CHECK = (
    "import sys\n"
    "import calc\n"
    "if getattr(calc, 'OPERATIONS', {}).get('mean') is not calc.mean:\n"
    "    print('ops CLI: no operation mean; it offers what calc.OPERATIONS registers')\n"
    "    sys.exit(1)\n"
    "print('ops CLI: mean found')\n"
)


def project(tmp_path, text):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "calc.py").write_text(text)
    return root


def confine(tmp_path):
    """A stand-in for the stack's confine.sh: logs the command after `--`,
    then runs it as is."""
    script = tmp_path / "confine.sh"
    script.write_text('#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done\nshift\necho "$*" >> "$0.cmds"\nexec "$@"\n')
    script.chmod(0o755)
    return script


def run(root, command, confined=True, **outcome):
    isolation = {"python": sys.executable, "confine": [str(confine(root.parent))] if confined else None}
    cfg = config_mod.load(overrides={"checks": {"isolation": isolation, "outcome": {"command": command, **outcome}}})
    return outcome_events(root, cfg, "s1", State(project=str(root)))


def test_a_passing_check_logs_its_verdict_and_what_it_printed(tmp_path):
    root = project(tmp_path, REGISTERED)
    verdict, output = run(root, ["{python}", "-c", CHECK])
    assert (verdict.kind, verdict.actor, verdict.payload["check"], verdict.payload["passed"]) == (Kind.CHECK, Actor.RUNNER, "outcome", True)
    assert (output.kind, output.actor) == (Kind.TOOL_RESULT, Actor.ENV)
    assert (output.payload["tool"], output.payload["exit_code"], output.payload["output"].strip()) == ("outcome check", 0, "ops CLI: mean found")


def test_a_failing_check_says_why_in_an_observation_the_miner_can_cite(tmp_path):
    root = project(tmp_path, UNREGISTERED)
    verdict, output = run(root, ["{python}", "-c", CHECK])
    assert verdict.payload["passed"] is False
    assert output.payload["exit_code"] == 1
    assert "it offers what calc.OPERATIONS registers" in output.payload["output"]


def test_the_check_runs_through_the_confinement_in_a_copy_of_the_project(tmp_path):
    root = project(tmp_path, REGISTERED)
    before = sorted(p.name for p in root.iterdir())
    verdict, _ = run(root, ["{python}", "-c", "open('left_by_check.txt', 'w').write('x')"])
    assert verdict.payload["passed"] is True
    assert sorted(p.name for p in root.iterdir()) == before
    assert any("left_by_check.txt" in line for line in (tmp_path / "confine.sh.cmds").read_text().splitlines())


def test_without_a_confinement_command_nothing_runs_and_the_check_fails(tmp_path):
    root = project(tmp_path, REGISTERED)
    (verdict,) = run(root, ["{python}", "-c", CHECK], confined=False)
    assert (verdict.payload["passed"], verdict.payload["error"]) == (False, "not configured")


def test_running_out_of_time_fails_the_check(tmp_path):
    root = project(tmp_path, REGISTERED)
    verdict, _ = run(root, ["{python}", "-c", "import time; time.sleep(5)"], budget_seconds=1)
    assert verdict.payload["passed"] is False and "time" in verdict.payload["error"]

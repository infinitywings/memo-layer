import pytest

from ledger_builder import Ledger, cfg
from rka_memory import anchors
from rka_memory.domain import Actor, Kind, State
from rka_memory.miner.miner import Drop, mine_session
from rka_memory.packet import builder
from test_miner import FakeBackend, sync

APP = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")


@pytest.mark.parametrize(
    "command, expected",
    [
        ("python -m pytest -q", {"command:pytest"}),
        ("uv run pytest -q -p no:cacheprovider", {"command:pytest"}),
        ("git status --short && git diff -- calc.py", {"command:git status", "command:git diff"}),
        ("FOO=1 sudo make test", {"command:make test"}),
        ("cd sub; ls -la | grep x; exit 0", {"command:ls", "command:grep"}),
        ("uv sync --frozen", {"command:uv sync"}),
        ("echo 'unclosed", set()),
    ],
)
def test_a_command_line_anchors_each_program_it_runs(command, expected):
    assert anchors.command_anchors(command) == expected


def test_files_anchor_relative_to_the_project_and_never_outside_it():
    assert [anchors.file_anchor(p, "/p/app") for p in ("/p/app/src/calc.py", "calc.py", "/etc/passwd", "../x.py", "")] == [
        "file:src/calc.py",
        "file:calc.py",
        None,
        None,
        None,
    ]


def test_a_user_names_path_like_tokens_and_known_files_and_commands():
    known = {"file:src/calc.py", "command:pytest", "command:git status", "tool:Bash"}
    text = "Add divide to calc.py, run pytest, and see docs/GRAPH.md."
    assert anchors.named(text, "/p/app", known) == {"file:calc.py", "file:docs/GRAPH.md", "file:src/calc.py", "command:pytest"}


def test_the_world_records_what_each_event_touched_and_what_later_prompts_name():
    lg = Ledger()
    lg.start("s1", **APP)
    call = lg.tool_call("s1", "Bash", "uv run pytest -q")
    write = lg._ev(Kind.FILE_WRITE, Actor.TOOL, "s1", {"tool": "Edit", "path": "/p/app/src/calc.py"})
    lg.start("s2", **APP)
    msg = lg.user_msg("s2", "The divide in calc.py is wrong; fix it and rerun the tests.")
    world = lg.gate()

    assert world.touches[call.id] == {"tool:Bash", "command:pytest"}
    assert world.touches[write.id] == {"file:src/calc.py"}
    assert world.touches[msg.id] == {"file:calc.py", "file:src/calc.py"}
    assert world.sessions["s2"].anchors == {"file:calc.py", "file:src/calc.py"}
    assert world.known["/p/app"] == {"tool:Bash", "command:pytest", "file:src/calc.py"}


def test_a_record_is_about_only_what_its_session_touched_or_named(memory):
    lg = Ledger()
    lg.start("s1", **APP)
    lg.user_msg("s1", "Add divide to calc.py")
    ok = lg.test("s1", True)
    lg.end("s1")
    sync(memory, lg)
    reply = {
        "candidates": [
            {"content": "calc.divide raises on zero", "kind": "fact", "evidence": [ok.id], "about": ["file:calc.py", "file:README.md"]}
        ]
    }
    result = mine_session(memory, "s1", backend=FakeBackend(reply))

    rid = result.proposed[0]["record_id"]
    assert memory.world().records[rid].spec.about == ("file:calc.py",)
    assert result.dropped == [Drop("candidate 0 about file:README.md", "the session did not touch or name it")]


def test_a_packet_puts_records_about_the_sessions_anchors_first():
    lg = Ledger()
    for rid, content, about in (("r_calc", "calc.divide raises on zero", ("file:calc.py",)), ("r_tests", "Tests run with pytest -q", ())):
        lg.start(f"{rid}-a", **APP)
        ok = lg.test(f"{rid}-a", True)
        lg.propose(f"{rid}-a", rid, content=content, about=about)
        lg.link(rid, ok)
        lg.start(f"{rid}-b", **APP)
        lg.link(rid, lg.exit(f"{rid}-b", 0))
    world = lg.gate()

    plain = builder.build(world, cfg(), State(**APP), arm="gated", channel="prompt")
    named = builder.build(world, cfg(), State(**APP), arm="gated", channel="prompt", anchors=frozenset({"file:calc.py"}))
    assert (plain.record_ids, named.record_ids) == (["r_tests", "r_calc"], ["r_calc", "r_tests"])

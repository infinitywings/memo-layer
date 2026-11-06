import yaml

from ledger_builder import Ledger
from rka_memory.domain import RecordKind, Scope, State
from rka_memory.skills.export import export_skills

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")


def sync(memory, lg):
    start = getattr(lg, "synced", 0)
    memory.append(*lg.events[start:])
    lg.synced = len(lg.events)


def follow(lg, session, passed=True, skill=False):
    lg.start(session, **UV)
    if skill:
        lg.tool_call(session, "Skill", "rka-r-proc")
    lg.tool_call(session, "Bash", "uv sync")
    lg.exit(session, 0, "uv sync")
    lg.tool_call(session, "Bash", "uv run pytest -q")
    lg.test(session, passed, "uv run pytest")
    lg.end(session)


def gate(memory, lg):
    sync(memory, lg)
    memory.gate(now=lg.clock)
    return memory.world().records["r_proc"].rung.value


def frontmatter(path):
    _, front, body = path.read_text().split("---\n", 2)
    return yaml.safe_load(front), body


def test_week5_procedure_exports_at_l2_auto_loads_at_l3_and_is_removed_on_retirement(memory):
    skills = memory.cfg.skills_dir
    mine = skills / "my-skill"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: my-skill\n---\nmine\n")

    lg = Ledger()
    lg.start("s1", **UV)
    ok1 = lg.test("s1", True)
    lg.propose(
        "s1",
        "r_proc",
        scope=Scope.exact(State(**UV), broaden=frozenset({"model_id"})),
        kind=RecordKind.PROCEDURE,
        content="Run uv sync before tests",
        steps=(("Bash", "uv sync"), ("Bash", "uv run pytest")),
        acceptance_check="uv run pytest",
    )
    lg.link("r_proc", ok1)
    lg.end("s1")
    lg.start("s2", **UV)
    ok2 = lg.test("s2", True)
    lg.link("r_proc", ok2)
    lg.end("s2")
    assert gate(memory, lg) == "L2"

    first = export_skills(memory)
    skill = skills / "rka-r-proc" / "SKILL.md"
    front, body = frontmatter(skill)
    assert first.to_json() == {"written": ["rka-r-proc"], "removed": [], "unchanged": [], "skipped": []}
    assert front == {
        "name": "rka-r-proc",
        "description": "Run uv sync before tests",
        "disable-model-invocation": True,
        "metadata": {
            "record_id": "r_proc",
            "rung": "L2",
            "version": 1,
            "scope": {"task_class": "coding", "project": "/p/app", "toolchain": "uv", "user_context": "default"},
            "evidence": [ok1.id, ok2.id],
            "acceptance_check": "uv run pytest",
        },
    }
    assert body == (
        "\n# rka-r-proc\n\nRun uv sync before tests\n\n"
        "Applies when: task_class=coding, project=/p/app, toolchain=uv, user_context=default.\n\n"
        "## Steps\n\n"
        "1. Tool `Bash`, with input containing `uv sync`.\n"
        "2. Tool `Bash`, with input containing `uv run pytest`.\n\n"
        "Before finishing, run `uv run pytest`; rka-memory records the result.\n\n"
        "If this skill is wrong, the user can type `rkm reject r_proc <reason>`.\n"
    )
    assert (skills / "rka-r-proc" / ".rka-memory").read_text() == "r_proc\n"
    assert export_skills(memory).to_json()["unchanged"] == ["rka-r-proc"]

    follow(lg, "s3")
    follow(lg, "s4")
    assert gate(memory, lg) == "L3"
    promoted = export_skills(memory)
    front, _ = frontmatter(skill)
    assert promoted.written == ["rka-r-proc"]
    assert "disable-model-invocation" not in front
    assert front["metadata"]["rung"] == "L3"

    follow(lg, "s5", passed=False, skill=True)
    assert gate(memory, lg) == "L4"
    retired = export_skills(memory)
    assert retired.to_json() == {"written": [], "removed": ["rka-r-proc"], "unchanged": [], "skipped": []}
    assert not (skills / "rka-r-proc").exists()
    assert (mine / "SKILL.md").read_text() == "---\nname: my-skill\n---\nmine\n"
    assert sorted(p.name for p in skills.iterdir()) == ["my-skill"]


def test_unmarked_directory_with_the_skill_name_is_never_overwritten(memory):
    clash = memory.cfg.skills_dir / "rka-r-proc"
    clash.mkdir(parents=True)
    (clash / "SKILL.md").write_text("user owned\n")
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_proc", kind=RecordKind.PROCEDURE, content="Run uv sync before tests")
    lg.confirm("cli", "r_proc")
    lg.end("s1")
    assert gate(memory, lg) == "L2"

    result = export_skills(memory)

    assert result.written == []
    assert [s["record_id"] for s in result.skipped] == ["r_proc"]
    assert (clash / "SKILL.md").read_text() == "user owned\n"

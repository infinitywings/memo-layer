"""Reconcile skill directories with the procedure records at L2 and L3.

Claude Code loads a skill by its description and knows nothing about scope,
so where a skill is written is how its scope is enforced. A skill pinned to
projects goes into each `<project>/.claude/skills`, which Claude Code loads
only in that project; a skill with no project constraint goes into the
global `skills.dir`. A skill auto-loads only at L3 and only when every other
scope field is enforced as well; otherwise it stays user-invocable and the
scope-checked session packet carries it when it applies.

Each exported directory carries a marker file. Only marked directories are
ever rewritten or removed, so a user's own skills are never touched.
Running the export twice changes nothing the second time.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from rka_memory.config import Config
from rka_memory.domain import DELIVERABLE, SCOPE_FIELDS, RecordKind, Rung, State
from rka_memory.packet.builder import flatten, rule_text, superseded
from rka_memory.skills.gate import skill_name
from rka_memory.validity.engine import assess
from rka_memory.world import RecordState, World

if TYPE_CHECKING:
    from rka_memory.service import Memory

MARKER = ".rka-memory"
EXPORTED_RUNGS = (Rung.L2, Rung.L3)
NAME_PATTERN = re.compile(r"[a-z0-9-]{1,64}")
DESCRIPTION_MAX = 1024


@dataclass
class ExportResult:
    written: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "removed": self.removed,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
        }


def exportable(world: World, cfg: Config, state: State | None = None) -> list[RecordState]:
    """Procedures at L2 or L3 that no valid newer version supersedes."""
    hidden = superseded(world, cfg, state)
    return [
        r
        for r in world.records.values()
        if r.spec.kind is RecordKind.PROCEDURE and r.rung in EXPORTED_RUNGS and r.id not in hidden
    ]


def rules_followed(rec: RecordState, world: World, cfg: Config) -> list[str]:
    """The procedure's dependencies at L2 or above, as the skill shows them.
    One that drops below L2 leaves the skill at the next export, while the
    procedure keeps its own rung (docs/GRAPH.md, decision 3)."""
    hidden = superseded(world, cfg)
    deps = [world.records[d] for d in rec.spec.depends_on if d in world.records]
    return [rule_text(d, world, cfg) for d in deps if d.rung in DELIVERABLE and d.id not in hidden]


def version(rec: RecordState, world: World) -> int:
    """1 for an original procedure, n + 1 for one that supersedes version n."""
    n, current, seen = 1, rec, {rec.id}
    while current.spec.supersedes in world.records and current.spec.supersedes not in seen:
        current = world.records[current.spec.supersedes]
        seen.add(current.id)
        n += 1
    return n


def _code(text: str) -> str:
    """Model-proposed text rendered as inline code: one line, no backticks."""
    return flatten(text).replace("`", "'")


def _description(content: str) -> str:
    line = " ".join(content.split())
    return line if len(line) <= DESCRIPTION_MAX else line[: DESCRIPTION_MAX - 3] + "..."


PROJECT_SKILLS = Path(".claude") / "skills"
# Scope fields no skill location can enforce: they vary between sessions of
# the same project, and Claude Code loads skills before anything checks them.
SESSION_FIELDS = ("model_id", "task_class", "user_context")


@dataclass(frozen=True)
class Placement:
    root: Path
    project: str | None
    unenforced: tuple[str, ...]

    def auto_load(self, rec: RecordState) -> bool:
        return rec.rung is Rung.L3 and not self.unenforced


def _open(constraint) -> bool:
    return constraint.include is None and not constraint.exclude


def placements(rec: RecordState, cfg: Config) -> tuple[list[Placement], list[str]]:
    """Where this skill goes, what scope each location leaves unenforced, and
    the projects it cannot be written to."""
    from rka_memory.ledger.state import detect

    session_gaps = [f for f in SESSION_FIELDS if not _open(rec.scope.constraint(f))]
    project = rec.scope.project
    toolchain = rec.scope.toolchain
    if project.include is None:
        gaps = session_gaps + (["project"] if project.exclude else []) + ([] if _open(toolchain) else ["toolchain"])
        return [Placement(cfg.skills_dir, None, tuple(gaps))], []
    out, missing = [], []
    for proj in sorted(project.include):
        path = Path(proj)
        if not path.is_absolute() or not path.is_dir():
            missing.append(proj)
            continue
        gaps = list(session_gaps)
        if not _open(toolchain) and not toolchain.covers(detect(path, cfg).toolchain):
            gaps.append("toolchain")
        out.append(Placement(path / PROJECT_SKILLS, proj, tuple(gaps)))
    return out, missing


def render(rec: RecordState, world: World, cfg: Config, placement: Placement) -> str:
    name = skill_name(rec.id)
    check = rec.acceptance_check or rec.spec.acceptance_check
    described = {f: rec.scope.constraint(f).describe() for f in SCOPE_FIELDS}
    scope = {f: d for f, d in described.items() if d != "*"}
    auto = placement.auto_load(rec)
    front: dict[str, Any] = {"name": name, "description": _description(rec.spec.content)}
    if not auto:
        front["disable-model-invocation"] = True
    front["metadata"] = {
        "record_id": rec.id,
        "rung": rec.rung.value,
        "version": version(rec, world),
        "scope": scope,
        "auto_load": auto,
        "evidence": [g.event.id for g in assess(rec, world, cfg).support],
        "acceptance_check": check,
    }
    body = [f"# {name}", "", flatten(rec.spec.content), "", f"Applies when: {flatten(rec.scope.describe())}."]
    if rec.rung is Rung.L3 and placement.unenforced:
        body += [
            "",
            f"Auto-loading is off because Claude Code cannot enforce this skill's scope on "
            f"{', '.join(placement.unenforced)}. rka-memory delivers it in the session packet when it applies.",
        ]
    if rec.spec.steps:
        body += ["", "## Steps", ""]
        body += [
            f"{i}. Tool `{_code(s.tool)}`, with input containing `{_code(s.match)}`."
            for i, s in enumerate(rec.spec.steps, 1)
        ]
    rules = rules_followed(rec, world, cfg)
    if rules:
        body += ["", "## Rules this procedure follows", ""]
        body += [f"- {line}" for line in rules]
    body.append("")
    if rec.rung is Rung.L3 and rec.acceptance_check:
        body += [f"Before finishing, run `{_code(rec.acceptance_check)}`; rka-memory records the result.", ""]
    body.append(f"If this skill is wrong, the user can type `rkm reject {rec.id} <reason>`.")
    frontmatter = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=10_000)
    return f"---\n{frontmatter}---\n\n" + "\n".join(body) + "\n"


def session_skills(world: World, cfg: Config, state: State, arm: str) -> list[dict[str, str]]:
    """L3 skills valid for one session's state, for a harness whose own skill
    provider is asked per session and so enforces the full scope itself. A
    session in the `none` arm gets no memory, so no skills either."""
    from rka_memory.validity.engine import valid

    if arm == "none":
        return []
    out = []
    for rec in sorted(exportable(world, cfg, state), key=lambda r: r.id):
        if rec.rung is not Rung.L3 or not valid(rec, state, world, cfg):
            continue
        text = render(rec, world, cfg, Placement(cfg.skills_dir, None, ()))
        _, front, body = text.split("---\n", 2)
        out.append(
            {
                "record_id": rec.id,
                "name": skill_name(rec.id),
                "description": yaml.safe_load(front)["description"],
                "body": body.lstrip("\n"),
            }
        )
    return out


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _roots(world: World, cfg: Config) -> list[Path]:
    """Every skills root an export could have written to: the global one and
    each project any procedure or session has named."""
    projects = {p for r in world.records.values() if r.spec.kind is RecordKind.PROCEDURE for p in (r.scope.project.include or ())}
    projects |= {s.state.project for s in world.sessions.values() if s.state.project}
    roots = [cfg.skills_dir] + [Path(p) / PROJECT_SKILLS for p in sorted(projects) if Path(p).is_absolute()]
    return list(dict.fromkeys(roots))


def export_skills(memory: Memory) -> ExportResult:
    """Reconcile SKILL.md files with the ledger. With `skills.export_files` off
    nothing is written and earlier exports are removed: a harness that gets
    skills through its plugin (dsh) should not find them as files, which any
    session can read whatever its packet arm."""
    cfg = memory.cfg
    world = memory.world()
    result = ExportResult()
    wanted: dict[Path, tuple[RecordState, str, Placement]] = {}
    for rec in sorted(exportable(world, cfg), key=lambda r: r.id) if cfg.skills_export_files else ():
        name = skill_name(rec.id)
        if not NAME_PATTERN.fullmatch(name):
            result.skipped.append({"record_id": rec.id, "reason": f"skill name {name!r} is not a valid skill name"})
            continue
        targets, missing = placements(rec, cfg)
        for proj in missing:
            result.skipped.append({"record_id": rec.id, "reason": f"project directory {proj} not found"})
        for placement in targets:
            wanted[placement.root / name] = (rec, render(rec, world, cfg, placement), placement)

    for target, (rec, text, placement) in wanted.items():
        marker = target / MARKER
        skill = target / "SKILL.md"
        if target.exists() and not marker.is_file():
            result.skipped.append({"record_id": rec.id, "reason": f"{target} exists and is not managed by rka-memory"})
            continue
        if skill.is_file() and skill.read_text(encoding="utf-8") == text:
            result.unchanged.append(str(target))
            continue
        target.mkdir(parents=True, exist_ok=True)
        # Marker first: a crash after it leaves a directory the next run still owns.
        _write(marker, f"{rec.id}\n")
        if placement.project is not None:
            # Inside someone's repository: keep the generated skill out of git.
            _write(target / ".gitignore", "*\n")
        _write(skill, text)
        result.written.append(str(target))

    for root in _roots(world, cfg):
        if not root.is_dir():
            continue
        for target in sorted(root.iterdir()):
            if target.is_dir() and target not in wanted and (target / MARKER).is_file():
                shutil.rmtree(target)
                result.removed.append(str(target))
    return result

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

from rka_memory.config import Config
from rka_memory.domain import RecordKind, Rung
from rka_memory.kernel import views
from rka_memory.kernel.labels import Labels
from rka_memory.skills.gate import skill_name
from rka_memory.world import RecordState, World

if TYPE_CHECKING:
    from rka_memory.service import Memory

MARKER = ".rka-memory"
NAME_PATTERN = re.compile(r"[a-z0-9-]{1,64}")


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


PROJECT_SKILLS = Path(".claude") / "skills"
# Scope fields no skill location can enforce: they vary between sessions of
# the same project, and Claude Code loads skills before anything checks them.
SESSION_FIELDS = ("model_id", "task_class", "user_context")


@dataclass(frozen=True)
class Placement:
    root: Path
    project: str | None
    unenforced: tuple[str, ...]

    def auto_load(self, labels: Labels, rec: RecordState) -> bool:
        return labels.rung(rec.id) is Rung.L3 and not self.unenforced


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


def render(rec: RecordState, world: World, labels: Labels, cfg: Config, placement: Placement) -> str:
    return views.skill(rec, world, labels, cfg, auto_load=placement.auto_load(labels, rec), unenforced=placement.unenforced)


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
    labels = memory.labels(world)
    result = ExportResult()
    wanted: dict[Path, tuple[RecordState, str, Placement]] = {}
    for rec in sorted(views.exportable(world, labels, cfg), key=lambda r: r.id) if cfg.skills_export_files else ():
        name = skill_name(rec.id)
        if not NAME_PATTERN.fullmatch(name):
            result.skipped.append({"record_id": rec.id, "reason": f"skill name {name!r} is not a valid skill name"})
            continue
        targets, missing = placements(rec, cfg)
        for proj in missing:
            result.skipped.append({"record_id": rec.id, "reason": f"project directory {proj} not found"})
        for placement in targets:
            wanted[placement.root / name] = (rec, render(rec, world, labels, cfg, placement), placement)

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

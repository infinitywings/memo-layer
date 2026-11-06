"""Reconcile `skills.dir` with the procedure records at L2 and L3.

Each exported skill directory carries a marker file. Only marked directories
are ever rewritten or removed, so a user's own skills are never touched.
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
from rka_memory.domain import SCOPE_FIELDS, RecordKind, Rung
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


def exportable(world: World) -> list[RecordState]:
    return [r for r in world.records.values() if r.spec.kind is RecordKind.PROCEDURE and r.rung in EXPORTED_RUNGS]


def version(rec: RecordState, world: World) -> int:
    """1 for an original procedure, n + 1 for one that supersedes version n."""
    n, current, seen = 1, rec, {rec.id}
    while current.spec.supersedes in world.records and current.spec.supersedes not in seen:
        current = world.records[current.spec.supersedes]
        seen.add(current.id)
        n += 1
    return n


def _description(content: str) -> str:
    line = " ".join(content.split())
    return line if len(line) <= DESCRIPTION_MAX else line[: DESCRIPTION_MAX - 3] + "..."


def render(rec: RecordState, world: World, cfg: Config) -> str:
    name = skill_name(rec.id)
    check = rec.acceptance_check or rec.spec.acceptance_check
    described = {f: rec.scope.constraint(f).describe() for f in SCOPE_FIELDS}
    scope = {f: d for f, d in described.items() if d != "*"}
    front: dict[str, Any] = {"name": name, "description": _description(rec.spec.content)}
    if rec.rung is Rung.L2:
        # An L2 procedure is validated but not yet proven as a skill, so the
        # model may only use it when the user invokes it by name.
        front["disable-model-invocation"] = True
    front["metadata"] = {
        "record_id": rec.id,
        "rung": rec.rung.value,
        "version": version(rec, world),
        "scope": scope,
        "evidence": [g.event.id for g in assess(rec, world, cfg).support],
        "acceptance_check": check,
    }
    body = [f"# {name}", "", rec.spec.content, "", f"Applies when: {rec.scope.describe()}."]
    if rec.spec.steps:
        body += ["", "## Steps", ""]
        body += [f"{i}. Tool `{s.tool}`, with input containing `{s.match}`." for i, s in enumerate(rec.spec.steps, 1)]
    body.append("")
    if check:
        body += [f"Before finishing, run `{check}`; rka-memory records the result.", ""]
    body.append(f"If this skill is wrong, the user can type `rkm reject {rec.id} <reason>`.")
    frontmatter = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=10_000)
    return f"---\n{frontmatter}---\n\n" + "\n".join(body) + "\n"


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def export_skills(memory: Memory) -> ExportResult:
    cfg = memory.cfg
    world = memory.world()
    root = cfg.skills_dir
    result = ExportResult()
    wanted: dict[str, tuple[RecordState, str]] = {}
    for rec in sorted(exportable(world), key=lambda r: r.id):
        name = skill_name(rec.id)
        if not NAME_PATTERN.fullmatch(name):
            result.skipped.append({"record_id": rec.id, "reason": f"skill name {name!r} is not a valid skill name"})
            continue
        wanted[name] = (rec, render(rec, world, cfg))

    for name, (rec, text) in wanted.items():
        target = root / name
        marker = target / MARKER
        skill = target / "SKILL.md"
        if target.exists() and not marker.is_file():
            result.skipped.append({"record_id": rec.id, "reason": f"{target} exists and is not managed by rka-memory"})
            continue
        if skill.is_file() and skill.read_text(encoding="utf-8") == text:
            result.unchanged.append(name)
            continue
        target.mkdir(parents=True, exist_ok=True)
        # Marker first: a crash after it leaves a directory the next run still owns.
        _write(marker, f"{rec.id}\n")
        _write(skill, text)
        result.written.append(name)

    if root.is_dir():
        for target in sorted(root.iterdir()):
            if target.is_dir() and target.name not in wanted and (target / MARKER).is_file():
                shutil.rmtree(target)
                result.removed.append(target.name)
    return result

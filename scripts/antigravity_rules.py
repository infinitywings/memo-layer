"""Write Antigravity rule files from the plugin's skills.

Antigravity loads rules, not plugin skills, from ~/.gemini/config/rules/.
Each skill becomes one rule: `using-rka-memory` is always on and starts with
the instruction to fetch the memory, since no hook delivers it there; the
others load when the model judges them relevant. A skill's references are
appended, as a rule cannot point at other files.

Usage: uv run python scripts/antigravity_rules.py [--check]
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"
OUT = ROOT / "antigravity" / "rules"
ALWAYS_ON = "using-rka-memory"
FETCH = (
    "At the start of a task in a project, call the rka-memory `memory_packet` tool once with "
    '`state: {"project": "<absolute path of the workspace root>"}`. Its `text` is the memory for that '
    "project, in the format described below. Antigravity sessions are not recorded, so nothing you do here "
    "teaches rka-memory; the user teaches it in Claude Code or Codex, or with `rka-memory rkm ...` in a terminal.\n\n"
)


def rules() -> dict[str, str]:
    out = {}
    for skill in sorted(SKILLS.glob("*/SKILL.md")):
        _, front, body = skill.read_text().split("---\n", 2)
        meta = yaml.safe_load(front)
        name = meta["name"]
        if name == ALWAYS_ON:
            head = "---\ntrigger: always_on\n---\n"
            body = FETCH + body.lstrip()
        else:
            head = f"---\ntrigger: model_decision\ndescription: {meta['description']}\n---\n"
        for ref in sorted((skill.parent / "references").glob("*.md")):
            body = body.rstrip() + "\n\n" + ref.read_text()
        out[f"{name}.md"] = head + body.lstrip("\n")
    return out


def main(argv: list[str]) -> int:
    wanted = rules()
    if "--check" in argv:
        have = {p.name: p.read_text() for p in OUT.glob("*.md")}
        return 0 if have == wanted else 1
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.md"):
        if old.name not in wanted:
            old.unlink()
    for name, text in wanted.items():
        (OUT / name).write_text(text)
    print(f"wrote {len(wanted)} rules to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

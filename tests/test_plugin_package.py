"""The plugin files a Claude Code or Codex install reads agree with the code:
every hook the code handles is registered, with its matcher, through the
plugin's launcher, and every manifest carries the package version."""

import json
import subprocess
import tomllib
from pathlib import Path

from rka_memory.ledger import hooks
from rka_memory.ledger.install import HOOK_EVENTS

ROOT = Path(__file__).resolve().parent.parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def registered(path: str) -> dict[str, tuple[str | None, str]]:
    doc = json.loads((ROOT / path).read_text())["hooks"]
    out = {}
    for event, groups in doc.items():
        [group] = groups
        [hook] = group["hooks"]
        out[event] = (group.get("matcher"), hook["command"])
    return out


def test_the_claude_code_plugin_registers_every_hook_the_settings_install_does():
    assert registered("hooks/hooks.json") == {
        event: (matcher, f'"${{CLAUDE_PLUGIN_ROOT}}/bin/rka-memory" hook {event}') for event, matcher in HOOK_EVENTS.items()
    }


def test_the_codex_hooks_reach_a_handler_through_the_codex_adapter():
    got = registered("hooks/codex-hooks.json")
    assert set(got) == {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"}
    assert {cmd for _, cmd in got.values()} == {f'"${{PLUGIN_ROOT}}/bin/rka-memory" hook --agent codex {e}' for e in got}
    assert "apply_patch" in got["PreToolUse"][0]
    assert set(got) - {"Stop"} | {"AssistantMessage"} <= set(hooks.HANDLERS)


def test_every_manifest_carries_the_package_version_and_points_at_this_plugin():
    claude = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    codex = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    assert claude["version"] == codex["version"] == market["plugins"][0]["version"] == VERSION
    assert codex["hooks"] == "./hooks/codex-hooks.json"
    assert market["plugins"][0]["source"] == "./"
    mcp = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["rka-memory"]
    assert (mcp["command"], mcp["args"]) == ("${CLAUDE_PLUGIN_ROOT}/bin/rka-memory", ["mcp"])


def isolated_env(home: Path) -> dict[str, str]:
    """A HOME of its own, so the launcher's environment is built there and not in the user's cache."""
    uv = Path(subprocess.check_output(["which", "uv"], text=True).strip())
    return {"HOME": str(home), "PATH": f"/usr/bin:/bin:{uv.parent}"}


def test_the_launcher_runs_this_checkouts_rka_memory(tmp_path):
    out = subprocess.run([str(ROOT / "bin/rka-memory"), "--help"], capture_output=True, text=True, timeout=300,
                         env=isolated_env(tmp_path))
    assert out.returncode == 0 and "hook" in out.stdout


def test_a_copied_plugin_runs_even_when_the_source_held_a_broken_environment(tmp_path):
    copy = tmp_path / "plugin"
    subprocess.run(f"git -C {ROOT} ls-files -z | xargs -0 -I{{}} rsync -R {{}} {copy}/", shell=True, check=True,
                   cwd=ROOT, capture_output=True)
    (copy / ".venv").mkdir()  # what a copy of a checkout or a used plugin directory carries
    out = subprocess.run([str(copy / "bin/rka-memory"), "--help"], capture_output=True, text=True, timeout=300,
                         env=isolated_env(tmp_path))
    assert out.returncode == 0 and "hook" in out.stdout, out.stderr
    assert not any((copy / ".venv").iterdir())


def test_each_skill_is_named_for_its_directory_and_codex_is_told_where_they_are():
    import yaml

    skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
    assert [p.parent.name for p in skills] == ["rka-memory-doctor", "rka-memory-review", "rka-memory-rules", "using-rka-memory"]
    for path in skills:
        _, front, body = path.read_text().split("---\n", 2)
        meta = yaml.safe_load(front)
        assert meta["name"] == path.parent.name and 0 < len(meta["description"]) <= 1024
        assert body.strip()
    assert json.loads((ROOT / ".codex-plugin/plugin.json").read_text())["skills"] == "./skills/"


def test_the_antigravity_rules_are_generated_from_the_current_skills():
    out = subprocess.run(["uv", "run", "python", "scripts/antigravity_rules.py", "--check"], cwd=ROOT, timeout=120)
    assert out.returncode == 0, "run scripts/antigravity_rules.py"

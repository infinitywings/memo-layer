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


def test_the_launcher_runs_this_checkouts_rka_memory():
    out = subprocess.run([str(ROOT / "bin/rka-memory"), "--help"], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and "hook" in out.stdout

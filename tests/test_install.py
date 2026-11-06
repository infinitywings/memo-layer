import json

from rka_memory.ledger.install import install_hooks

ORIGINAL = {
    "model": "opus",
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo guard"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}],
    },
}


def ours(event: str) -> dict:
    return {"type": "command", "command": f"rka-memory hook {event}"}


def test_install_is_idempotent_and_uninstall_restores_other_hooks(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(ORIGINAL, indent=2))

    first = install_hooks(settings)
    installed_text = settings.read_text()
    assert json.loads(installed_text) == {
        "model": "opus",
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo guard"}]},
                {"matcher": "Write|Edit|MultiEdit|NotebookEdit", "hooks": [ours("PreToolUse")]},
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}],
            "SessionStart": [{"hooks": [ours("SessionStart")]}],
            "UserPromptSubmit": [{"hooks": [ours("UserPromptSubmit")]}],
            "PostToolUse": [{"matcher": "*", "hooks": [ours("PostToolUse")]}],
            "PostToolUseFailure": [{"matcher": "*", "hooks": [ours("PostToolUseFailure")]}],
            "SessionEnd": [{"hooks": [ours("SessionEnd")]}],
        },
    }
    assert first["changed"] is True
    backup = tmp_path / "settings.json.bak-rka-memory"
    assert json.loads(backup.read_text()) == ORIGINAL

    second = install_hooks(settings)
    assert second["changed"] is False
    assert settings.read_text() == installed_text

    removed = install_hooks(settings, uninstall=True)
    assert removed["changed"] is True
    assert json.loads(settings.read_text()) == ORIGINAL
    assert json.loads(backup.read_text()) == ORIGINAL


def test_install_creates_a_missing_settings_file_without_a_backup(tmp_path):
    settings = tmp_path / "nested" / "settings.json"
    result = install_hooks(settings, command="/opt/bin/rka-memory")
    assert result["backup"] is None
    hooks = json.loads(settings.read_text())["hooks"]
    assert hooks["SessionEnd"] == [{"hooks": [{"type": "command", "command": "/opt/bin/rka-memory hook SessionEnd"}]}]
    install_hooks(settings, command="/opt/bin/rka-memory", uninstall=True)
    assert json.loads(settings.read_text()) == {}


def test_harden_denies_the_agent_the_ledger_and_undo_removes_only_ours(tmp_path):
    from rka_memory.ledger.install import harden

    home = tmp_path / "home"
    ledger = home / ".rka-memory"
    ledger.mkdir(parents=True)
    mine = {"permissions": {"deny": ["Read(./.env)"]}, "sandbox": {"excludedCommands": ["docker"]}}
    hard, notes = harden(mine, ledger, home)
    assert hard["permissions"]["deny"] == ["Read(./.env)", "Edit(~/.rka-memory/**)"]
    assert hard["sandbox"] == {
        "excludedCommands": ["docker"],
        "filesystem": {"denyWrite": ["~/.rka-memory"]},
        "enabled": True,
        "allowUnsandboxedCommands": False,
        "failIfUnavailable": True,
    }
    assert notes == ["sandbox.excludedCommands ['docker'] run outside the sandbox and could still write the ledger"]
    assert harden(hard, ledger, home)[0] == hard
    undone, _ = harden(hard, ledger, home, undo=True)
    assert undone["permissions"] == {"deny": ["Read(./.env)"]}
    assert "filesystem" not in undone["sandbox"]
    outside, _ = harden({}, tmp_path / "elsewhere", home)
    assert outside["permissions"]["deny"] == [f"Edit(/{(tmp_path / 'elsewhere').resolve().as_posix()}/**)"]

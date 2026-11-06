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

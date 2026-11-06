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


def test_harden_covers_symlinked_ledgers_and_a_config_outside_the_ledger(tmp_path):
    from rka_memory.ledger.install import harden

    home = tmp_path / "home"
    real = tmp_path / "disk" / "rka"
    real.mkdir(parents=True)
    home.mkdir()
    (home / ".rka-memory").symlink_to(real)
    config = tmp_path / "etc" / "rkm.yaml"
    inside = home / ".rka-memory" / "config.yaml"
    hard, _ = harden({}, home / ".rka-memory", home, extra_files=(config, inside))
    disk, etc = "/" + real.resolve().as_posix(), "/" + config.resolve().as_posix()
    assert hard["permissions"]["deny"] == [
        "Edit(~/.rka-memory/**)", f"Edit({disk}/**)", f"Edit({etc})",
    ]
    assert hard["sandbox"]["filesystem"]["denyWrite"] == ["~/.rka-memory", disk, etc]
    undone, _ = harden(hard, home / ".rka-memory", home, undo=True, extra_files=(config, inside))
    assert "permissions" not in undone and "filesystem" not in undone["sandbox"]


def test_cli_harden_refuses_an_editable_install_and_a_config_outside_the_ledger(tmp_path, monkeypatch, capsys):
    from rka_memory import cli

    monkeypatch.setenv("RKA_MEMORY_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("RKA_MEMORY_CONFIG", str(tmp_path / "ledger" / "config.yaml"))
    settings = tmp_path / "settings.json"
    assert cli.main(["install-hooks", "--settings", str(settings), "--harden"]) == 2
    assert "non-editable install" in capsys.readouterr().err
    assert not settings.exists()
    monkeypatch.setenv("RKA_MEMORY_CONFIG", str(tmp_path / "elsewhere.yaml"))
    assert cli.main(["install-hooks", "--settings", str(settings), "--harden", "--allow-editable"]) == 2
    assert "config inside the ledger directory" in capsys.readouterr().err


def test_harden_protects_the_installed_code_and_its_launcher(tmp_path):
    from rka_memory.ledger.install import harden

    home = tmp_path / "home"
    tool = home / ".local" / "share" / "uv" / "tools" / "rka-memory"
    tool.mkdir(parents=True)
    hard, _ = harden({}, home / ".rka-memory", home, extra_files=(home / ".local" / "bin" / "rka-memory",), extra_dirs=(tool,))
    assert hard["permissions"]["deny"] == [
        "Edit(~/.rka-memory/**)",
        "Edit(~/.local/share/uv/tools/rka-memory/**)",
        "Edit(~/.local/bin/rka-memory)",
    ]


def test_cli_harden_never_writes_a_relative_launcher(tmp_path, monkeypatch, capsys):
    from rka_memory import cli
    from rka_memory.ledger import install

    monkeypatch.setenv("RKA_MEMORY_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("RKA_MEMORY_CONFIG", str(tmp_path / "ledger" / "config.yaml"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bin").mkdir()
    planted = tmp_path / "bin" / "rka-memory"
    planted.write_text("#!/bin/sh\n")
    planted.chmod(0o755)
    monkeypatch.setenv("PATH", "bin")
    monkeypatch.setattr(install, "interpreter_dirs", lambda: ())
    settings = tmp_path / "settings.json"
    assert cli.main(["install-hooks", "--settings", str(settings), "--harden", "--allow-editable", "--command-path", "rka-memory"]) == 0
    commands = {h["command"] for gs in __import__("json").loads(settings.read_text())["hooks"].values() for g in gs for h in g["hooks"]}
    assert commands == {f"{planted.resolve()} hook {e}" for e in install.HOOK_EVENTS}
    assert cli.main(["install-hooks", "--settings", str(settings), "--harden", "--allow-editable", "--command-path", "missing-cmd"]) == 2
    assert "existing absolute file" in capsys.readouterr().err

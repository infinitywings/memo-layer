"""Merge rka-memory hook entries into a Claude Code settings file, idempotently."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

FILE_TOOLS = "Write|Edit|MultiEdit|NotebookEdit"

# Hook event -> matcher (None for events that take no matcher).
HOOK_EVENTS: dict[str, str | None] = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PreToolUse": FILE_TOOLS,
    "PostToolUse": "*",
    "PostToolUseFailure": "*",
    "SessionEnd": None,
}

BACKUP_SUFFIX = ".bak-rka-memory"


def _without_ours(groups: list[Any], prefix: str) -> list[Any]:
    kept = []
    for group in groups:
        hooks = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(hooks, list):
            kept.append(group)
            continue
        others = [h for h in hooks if not (isinstance(h, dict) and str(h.get("command", "")).startswith(prefix))]
        if len(others) == len(hooks):
            kept.append(group)
        elif others:
            kept.append({**group, "hooks": others})
    return kept


def merge(settings: dict[str, Any], command: str, *, uninstall: bool = False) -> dict[str, Any]:
    """`settings` with our hook entries added (or removed); other hooks untouched."""
    prefix = f"{command} hook "
    hooks = dict(settings.get("hooks") or {})
    for event, matcher in HOOK_EVENTS.items():
        groups = _without_ours(list(hooks.get(event) or []), prefix)
        if not uninstall:
            group: dict[str, Any] = {"matcher": matcher} if matcher is not None else {}
            group["hooks"] = [{"type": "command", "command": f"{prefix}{event}"}]
            groups.append(group)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    out = dict(settings)
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out


def settings_path_rule(path: Path, home: Path) -> str:
    """A path in Claude Code's settings syntax: `~/` under home, `//` for absolute."""
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return "/" + path.as_posix()


def path_rules(path: Path, home: Path) -> list[str]:
    """Rules for the path as written and as resolved, so a symlink anywhere on
    it (or under home) cannot leave one spelling of it writable."""
    literal = path.expanduser().absolute()
    resolved = literal.resolve()
    forms = [settings_path_rule(literal, home.absolute()), settings_path_rule(resolved, home.resolve())]
    return list(dict.fromkeys(forms))


def protected_rules(ledger_dir: Path, home: Path, extra_files: tuple[Path, ...] = ()) -> tuple[list[str], list[str]]:
    """(Edit deny rules, sandbox denyWrite paths) for the ledger directory and
    any rka-memory file that lives outside it, such as a relocated config."""
    edit, write = [], []
    for rule in path_rules(ledger_dir, home):
        edit.append(f"Edit({rule}/**)")
        write.append(rule)
    ledger = ledger_dir.expanduser().resolve()
    for f in extra_files:
        if ledger in f.expanduser().resolve().parents:
            continue
        for rule in path_rules(f, home):
            edit.append(f"Edit({rule})")
            write.append(rule)
    return list(dict.fromkeys(edit)), list(dict.fromkeys(write))


def harden(
    settings: dict[str, Any],
    ledger_dir: Path,
    home: Path,
    *,
    undo: bool = False,
    extra_files: tuple[Path, ...] = (),
) -> tuple[dict[str, Any], list[str]]:
    """Keep the agent from writing the ledger, which hooks alone cannot do.

    The ledger's actor field is only trustworthy if the recorder is its only
    writer. Claude Code's Write and Edit tools are stopped by deny rules; shell
    commands only by the OS-level bash sandbox, with its escape hatch
    (`dangerouslyDisableSandbox`) closed and a missing sandbox made fatal rather
    than silently skipped. Hooks run outside the sandbox, so they still record.
    Claude Code itself already guards its settings files, user and project, from
    both the Write tool and sandboxed shells (scripts/host_hardening_check.py).
    """
    edit_rules, write_rules = protected_rules(ledger_dir, home, extra_files)
    out = json.loads(json.dumps(settings))
    perms = out.setdefault("permissions", {})
    deny = [r for r in perms.get("deny", []) if r not in edit_rules]
    sandbox = out.setdefault("sandbox", {})
    fs = sandbox.setdefault("filesystem", {})
    deny_write = [r for r in fs.get("denyWrite", []) if r not in write_rules]
    notes: list[str] = []
    if undo:
        notes.append("sandbox.enabled, allowUnsandboxedCommands and failIfUnavailable were left as they are")
    else:
        deny.extend(edit_rules)
        deny_write.extend(write_rules)
        sandbox["enabled"] = True
        sandbox["allowUnsandboxedCommands"] = False
        sandbox["failIfUnavailable"] = True
        if sandbox.get("excludedCommands"):
            notes.append(
                f"sandbox.excludedCommands {sandbox['excludedCommands']} run outside the sandbox and could still write the ledger"
            )
        if fs.get("disabled"):
            notes.append("sandbox.filesystem.disabled is true, so shell commands can still write the ledger")
    perms["deny"] = deny
    fs["denyWrite"] = deny_write
    for container, key in ((perms, "deny"), (fs, "denyWrite")):
        if not container[key]:
            del container[key]
    if not fs:
        del sandbox["filesystem"]
    if not sandbox:
        del out["sandbox"]
    if not perms:
        del out["permissions"]
    return out, notes


def install_hooks(
    settings_path: Path,
    command: str = "rka-memory",
    uninstall: bool = False,
    *,
    harden_ledger: Path | None = None,
    home: Path | None = None,
    extra_files: tuple[Path, ...] = (),
) -> dict[str, Any]:
    before = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    after = merge(before, command, uninstall=uninstall)
    notes: list[str] = []
    if harden_ledger is not None:
        after, notes = harden(after, harden_ledger, home or Path.home(), undo=uninstall, extra_files=extra_files)
    backup = settings_path.with_name(settings_path.name + BACKUP_SUFFIX)
    changed = after != before
    if changed:
        if settings_path.exists() and not backup.exists():
            shutil.copy2(settings_path, backup)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=settings_path.parent, prefix=".tmp-")
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(after, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, settings_path)
    return {
        "settings": str(settings_path),
        "backup": str(backup) if backup.exists() else None,
        "changed": changed,
        "action": "uninstall" if uninstall else "install",
        "events": [] if uninstall else list(HOOK_EVENTS),
        "hardened": harden_ledger is not None and not uninstall,
        "notes": notes,
    }

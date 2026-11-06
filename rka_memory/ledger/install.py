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


def install_hooks(settings_path: Path, command: str = "rka-memory", uninstall: bool = False) -> dict[str, Any]:
    before = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    after = merge(before, command, uninstall=uninstall)
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
    }

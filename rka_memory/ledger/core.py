"""RKA Core as the durable store: push ledger events as journal notes, pull them back.

Each event becomes one `type=log` note whose content is the event's canonical
JSON and whose `request_id` is the event id, so Core replays the original note
when an event is sent twice. Core orders notes by a creation time with
one-second resolution, so each note also carries an `rkm-seq:<n>` tag with the
event's ledger position; `pull` restores ledger order from it.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from rka_memory import config as config_mod
from rka_memory.config import Config
from rka_memory.domain import Event, canonical_json
from rka_memory.service import Memory

TAG = "rka-memory"
SEQ_TAG = "rkm-seq:"
PAGE_SIZE = 200


class CoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class SyncResult:
    """`sent` counts events Core now holds from this run, new or replayed."""

    project_id: str
    sent: int
    duplicates_skipped: int
    cursor: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PullResult:
    project_id: str
    pulled: int
    skipped: int
    path: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _client(cfg: Config) -> httpx.Client:
    return httpx.Client(base_url=cfg.core_url, timeout=30)


def _request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    try:
        return client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise CoreError(f"cannot reach RKA Core at {client.base_url}: {exc}") from exc


def _ok(resp: httpx.Response, what: str) -> httpx.Response:
    if resp.status_code >= 400:
        raise CoreError(f"{what} failed: HTTP {resp.status_code}: {resp.text[:300]}")
    return resp


def _project_id(cfg: Config) -> str:
    if not cfg.core_project_id:
        raise CoreError("no RKA Core project configured: run `rka-memory init` first")
    return cfg.core_project_id


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def init_project(memory: Memory, name: str) -> dict[str, Any]:
    """Create the Core project that stores the ledger and record it in the user config."""
    with _client(memory.cfg) as client:
        resp = _request(client, "POST", "/api/projects", json={"name": name, "description": "rka-memory event ledger"})
    project = _ok(resp, "create project").json()
    path = config_mod.user_config_path()
    raw = (yaml.safe_load(path.read_text()) if path.exists() else None) or {}
    raw["core"] = {**(raw.get("core") or {}), "project_id": project["id"]}
    _write_text(path, yaml.safe_dump(raw, sort_keys=False))
    return project


def note_for(event: Event, seq: int) -> dict[str, Any]:
    return {
        "content": canonical_json(event.to_json()),
        "type": "log",
        "source": "executor",
        "summary": f"{event.kind.value} by {event.actor.value}",
        "tags": [TAG, f"rkm-kind:{event.kind.value}", f"rkm-session:{event.session_id}", f"{SEQ_TAG}{seq:012d}"],
        "request_id": event.id,
    }


def _cursor_path(cfg: Config) -> Path:
    return cfg.ledger_dir / "core_cursor.json"


def _read_cursor(cfg: Config, project_id: str) -> int:
    try:
        data = json.loads(_cursor_path(cfg).read_text())
    except (OSError, ValueError):
        return 0
    return int(data.get("position", 0)) if data.get("project_id") == project_id else 0


def _push(client: httpx.Client, project_id: str, note: dict[str, Any]) -> None:
    """Create the note. A 409 means Core holds this request id with other tags,
    as after re-syncing a pulled ledger; that is fine only if the event matches."""
    headers = {"X-RKA-Project": project_id}
    resp = _request(client, "POST", "/api/notes", json=note, headers=headers)
    if resp.status_code != 409:
        _ok(resp, f"push {note['request_id']}")
        return
    receipt = _request(client, "GET", f"/api/notes/write-receipts/{note['request_id']}", headers=headers)
    if _ok(receipt, f"read receipt {note['request_id']}").json()["entry"]["content"] != note["content"]:
        raise CoreError(f"Core holds a different event under id {note['request_id']}")


def sync(memory: Memory) -> SyncResult:
    """Push ledger events after the cursor. The cursor advances after each success."""
    cfg = memory.cfg
    project_id = _project_id(cfg)
    events = memory.ledger.read()
    cursor = _read_cursor(cfg, project_id)
    seen = {ev.id for ev in events[:cursor]}
    sent = duplicates = 0
    with _client(cfg) as client:
        for position in range(cursor, len(events)):
            ev = events[position]
            if ev.id in seen:
                duplicates += 1
            else:
                _push(client, project_id, note_for(ev, position))
                sent += 1
                seen.add(ev.id)
            _write_text(_cursor_path(cfg), json.dumps({"project_id": project_id, "position": position + 1}))
    return SyncResult(project_id, sent, duplicates, max(cursor, len(events)))


def _parse_note(note: dict[str, Any]) -> tuple[int, Event] | None:
    try:
        event = Event.from_json(json.loads(note["content"]))
    except (KeyError, TypeError, ValueError):
        return None
    seqs = [t[len(SEQ_TAG) :] for t in note.get("tags") or () if t.startswith(SEQ_TAG)]
    seq = int(seqs[0]) if seqs and seqs[0].isdigit() else 2**63
    return seq, event


def pull(memory: Memory, *, page_size: int = PAGE_SIZE) -> PullResult:
    """Rebuild the ledger from Core into ledger.pulled.jsonl; ledger.jsonl is never touched."""
    cfg = memory.cfg
    project_id = _project_id(cfg)
    found: dict[str, tuple[int, Event]] = {}
    skipped = offset = 0
    with _client(cfg) as client:
        while True:
            resp = _request(
                client,
                "GET",
                "/api/notes",
                params={"tags": [TAG], "limit": page_size, "offset": offset, "hide_superseded": "false"},
                headers={"X-RKA-Project": project_id},
            )
            notes = _ok(resp, "list notes").json()
            for note in notes:
                parsed = _parse_note(note)
                if parsed is None:
                    skipped += 1
                else:
                    found.setdefault(parsed[1].id, parsed)
            if len(notes) < page_size:
                break
            offset += page_size
    ordered = sorted(found.values(), key=lambda p: (p[0], p[1].ts))
    path = cfg.ledger_dir / "ledger.pulled.jsonl"
    _write_text(path, "".join(json.dumps(ev.to_json(), ensure_ascii=False) + "\n" for _, ev in ordered))
    return PullResult(project_id, len(ordered), skipped, str(path))

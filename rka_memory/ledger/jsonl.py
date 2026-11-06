"""Local append-only spool. Hooks write here so a tool call never waits on the
network; `rka-memory sync` copies it into RKA Core, the durable store."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from rka_memory.domain import Event


class JsonlLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.skipped = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, events: Iterable[Event]) -> list[Event]:
        events = list(events)
        if not events:
            return []
        lines = "".join(json.dumps(e.to_json(), ensure_ascii=False) + "\n" for e in events)
        with open(self.path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.write(lines)
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        return events

    def read(self) -> list[Event]:
        return list(self.iter_from(0))

    def iter_from(self, line_no: int) -> Iterator[Event]:
        """Events from `line_no` on. A line that does not parse is skipped:
        a host and a container appending at once (file locks may not cross the
        Docker VM) can interleave one line, and one torn event must not make
        the whole ledger unreadable."""
        if not self.path.exists():
            return
        with open(self.path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i < line_no or not line.strip():
                    continue
                try:
                    yield Event.from_json(json.loads(line))
                except (ValueError, KeyError):
                    self.skipped += 1

    def line_count(self) -> int:
        if not self.path.exists():
            return 0
        with open(self.path, "rb") as fh:
            return sum(1 for _ in fh)

    @contextmanager
    def exclusive(self, name: str = "gate") -> Iterator[None]:
        """Serialize read-evaluate-append cycles across hook processes."""
        lock = self.path.parent / f"{name}.lock"
        with open(lock, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

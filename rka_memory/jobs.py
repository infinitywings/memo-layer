"""Periodic upkeep for a long-running ledger: the `memory` service's loop.

Each round runs the gate, reconciles exported skills, writes the day's
metrics file and, when a Core project is configured, syncs the ledger. A
failing step is reported and the next round tries again.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from rka_memory.domain import clock_now
from rka_memory.service import Memory


@dataclass
class RoundReport:
    at: str
    steps: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"at": self.at, "steps": self.steps, "errors": self.errors}


def run_round(memory: Memory) -> RoundReport:
    from rka_memory.audit import metrics
    from rka_memory.ledger import core
    from rka_memory.skills.export import export_skills

    report = RoundReport(at=clock_now().isoformat())
    steps: list[tuple[str, Callable[[], Any]]] = [
        ("gate", lambda: len(memory.gate())),
        ("export_skills", lambda: export_skills(memory).to_json()),
        ("metrics", lambda: str(metrics.write_daily(metrics.compute(memory.world(), memory.cfg, clock_now()), memory.cfg))),
    ]
    if memory.cfg.core_project_id:
        steps.append(("sync", lambda: core.sync(memory).to_json()))
    for name, step in steps:
        try:
            report.steps[name] = step()
        except Exception as exc:  # one failing step must not stop the others or the loop
            report.errors[name] = f"{type(exc).__name__}: {exc}"
    return report


def loop(memory: Memory, every_seconds: float, rounds: int | None = None, emit: Callable[[RoundReport], None] = print) -> None:
    done = 0
    while rounds is None or done < rounds:
        emit(run_round(memory))
        done += 1
        if rounds is None or done < rounds:
            time.sleep(every_seconds)

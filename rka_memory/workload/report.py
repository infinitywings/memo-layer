"""Per-stream report: what each session received and changed, the records at
the end, and the metrics. Live streams have no expectations, so this report is
their result; scripted streams get one too."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from rka_memory.domain import Kind
from rka_memory.world import World


def protected_attempts(after: World, sid: str, protected: Sequence[str]) -> int:
    """Tool calls in the session whose input names a protected directory: a
    denied native tool call, or a shell command that tried. Any such session
    is contaminated for an arm comparison, whether or not the attempt got
    through."""
    hits = 0
    for ev in after.sessions[sid].events:
        if ev.kind is Kind.TOOL_CALL:
            text = json.dumps(ev.payload.get("input"), ensure_ascii=False)
            if any(root in text for root in protected):
                hits += 1
    return hits


def session_row(label: str, at: str, sid: str | None, before: World, after: World, protected: Sequence[str] = ()) -> dict[str, Any]:
    row: dict[str, Any] = {"session": label, "at": at, "session_id": sid}
    sess = after.sessions.get(sid) if sid else None
    if sess is None:
        return row
    deliveries = [d for d in after.deliveries if d.event.session_id == sid]
    start = next((d for d in deliveries if d.channel == "session_start"), None)
    end = sess.ended.payload if sess.ended else {}
    moves = {}
    for rid, rec in after.records.items():
        was = before.records.get(rid)
        old = was.rung.value if was and was.rung else None
        new = rec.rung.value if rec.rung else None
        if old != new:
            moves[rid] = f"{old or 'new'} -> {new or 'pending'}"
    row.update(
        task_class=sess.state.task_class,
        toolchain=sess.state.toolchain,
        arm=start.arm if start else None,
        delivered=sorted({rid for d in deliveries for rid in d.record_ids}),
        tokens_total=end.get("tokens_total"),
        tokens_before_first_write=end.get("tokens_before_first_write") if end.get("first_write_seen") else None,
        proposed=sorted(rid for rid in after.records if rid not in before.records),
        rung_changes=moves,
        protected_path_attempts=protected_attempts(after, sid, protected) if protected else 0,
    )
    return row


def records(world: World) -> list[dict[str, Any]]:
    order = {r: i for i, r in enumerate(world.records)}
    rows = sorted(world.records.values(), key=lambda r: (-(r.rung.order if r.rung else -2), order[r.id]))
    return [
        {
            "id": r.id,
            "rung": r.rung.value if r.rung else None,
            "kind": r.spec.kind.value,
            "content": r.spec.content,
            "scope": r.scope.describe(),
        }
        for r in rows
    ]


def by_arm(sessions: list[dict[str, Any]]) -> dict[tuple[str, str], tuple[int, int]]:
    """(observation, arm) -> (sessions observed, sessions in which it held)."""
    out: dict[tuple[str, str], tuple[int, int]] = {}
    for s in sessions:
        for label, held in (s.get("observed") or {}).items():
            n, k = out.get((label, s.get("arm") or "none"), (0, 0))
            out[(label, s.get("arm") or "none")] = (n + 1, k + int(bool(held)))
    return dict(sorted(out.items()))


def _cell(value: Any) -> str:
    if value is None or value == [] or value == {}:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {v}" for k, v in value.items())
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return lines


def render(report: dict[str, Any]) -> str:
    m = report.get("metrics") or {}
    lines = [f"# Stream {report['stream']} ({report['model']} model)", ""]
    if report.get("description"):
        lines += [report["description"], ""]
    lines += [f"Model route: {report['route']}. Miner: {report['miner']}.", "", "## Sessions", ""]
    lines += _table(
        ["session", "simulated time", "task", "toolchain", "arm", "delivered", "tokens", "tokens before first write", "proposed", "rung changes", "observed", "protected path attempts"],
        [
            [s["session"], s["at"], s.get("task_class"), s.get("toolchain"), s.get("arm"), s.get("delivered"),
             s.get("tokens_total"), s.get("tokens_before_first_write"), s.get("proposed"), s.get("rung_changes"),
             {k: "yes" if v else "no" for k, v in (s.get("observed") or {}).items()}, s.get("protected_path_attempts")]
            for s in report["sessions"]
        ],
    )
    rules = by_arm(report["sessions"])
    if rules:
        lines += ["", "## Observations by arm", "", "How often each observation held, by the arm of the session's packet.", ""]
        lines += _table(["observation", "arm", "sessions", "held"], [[label, arm, n, held] for (label, arm), (n, held) in rules.items()])
    lines += ["", "## Records at the end", ""]
    lines += _table(["rung", "kind", "id", "content", "scope"], [[r["rung"], r["kind"], r["id"], r["content"], r["scope"]] for r in report["records"]])
    lines += ["", "## Metrics", ""]
    lines += [f"Records by rung: {_cell(m.get('records_by_rung'))}.", ""]
    rediscovery = m.get("rediscovery_tokens") or {}
    lines += _table(
        ["arm", "sessions with a write", "mean tokens before first write", "median"],
        [[arm, v.get("count"), v.get("mean"), v.get("median")] for arm, v in rediscovery.items()],
    )
    mis = (m.get("misapplication_rate") or {}).get("overall") or {}
    cost = (m.get("validation_cost") or {}).get("totals") or {}
    lines += [
        "",
        f"Misapplication: {mis.get('misapplied', 0)} of {mis.get('sessions', 0)} sessions given a packet. "
        f"Regressions: {(m.get('regression_count') or {}).get('count', 0)}. "
        f"Validation cost: {_cell(cost)}.",
        "",
        f"Harness checks: {report['checks_passed']} of {report['checks']} passed.",
        "",
    ]
    tainted = [s["session"] for s in report["sessions"] if s.get("protected_path_attempts")]
    if tainted:
        lines += [f"Sessions whose tools named a protected path (contaminated for arm comparisons): {', '.join(tainted)}.", ""]
    return "\n".join(lines)

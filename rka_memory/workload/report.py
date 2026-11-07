"""Per-stream report: what each session received and changed, the records at
the end, and the metrics. Live streams have no expectations, so this report is
their result; scripted streams get one too."""

from __future__ import annotations

from typing import Any

from rka_memory.world import World


def session_row(label: str, at: str, sid: str | None, before: World, after: World) -> dict[str, Any]:
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
        ["session", "simulated time", "task", "toolchain", "arm", "delivered", "tokens", "tokens before first write", "proposed", "rung changes"],
        [
            [s["session"], s["at"], s.get("task_class"), s.get("toolchain"), s.get("arm"), s.get("delivered"),
             s.get("tokens_total"), s.get("tokens_before_first_write"), s.get("proposed"), s.get("rung_changes")]
            for s in report["sessions"]
        ],
    )
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
    return "\n".join(lines)

"""Drive the rka-memory MCP server over stdio with a real MCP client.

Seeds one validated record, then lists tools, calls memory_packet, and
calls memory_confirm to check that a model-side confirm grounds nothing.
Usage: uv run python scripts/e2e_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent
EXPECTED = {"memory_packet", "memory_hints", "memory_confirm", "memory_reject", "memory_explain", "memory_state"}


async def run(tmp: Path, project: Path) -> list[bool]:
    params = StdioServerParameters(
        command=str(REPO / ".venv" / "bin" / "rka-memory"),
        args=["mcp"],
        cwd=str(project),
        env={**os.environ, "RKA_MEMORY_DIR": str(tmp / "ledger"), "RKA_MEMORY_CONFIG": str(tmp / "config.yaml")},
    )
    results = []

    def check(name: str, ok: bool, detail: object = "") -> None:
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if not ok else ""))

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            check("server exposes exactly the six spec tools", tools == EXPECTED, tools)
            res = await session.call_tool("memory_packet", {"session_id": "mcp-e2e"})
            text = "".join(c.text for c in res.content if hasattr(c, "text"))
            check("memory_packet returns the validated record", "r_e2e" in text, text[:300])
            res = await session.call_tool("memory_confirm", {"record_id": "r_e2e"})
            text = "".join(c.text for c in res.content if hasattr(c, "text"))
            check("memory_confirm asks the user to type the command", "rkm confirm" in text, text[:300])
    return results


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rkm-mcp-"))
    project = (tmp / "proj")
    project.mkdir()
    (project / "uv.lock").write_text("")
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    project = project.resolve()
    (tmp / "config.yaml").write_text("packet:\n  arms: [gated]\nminer:\n  backend: none\n")
    os.environ["RKA_MEMORY_DIR"] = str(tmp / "ledger")
    os.environ["RKA_MEMORY_CONFIG"] = str(tmp / "config.yaml")
    sys.path.insert(0, str(REPO))
    from rka_memory.domain import Actor, Event, Kind, RecordKind, RecordSpec, Scope, State
    from rka_memory.service import Memory

    mem = Memory.open()
    state = State(project=str(project), toolchain="uv", user_context="default")
    for i, sid in enumerate(("seed-1", "seed-2")):
        ok = Event.new(Kind.TEST_RESULT, Actor.TOOL, sid, {"passed": True, "command": "uv run pytest"})
        evs = [Event.new(Kind.SESSION_START, Actor.RUNNER, sid, {"state": state.to_json()}), ok]
        if i == 0:
            evs.append(Event.new(Kind.RECORD_PROPOSED, Actor.MODEL, sid, RecordSpec("r_e2e", RecordKind.FACT, "Tests run with uv run pytest", Scope.exact(state)).to_json()))
        evs.append(Event.new(Kind.EVIDENCE_LINKED, Actor.MODEL, sid, {"record_id": "r_e2e", "event_id": ok.id, "polarity": "support"}))
        mem.append(*evs)
    mem.gate()
    results = asyncio.run(run(tmp, project))
    world = mem.world()
    confirms = [e for e in world.events.values() if e.kind is Kind.USER_CONFIRM]
    requests = [e for e in world.events.values() if e.kind is Kind.JUDGMENT and e.payload.get("target") == "verdict_request"]
    ok = not confirms and len(requests) == 1 and requests[0].actor is Actor.MODEL
    print(f"{'PASS' if ok else 'FAIL'}  model-side confirm logged as a model judgment, not a user_confirm")
    mcp_deliveries = [d for d in world.deliveries if d.channel == "mcp"]
    ok2 = len(mcp_deliveries) == 1 and "r_e2e" in mcp_deliveries[0].record_ids
    print(f"{'PASS' if ok2 else 'FAIL'}  MCP delivery logged with record ids")
    return 0 if all(results) and ok and ok2 else 1


if __name__ == "__main__":
    raise SystemExit(main())

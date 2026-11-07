"""`rka-memory` command line. Handlers import lazily so each stays cheap."""

from __future__ import annotations

import argparse
import json
import sys
from rka_memory.domain import clock_now
from pathlib import Path


def _memory():
    from rka_memory.service import Memory

    return Memory.open()


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def cmd_hook(args) -> int:
    from rka_memory.ledger import hooks

    return hooks.main(args.event, sys.stdin.read())


def cmd_gate(args) -> int:
    moves = _memory().gate()
    _print([{"record_id": m.payload["record_id"], "kind": m.kind.value, **m.payload} for m in moves])
    return 0


def cmd_mine(args) -> int:
    from rka_memory.miner import miner

    result = miner.mine_session(_memory(), args.session_id, force=args.force)
    _print(result.to_json())
    return 0


def cmd_export_skills(args) -> int:
    from rka_memory.skills import export

    _print(export.export_skills(_memory()).to_json())
    return 0


def cmd_metrics(args) -> int:
    from rka_memory.audit import metrics

    mem = _memory()
    report = metrics.compute(mem.world(), mem.cfg, clock_now())
    if args.write:
        path = metrics.write_daily(report, mem.cfg)
        print(f"wrote {path}", file=sys.stderr)
    _print(report)
    return 0


def cmd_status(args) -> int:
    world = _memory().world()
    rows = sorted(world.records.values(), key=lambda r: (-(r.rung.order if r.rung else -2), r.id))
    for r in rows:
        rung = r.rung.value if r.rung else "-"
        print(f"{rung:4} {r.id}  {r.spec.kind.value:10}  {r.spec.content[:90]}")
    print(f"\n{len(world.events)} events, {len(world.sessions)} sessions, {len(world.records)} records", file=sys.stderr)
    if world.anomalies:
        print(f"{len(world.anomalies)} skipped rung changes (gate races): run `rka-memory replay`", file=sys.stderr)
    return 0


def cmd_explain(args) -> int:
    _print(_memory().explain(args.record_id))
    return 0


def cmd_verdict(args) -> int:
    mem = _memory()
    ev = mem.verdict(args.record_id, confirm=args.command == "confirm", reason=getattr(args, "reason", ""))
    moves = mem.gate()
    _print({"event_id": ev.id, "moves": [m.payload for m in moves if m.payload.get("record_id") == args.record_id]})
    return 0


def cmd_packet(args) -> int:
    from rka_memory.ledger import state as state_mod

    mem = _memory()
    st = state_mod.detect(Path(args.cwd).resolve(), mem.cfg, model_id=args.model)
    from rka_memory.packet import builder

    world = mem.world()
    arm = args.arm or builder.arm_for(clock_now().astimezone().date(), mem.cfg)
    pkt = builder.build(world, mem.cfg, st, arm=arm, channel="preview")
    print(pkt.render() or f"(empty packet; arm={arm}, state={st.to_json()})")
    return 0


def cmd_replay(args) -> int:
    from rka_memory.world import project

    mem = _memory()
    world = project(mem.ledger.read())
    if args.session:
        sess = world.sessions[args.session]
        for ev in sess.events:
            print(json.dumps(ev.to_json(), ensure_ascii=False))
        return 0
    _print(
        {
            "events": len(world.events),
            "sessions": len(world.sessions),
            "records": {r.id: r.rung.value if r.rung else None for r in world.records.values()},
            "anomalies": world.anomalies,
        }
    )
    return 0


def cmd_sync(args) -> int:
    from rka_memory.ledger import core

    _print(core.sync(_memory()).to_json())
    return 0


def cmd_pull(args) -> int:
    from rka_memory.ledger import core

    _print(core.pull(_memory()).to_json())
    return 0


def cmd_init(args) -> int:
    from rka_memory.ledger import core

    _print(core.init_project(_memory(), name=args.name))
    return 0


def cmd_install_hooks(args) -> int:
    from rka_memory.ledger import install

    target = Path(args.settings).expanduser()
    ledger, files, dirs = None, (), ()
    if args.harden:
        from rka_memory import config as config_mod

        ledger = config_mod.load().ledger_dir
        config_file = config_mod.user_config_path()
        if ledger.resolve() not in config_file.resolve().parents:
            print(f"--harden needs the config inside the ledger directory ({ledger}); it is at {config_file}", file=sys.stderr)
            return 2
        env, contained = install.installed_code()
        if not contained and not args.allow_editable:
            print(
                "--harden needs a non-editable install: this rka-memory runs source from a checkout an agent may edit. "
                "Install it with `uv tool install <path-to-rka-memory>` and run that copy.",
                file=sys.stderr,
            )
            return 2
        import shutil

        launcher = Path(args.command_path).expanduser()
        if not launcher.is_absolute():
            # `which` returns a relative path when PATH has a relative entry, and
            # a relative hook command would resolve inside the project.
            found = shutil.which(args.command_path)
            launcher = Path(found).resolve() if found else launcher
        if not launcher.is_absolute() or not launcher.is_file():
            print(f"--harden needs --command-path to name an existing absolute file; got {args.command_path!r}", file=sys.stderr)
            return 2
        args.command_path = str(launcher)
        files = (launcher,)
        dirs = install.interpreter_dirs()
    result = install.install_hooks(
        target, command=args.command_path, uninstall=args.uninstall, harden_ledger=ledger, extra_files=files, extra_dirs=dirs
    )
    _print(result)
    if not args.harden and not args.uninstall:
        print(
            "warning: without --harden the agent's Bash and Write tools can append to the ledger and forge events",
            file=sys.stderr,
        )
    return 0


def cmd_skills(args) -> int:
    """L3 skills valid for a session, for harness plugins that load skills
    themselves. With --session the offer is logged as a delivery."""
    from rka_memory.domain import Actor, Event, Kind, clock_now
    from rka_memory.ledger import state as state_mod
    from rka_memory.packet.builder import arm_for, session_arm
    from rka_memory.skills.export import session_skills

    mem = _memory()
    world = mem.world()
    sess = world.sessions.get(args.session) if args.session else None
    if sess is not None and sess.started is not None:
        st = sess.state
    else:
        st = state_mod.detect(Path(args.cwd).resolve(), mem.cfg)
    known = sess is not None and sess.started is not None
    today = clock_now().astimezone().date()
    arm = session_arm(world, args.session, mem.cfg, today) if known else arm_for(today, mem.cfg)
    skills = session_skills(world, mem.cfg, st, arm)
    if known and skills:
        mem.append(
            Event.new(
                Kind.PACKET_DELIVERED,
                Actor.RUNNER,
                args.session,
                {"record_ids": [s["record_id"] for s in skills], "arm": arm, "channel": "skills"},
                state_hash=st.hash(),
            )
        )
    _print(skills)
    return 0


def cmd_jobs(args) -> int:
    from rka_memory import jobs

    jobs.loop(_memory(), args.every, rounds=1 if args.once else None, emit=lambda r: _print(r.to_json()))
    return 0


def cmd_workload_run(args) -> int:
    from rka_memory.workload import runner
    from rka_memory.workload.stream import StreamError

    try:
        return runner.run_all(Path(args.path), Path(args.out), args.dsh)
    except (StreamError, FileNotFoundError, ValueError) as exc:
        print(f"rka-memory workload: {exc}", file=sys.stderr)
        return 2


def cmd_mcp(args) -> int:
    from rka_memory.mcp import server

    server.main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rka-memory", description="Validity-gated memory for Claude Code agents")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("hook", help="Claude Code hook entrypoint (reads hook JSON on stdin)")
    s.add_argument("event")
    s.set_defaults(fn=cmd_hook)

    sub.add_parser("gate", help="run the validity engine and persist rung changes").set_defaults(fn=cmd_gate)

    s = sub.add_parser("mine", help="propose candidates from one session's events")
    s.add_argument("session_id")
    s.add_argument("--force", action="store_true", help="mine even if this session was mined before")
    s.set_defaults(fn=cmd_mine)

    sub.add_parser("export-skills", help="write SKILL.md files for L2/L3 procedures").set_defaults(
        fn=cmd_export_skills
    )

    s = sub.add_parser("metrics", help="compute the C6 counters from the ledger")
    s.add_argument("--write", action="store_true", help="also write the daily metrics JSON file")
    s.set_defaults(fn=cmd_metrics)

    sub.add_parser("status", help="list records by rung").set_defaults(fn=cmd_status)

    s = sub.add_parser("explain", help="rung history, scope and evidence of one record")
    s.add_argument("record_id")
    s.set_defaults(fn=cmd_explain)

    s = sub.add_parser("confirm", help="user confirm: strong grounding (and override) for a record")
    s.add_argument("record_id")
    s.set_defaults(fn=cmd_verdict)

    s = sub.add_parser("reject", help="user reject: a counter-event for a record")
    s.add_argument("record_id")
    s.add_argument("reason")
    s.set_defaults(fn=cmd_verdict)

    s = sub.add_parser("packet", help="preview the packet a session in CWD would receive")
    s.add_argument("--cwd", default=".")
    s.add_argument("--model", default=None)
    s.add_argument("--arm", choices=["gated", "none", "ungated"], default=None)
    s.set_defaults(fn=cmd_packet)

    s = sub.add_parser("replay", help="rebuild the projection from the ledger alone")
    s.add_argument("--session", default=None, help="print one session's events")
    s.set_defaults(fn=cmd_replay)

    sub.add_parser("sync", help="push unsynced ledger events into RKA Core").set_defaults(fn=cmd_sync)
    sub.add_parser("pull", help="rebuild the local ledger from RKA Core").set_defaults(fn=cmd_pull)

    s = sub.add_parser("init", help="create the RKA Core project that stores the ledger")
    s.add_argument("--name", default="rka-memory ledger")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("install-hooks", help="add rka-memory hooks to a Claude Code settings file")
    s.add_argument("--settings", default="~/.claude/settings.json")
    s.add_argument("--command-path", default="rka-memory", help="how hooks should invoke rka-memory")
    s.add_argument("--uninstall", action="store_true")
    s.add_argument(
        "--harden",
        action="store_true",
        help="also deny the agent write access to the ledger and the installed code (permission rule + OS bash sandbox)",
    )
    s.add_argument("--allow-editable", action="store_true", help="tests only: harden an editable install anyway")
    s.set_defaults(fn=cmd_install_hooks)

    s = sub.add_parser("skills", help="L3 skills valid for a session (for harness plugins)")
    s.add_argument("--session", default=None)
    s.add_argument("--cwd", default=".")
    s.set_defaults(fn=cmd_skills)

    s = sub.add_parser("jobs", help="periodic gate, skill export, daily metrics and sync")
    s.add_argument("--every", type=float, default=300.0, help="seconds between rounds")
    s.add_argument("--once", action="store_true")
    s.set_defaults(fn=cmd_jobs)

    sub.add_parser("mcp", help="run the MCP server (stdio)").set_defaults(fn=cmd_mcp)

    s = sub.add_parser("workload", help="task streams through dsh with a scripted model (docs/WORKLOAD.md)")
    wsub = s.add_subparsers(dest="workload_command", required=True)
    r = wsub.add_parser("run", help="run stream files; exit non-zero if any expectation fails")
    r.add_argument("path", help="a stream file, or a directory of them")
    r.add_argument("--out", default="workload-runs", help="results go to a new timestamped directory here")
    r.add_argument("--dsh", default=None, help="dsh executable (default: $DSH_BIN, then dsh on PATH)")
    r.set_defaults(fn=cmd_workload_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

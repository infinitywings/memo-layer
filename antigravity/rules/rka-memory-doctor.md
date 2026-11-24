---
trigger: model_decision
description: Use when rka-memory seems not to work (no notes appear, nothing is learned, hook errors), when the user asks how rka-memory is set up, where their session text goes, how to turn mining off, harden or uninstall it.
---
# Diagnosing rka-memory

## Run the doctor

`rka-memory doctor` is the one rka-memory command you may run. It reports
counts and times only, never record text or session content:

```bash
rka-memory doctor 2>/dev/null || ~/.local/share/rka-memory/memo-layer/bin/rka-memory doctor
```

Read its lines against the table in `references/symptoms.md` and tell the
user what it shows and what to do. Do not read `~/.rka-memory` yourself: a
hook denies it, and the error log can quote session content. When the user
needs a log, ask them to open it.

## Facts to give when asked

- **What is recorded.** Hooks log the session's prompts, tool calls and
  results, exit codes, test results, file writes, and the agent's replies to
  `~/.rka-memory/ledger.jsonl` on this machine. Nothing leaves the machine
  except for mining.
- **Where session text goes.** After a session ends, a miner reads it and
  proposes lessons. It runs the `claude` CLI (Claude Code's login, model
  haiku) if installed, else the `codex` CLI (Codex's login). The session's
  text is sent to that provider. To turn mining off, the user puts this in
  `~/.rka-memory/config.yaml`:

  ```yaml
  miner:
    backend: none
  ```

  Ask the user to make this edit: the hook denies your tools the
  `~/.rka-memory` directory.
- **Codex skills.** Learned procedures are written as skills for Claude
  Code. For Codex too, the user adds `skills: {codex_dir: ~/.agents/skills}`
  to the same config file.
- **What the plugin install does not protect.** Your shell commands could
  still reach `~/.rka-memory` by a route the hook does not recognize. The
  hardened install (`rka-memory install-hooks --harden`, in the project's
  README) adds an OS sandbox rule in Claude Code that closes it.
- **Uninstall.** The "Uninstall" section of INSTALL.md in
  `~/.local/share/rka-memory/memo-layer/`. The ledger stays unless the user
  deletes it; it is everything rka-memory has learned.

# Reading `rka-memory doctor`

| Line shows | Meaning | What the user does |
|---|---|---|
| `events: 0` after a session | The hooks did not run | Claude Code: check `claude plugin list` shows rka-memory enabled, then start a new session. Codex: type `/hooks` in codex and trust the rka-memory hooks. Antigravity records no sessions. |
| `last session start` is old | The hooks stopped running in recent sessions | As above; also check that `uv --version` works in a new terminal |
| `ended sessions mined: 0 of N` | Mining has not run or fails | Check the `miner:` line. Mining runs in the background after a session ends and catches up at the next session start. |
| `miner: auto -> none` | Neither `claude` nor `codex` is on PATH | Install one, or set `miner.backend` in `~/.rka-memory/config.yaml` |
| `hook errors: N; latest ... in <event> (<type>)` | A hook raised an error | The user opens `~/.rka-memory/hook-errors.log` and reports the latest entry, or files it on the project's GitHub issues. The hook never blocks the agent; it logs and returns. |
| `records by rung: none` after several sessions | Nothing was learned yet | Normal at first: a lesson needs evidence you did not write, such as passing tests or the user's own words |
| `records by rung: L1 n` but no notes appear | Lessons wait for validation | L1 records are not delivered. They reach L2 when the user confirms one (`rkm confirm <id>`), states it as a standing rule, or later sessions ground it |
| `guard: off` | The tripwire is off | `hooks.guard: true` in `~/.rka-memory/config.yaml` turns it back on |
| `RKA Core sync: not configured` | No durable copy in RKA Core | Optional. `rka-memory init` creates the RKA Core project when the user runs RKA Core |

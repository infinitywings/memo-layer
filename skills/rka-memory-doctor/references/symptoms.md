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

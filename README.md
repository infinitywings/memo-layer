# rka-memory

A memory layer for Claude Code that lets an agent improve across sessions, but only through records whose validity is established by evidence the agent did not write.

    valid(r, s) = grounded(r) AND applicable(r, s)

A record is grounded when it cites at least one event from a test, an exit code, a tool, or the user, never the model's own text. It is applicable when its scope covers the current state (project, toolchain, model, task class, user context) and no counter-event inside that scope has arrived since. Only valid records reach the agent. Everything else stays in the ledger for audit.

The spec is [docs/SPEC.md](docs/SPEC.md). The event schema is [docs/LEDGER.md](docs/LEDGER.md). The promotion rules and every departure from the spec are in [docs/LADDER.md](docs/LADDER.md).

## How it works

1. **Ledger (C1).** Claude Code hooks append events to `~/.rka-memory/ledger.jsonl`: prompts, tool calls and results, exit codes, test results, and file writes. At the next session start, the hooks also detect user edits and undos of files the agent wrote, and repeated instructions. The adapter sets each event's actor, so model text can never pose as a user signal. `rka-memory sync` copies the ledger into an RKA Core project, which is the durable store.
2. **Miner (C2).** When a session ends, an LLM pass proposes candidate records (facts, preferences, procedures, constraints) that cite event ids. Code drops citations of model-authored events, and a candidate with none left is filed at L0D and never shown. Duplicates merge into the existing record as new evidence.
3. **Validity engine (C3).** A deterministic job moves records up and down the ladder: L0D, L1 (grounded), L2 (validated), L3 (skill), L4 (retired). Every move is a `rung_change` event with cause ids, so replaying the ledger reproduces every rung.
4. **Packet (C4).** At session start, the hook injects the L2 and L3 records valid for the current state, cut to a token budget, and logs which ids it delivered. L1 records are available as unvalidated hints over MCP.
5. **Skills (C5).** A procedure at L2 is written as `rka-<id>/SKILL.md` with auto-loading off: into `<project>/.claude/skills` when its scope pins a project, else into `~/.claude/skills`. At L3 it auto-loads, but only when Claude Code can enforce the rest of its scope. If its acceptance check fails afterwards, it retires and the file is removed.
6. **Metrics (C6).** `rka-memory metrics` computes misapplication rate, regression count, rediscovery tokens (with and without a packet, alternating days), and validation cost from the ledger alone.

## Install

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), and a running RKA Core 3.0 (for sync only; the hooks work without it).

```bash
uv tool install --editable /Volumes/FuSpace/Projects/memo-layer
```

Create the RKA Core project that stores the ledger. The command writes its id into `~/.rka-memory/config.yaml`:

```bash
rka-memory init
```

Add the hooks to your Claude Code user settings. The command backs up the file first and leaves your other hooks alone:

```bash
rka-memory install-hooks --settings ~/.claude/settings.json
```

Register the MCP server:

```bash
claude mcp add -s user rka-memory -- rka-memory mcp
```

The miner calls `claude -p`, so the `claude` CLI must be logged in.

## Daily use

Work as usual. To give the memory a verdict, type one of these as your whole prompt in Claude Code. The hook records it as your verdict and does not send it to the model:

    rkm confirm <record-id>
    rkm reject <record-id> <reason>

The same verdicts work from a terminal:

```bash
rka-memory confirm <record-id>
```

```bash
rka-memory reject <record-id> "we moved to tox"
```

Inspect what the memory holds and why:

```bash
rka-memory status
```

```bash
rka-memory explain <record-id>
```

```bash
rka-memory packet --cwd .
```

Other commands are `gate`, `mine <session-id>`, `export-skills`, `metrics --write`, `replay`, `sync`, and `pull`. Run `rka-memory --help` for details.

## Configuration

Defaults live in [config/default.yaml](config/default.yaml). Put overrides in `~/.rka-memory/config.yaml`. You can tune the promotion threshold `k`, the signal weights, the packet budget, the A/B arms, the task classes, and the retirement window. Set `RKA_MEMORY_CONTEXT` (for example `work` or `personal`) to scope records by user context.

## Development

```bash
uv sync
```

```bash
uv run pytest -q
```

End-to-end checks through real Claude Code hooks and a real MCP client:

```bash
uv run python scripts/e2e_claude_hooks.py
```

```bash
uv run python scripts/e2e_mcp.py
```

MIT license.

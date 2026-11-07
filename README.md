# rka-memory

A memory layer for Claude Code that lets an agent improve across sessions, but only through records whose validity is established by evidence the agent did not write.

    valid(r, s) = grounded(r) AND applicable(r, s)

A record is grounded when it cites at least one event from a test, an exit code, a tool, or the user, never the model's own text. It is applicable when its scope covers the current state (project, toolchain, model, task class, user context) and no counter-event inside that scope has arrived since. Only valid records reach the agent. Everything else stays in the ledger for audit.

The spec is [docs/SPEC.md](docs/SPEC.md). The event schema is [docs/LEDGER.md](docs/LEDGER.md). The promotion rules and every departure from the spec are in [docs/LADDER.md](docs/LADDER.md). The proposal to treat the memory as a knowledge graph is [docs/GRAPH.md](docs/GRAPH.md).

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
uv tool install /Volumes/FuSpace/Projects/memo-layer
```

This installs a copy, not an editable link: the hooks must not run code that a Claude Code session in this repository could edit. After changing the code, reinstall with `uv tool install --reinstall /Volumes/FuSpace/Projects/memo-layer`.

Create the RKA Core project that stores the ledger. The command writes its id into `~/.rka-memory/config.yaml`:

```bash
rka-memory init
```

Add the hooks to your Claude Code user settings. The command backs up the file first and leaves your other hooks alone. `--harden` also keeps the agent from writing the ledger: an `Edit` deny rule for `~/.rka-memory` plus Claude Code's OS-level bash sandbox with that directory denied and the unsandboxed escape hatch off. Without it, a prompt-injected agent can forge ledger events, including your verdicts (see the trust boundary in [docs/LADDER.md](docs/LADDER.md)). The sandbox applies to every Claude Code session, including its network isolation, so commands that reach the network may ask first.

```bash
rka-memory install-hooks --settings ~/.claude/settings.json --harden --command-path ~/.local/bin/rka-memory
```

Register the MCP server:

```bash
claude mcp add -s user rka-memory -- rka-memory mcp
```

The miner calls `claude -p`, so the `claude` CLI must be logged in.

### DeepSeek Harness (dsh)

rka-memory also runs inside dsh (0.2.2-rc.1 or later) through the plugin in [plugins/dsh](plugins/dsh/README.md). It records the same ledger events, keeps the packet in the system prompt for the whole session, and registers only the L3 skills valid for the session's state, so dsh enforces skill scope that Claude Code cannot.

dsh needs Node 24.9 or later. The Docker image provides it, with dsh, the plugin and rka-memory preinstalled. Build it once from this repository:

```bash
docker build -f docker/dsh/Dockerfile -t rka-memory-dsh .
```

Then run dsh from any project directory with `scripts/dsh-docker` (pass your model key, for example `DEEPSEEK_API_KEY`, in the environment):

```bash
/Volumes/FuSpace/Projects/memo-layer/scripts/dsh-docker tui
```

The image runs the agent's tool processes as an unprivileged `agent` user (`RKM_CONFINE=0` turns that off if the agent cannot write the mounted project). The wrapper mounts the project's git root at the same absolute path, so dsh sessions and Claude Code sessions share record scopes. It also mounts `~/.rka-memory`, so both feed one ledger, and points sync at `host.docker.internal:9712`. The agent's shell commands run inside the container, in dsh's tool sandbox, which keeps the agent from writing the ledger; only the plugin writes it (see the trust boundary in [docs/LADDER.md](docs/LADDER.md)). Never approve a sandbox escalation: it gives the command root in the container. Run the wrapper from inside a project; it refuses to start when the project contains the ledger directory (for example from your home directory). The image has no `claude` CLI, so the miner cannot run there. A dsh session is mined at the next Claude Code session start on the host, which catches up unmined sessions. On a Linux host the container writes as root into the mounted directories; Docker Desktop on macOS maps writes to your user.

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

`explain` shows a record's evidence, rung history, relations to other records and every delivery that carried it. `rka-memory graph` prints the ledger as typed nodes and edges in JSON (`--record <id>` for one record's neighborhood). `rka-memory graph --trace` checks that every delivered record traces to grounding evidence linked before the delivery, and exits 1 if one does not.

Other commands are `gate`, `mine <session-id>`, `export-skills`, `metrics --write`, `replay`, `sync`, and `pull`. Run `rka-memory --help` for details.

## Configuration

Defaults live in [rka_memory/default.yaml](rka_memory/default.yaml). Put overrides in `~/.rka-memory/config.yaml`. You can tune the promotion threshold `k`, the signal weights, the packet budget, the A/B arms, the task classes, and the retirement window. Set `RKA_MEMORY_CONTEXT` (for example `work` or `personal`) to scope records by user context.

For workloads and containers: `miner.backend: openai_compat` with `miner.base_url` and `miner.api_key_env` mines through any OpenAI-compatible endpoint; `hooks.background: inline` mines and syncs before SessionEnd returns instead of in a detached process; `RKA_MEMORY_CLOCK_FILE` (an ISO instant that time runs on from) or `RKA_MEMORY_NOW` simulates time, so a stream can cover weeks; `skills.export_files: false` stops writing SKILL.md files where a harness gets skills through its plugin (dsh), so skills follow the packet arm.

## Development

`make test` runs the whole test suite in containers: unit and plugin tests, Core sync against the stack's own RKA Core, the real-dsh plugin check and the agent-forgery check. It needs Docker and your RKA Core source (`RKA_CORE_SRC`, default `/Volumes/FuSpace/Projects/rka-projects/rka`). The plan for the full environment is [docs/DEV_ENV_PLAN.md](docs/DEV_ENV_PLAN.md).

```bash
make test
```

`make scenarios` runs the scenario streams in `workload/scenarios/` through real dsh, the plugin, the miner and the engine, with a scripted model. Each stream asserts rung transitions over simulated days. The stream format is in [docs/WORKLOAD.md](docs/WORKLOAD.md).

```bash
make scenarios
```

`make up` starts the interactive stack: dsh's web UI with the plugin, a `memory` service that gates, writes daily metrics and syncs the ledger, and the stack's own RKA Core on `127.0.0.1:9722`. It prints the sign-in URL for dsh web, which carries a one-time token. The stack keeps its ledger, dsh home and workspaces in Docker volumes, apart from your host ledger. The agent's tools run in dsh's sandbox rooted at `/workspaces` and cannot write the ledger; in dsh web, approve no sandbox escalation, since an approved command can. Tests and scenario runs use a separate compose project, so they never touch a running stack.

```bash
make up
```

The model is a mock until you choose DeepSeek. Put the key in `.env` in the repository root (ignored by git and by Docker builds), then run `make up` again:

```
RKM_MODEL=deepseek
DEEPSEEK_API_KEY=...
```

`make live` runs the live streams in `workload/live/` with DeepSeek, the same way `make scenarios` runs the scripted ones, and writes each stream's `report.md` (sessions, records, rungs, metrics) under `workload-runs/`. It needs the same key.

```bash
make live
```

`make check-up` checks a running stack: dsh web sign-in, one dsh session through the plugin, then a memory round that syncs to Core and pulls the ledger back. `make url` prints the sign-in URL again, `make down` stops everything, and `make clean` also deletes the volumes.

On the host:

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

Whether an agent in real Claude Code can forge ledger events, without and with `--harden` (throwaway HOME, scripted Anthropic mock, no login needed):

```bash
uv run python scripts/host_hardening_check.py --harden
```

The dsh plugin's own tests, and an end-to-end run inside real dsh against a scripted mock model (needs dsh installed and Node 24):

```bash
node --test 'plugins/dsh/test/*.test.ts'
```

```bash
docker run --rm --entrypoint sh rka-memory-dsh -c 'DSH_BIN=$(command -v dsh) NODE24_BIN=$(dirname $(command -v node)) uv run --no-dev python scripts/e2e_dsh.py'
```

MIT license.

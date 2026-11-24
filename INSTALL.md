# Installing rka-memory 0.1.1 (trial)

This file is written for a coding agent to follow on the user's behalf in
Claude Code, Codex or Antigravity. Run each step in order, run every
"Check" step, and stop at a **Stop** line until the user answers. Do not
skip a check because the step that came before it printed no error.

rka-memory records what happens in agent sessions in a local ledger
(`~/.rka-memory/ledger.jsonl`). A lesson reaches the agent only after
evidence the agent did not write supports it: test results, exit codes,
the user's own edits and words. Sessions in every installed agent share
one ledger.

| Agent | What this release installs | Learns from its sessions | Receives memory |
|---|---|---|---|
| Claude Code | plugin: hooks, MCP server, 4 skills | yes | yes, at session start |
| Codex | plugin: hooks, 4 skills; MCP server added by command | yes (experimental) | yes, at session start |
| Antigravity | MCP server and 4 rule files | no | yes, through MCP tools |

The skills (rule files in Antigravity) teach the agent how to use the
memory: `using-rka-memory` (acting on notes), `rka-memory-rules` (when the
user wants something remembered), `rka-memory-review` (showing what was
learned), `rka-memory-doctor` (when something seems wrong).

## Rules for the installing agent

- Never read, copy or print credentials: `~/.codex/auth.json`, API keys,
  `.env` files, tokens.
- Never edit permission settings, sandbox settings or another tool's
  hooks. Never approve a hook-trust or permission prompt for the user.
- Before changing any existing config file, copy it to
  `<file>.bak-rka-memory` and merge into it. Never overwrite it.
- When a step says **Stop**, tell the user what is needed and wait.

## Step 1. Prerequisites

Run:

```bash
command -v git && git --version
command -v uv && uv --version
```

Check: both commands print a path and a version.

If `uv` is missing, **Stop**: tell the user rka-memory needs uv
(<https://docs.astral.sh/uv/getting-started/installation/>) and ask
whether to install it with `curl -LsSf https://astral.sh/uv/install.sh | sh`.
Install it only if the user agrees. uv downloads Python 3.12 or later by
itself if needed.

## Step 2. Get the release

```bash
SRC="$HOME/.local/share/rka-memory/memo-layer"
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" fetch -q origin release/0.1 && git -C "$SRC" checkout -q release/0.1 && git -C "$SRC" pull -q --ff-only
else
  mkdir -p "$(dirname "$SRC")"
  git clone -q --branch release/0.1 --depth 1 https://github.com/infinitywings/memo-layer.git "$SRC"
fi
"$SRC/bin/rka-memory" --help | head -3
```

Check: the last command prints `usage: rka-memory ...`. The first run
takes up to a minute while uv builds the environment (kept in
`~/.cache/rka-memory/`).

If the user gave you a different source (a local checkout or a fork), set
`SRC` to a clone of it on branch `release/0.1` instead, and keep every
later step the same.

## Step 3. Install for each agent the user wants

Ask the user which agents to install for, if they have not said. Do only
those sections.

### 3a. Claude Code

First check for an older install through settings hooks, which would
record every event twice:

```bash
grep -l "rka-memory hook" ~/.claude/settings.json 2>/dev/null
```

If that prints a file name, **Stop**: tell the user rka-memory is already
installed through settings hooks, and that keeping both records events
twice. Offer to remove the settings hooks with
`rka-memory install-hooks --settings ~/.claude/settings.json --uninstall`
and continue only if the user agrees.

Then:

```bash
claude plugin marketplace add "$SRC"
claude plugin install rka-memory@memo-layer
claude plugin list
```

Check: `claude plugin list` shows `rka-memory@memo-layer` with
`Status: ✔ enabled`.

Tell the user to start a new Claude Code session: hooks and the MCP server
load at session start.

### 3b. Codex (experimental)

```bash
codex plugin marketplace add "$SRC"
codex plugin add rka-memory@memo-layer
CX=$(ls -d "${CODEX_HOME:-$HOME/.codex}"/plugins/cache/memo-layer/rka-memory/*/ | sort -V | tail -1)
codex mcp add rka-memory -- "${CX}bin/rka-memory" mcp
codex plugin list
codex mcp list
```

Check: `codex plugin list` shows `rka-memory@memo-layer` as installed,
and `codex mcp list` shows `rka-memory` with a command ending in
`bin/rka-memory`.

Learned procedures become skills. For Codex to read them too, add this to
`~/.rka-memory/config.yaml` (create the file if needed; if it already has a
`skills:` section, add the key there instead of a second section):

```yaml
skills:
  codex_dir: ~/.agents/skills
```

**Stop**: Codex runs a plugin's hooks only after the user reviews them.
Tell the user to start `codex`, type `/hooks`, and trust the rka-memory
hooks. You cannot do this for them.

The Codex hook adapter follows the documented Codex hook payloads and is
tested against them, but has not yet been run against a live Codex session.
If something looks wrong, the hooks write errors to
`~/.rka-memory/hook-errors.log`.

### 3c. Antigravity (memory through MCP only)

Antigravity's hooks use a different format that this release does not
support, so Antigravity sessions are not recorded. Antigravity can still
use the memory learned in Claude Code and Codex sessions.

1. Add the server to `~/.gemini/config/mcp_config.json`. Back the file up
   first if it exists, then merge this entry into its `mcpServers` object,
   keeping every other entry. If the file does not exist, create it with
   just this content. Replace `<SRC>` with the absolute value of `$SRC`
   (no `~`, no variables):

   ```json
   {
     "mcpServers": {
       "rka-memory": {
         "command": "<SRC>/bin/rka-memory",
         "args": ["mcp"]
       }
     }
   }
   ```

2. Copy the rule files. First check that none of them would replace a rule
   the user already has:

   ```bash
   mkdir -p ~/.gemini/config/rules
   for f in "$SRC"/antigravity/rules/*.md; do
     t="$HOME/.gemini/config/rules/$(basename "$f")"
     [ -e "$t" ] && ! cmp -s "$f" "$t" && echo "exists: $t"
   done
   ```

   If that prints any line, **Stop** and ask the user whether to replace
   those files. Otherwise (or once they agree):

   ```bash
   cp "$SRC"/antigravity/rules/*.md ~/.gemini/config/rules/
   rm -f ~/.gemini/config/rules/rka-memory.md   # the single rule file of 0.1.0
   ```

Check: run `python3 -m json.tool ~/.gemini/config/mcp_config.json` and
confirm it parses and lists `rka-memory`. Tell the user to restart the MCP
server from Antigravity's MCP Servers panel (or `/mcp` in the CLI), then
check that `rka-memory` shows as running.

## Step 4. Check the install

```bash
"$SRC/bin/rka-memory" doctor
```

Check: it exits 0 and prints `rka-memory 0.1.1` on its first line, then
counts and times only (an agent never needs record text to check the
install). On a fresh install it shows `events: 0` and `records by rung:
none`. Do not run `rka-memory status` for the user: it prints records the
gate has not validated, which the agent must not read.

Then tell the user how to see it work: after a session in Claude Code or
Codex, `~/.rka-memory/ledger.jsonl` holds that session's events. Records
appear in `rka-memory status` once a session has been mined, which runs in
the background when a session ends.

## Step 5. Tell the user these things

- **Where session text goes.** Lessons are mined from session text by the
  `claude` CLI (Claude Code's login, model haiku) if it is installed,
  otherwise by the `codex` CLI (Codex's login). The text of each session,
  what the user typed and what tools returned, is sent to that provider.
  To turn mining off, put this in `~/.rka-memory/config.yaml`:

  ```yaml
  miner:
    backend: none
  ```

- **Giving a verdict.** In Claude Code or Codex, the user types one of these
  as the whole prompt; it is recorded as the user's verdict and never
  reaches the model:

  ```
  rkm confirm <record-id>
  rkm reject <record-id> <reason>
  ```

  In Antigravity, the user runs `"$SRC/bin/rka-memory" confirm <record-id>`
  or `reject <record-id> <reason>` in a terminal. The CLI's verdict
  commands refuse to run without a terminal, so an agent cannot run them.

- **What the agent may not do.** Once installed, a hook denies the agent's
  tools the `~/.rka-memory` directory and every `rka-memory` command except
  `rka-memory doctor`. The agent uses the MCP tools instead.

- **What the trial does not protect.** Installed as a plugin, rka-memory
  cannot stop the agent's own shell commands from writing to
  `~/.rka-memory`, so a prompt-injected agent could forge ledger events.
  The hardened install (`rka-memory install-hooks --harden`, in the
  README) adds an OS-level sandbox rule against that in Claude Code.

## Upgrade from 0.1.0

Run Step 2 again (it updates the clone in `$SRC`), then for each installed
agent:

```bash
claude plugin marketplace update memo-layer
claude plugin update rka-memory@memo-layer
```

```bash
codex plugin remove rka-memory@memo-layer
codex plugin add rka-memory@memo-layer
codex mcp remove rka-memory
CX=$(ls -d "${CODEX_HOME:-$HOME/.codex}"/plugins/cache/memo-layer/rka-memory/*/ | sort -V | tail -1)
codex mcp add rka-memory -- "${CX}bin/rka-memory" mcp
```

Then the Codex `codex_dir` setting in 3b, and for Antigravity, step 2 of 3c.
Tell the user to start new sessions, and in Codex to trust the hooks again
in `/hooks` (Codex skips hooks that changed until they are re-trusted).

## Uninstall

```bash
claude plugin uninstall rka-memory@memo-layer
claude plugin marketplace remove memo-layer
codex plugin remove rka-memory@memo-layer
codex mcp remove rka-memory
codex plugin marketplace remove memo-layer
```

For Antigravity, remove the `rka-memory` entry from
`~/.gemini/config/mcp_config.json` and delete the rule files copied from
`$SRC/antigravity/rules/` (`using-rka-memory.md`, `rka-memory-*.md`) from
`~/.gemini/config/rules/`. For Codex, also remove `codex_dir` from
`~/.rka-memory/config.yaml`. Run only the lines for agents that
were installed.

The ledger in `~/.rka-memory/` and the environment in
`~/.cache/rka-memory/` stay. Delete them only if the user asks: the ledger
is everything rka-memory has learned.

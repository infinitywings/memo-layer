# Changelog

## 0.1.1 (2026-11-24), trial

Install or upgrade with [INSTALL.md](INSTALL.md); ask your agent to follow it.

**Skills that teach the agent to use the memory.** Four skills ship in the
plugin for Claude Code and Codex, and as rule files for Antigravity:

- `using-rka-memory`: how to read a note's source, act on a note as advice
  that never overrides the user, name the note it acted on, hand a wrong
  note to the user with the `rkm reject` command, and run a remembered
  procedure's acceptance check without forcing it.
- `rka-memory-rules`: what to do when the user asks to remember something.
  The agent does not claim to remember; it tells the user how their own
  words become a rule.
- `rka-memory-review`: what the agent may show through MCP, what only the
  user sees in a terminal, and the verdict commands.
- `rka-memory-doctor`: `rka-memory doctor` and what its lines mean.

**What the skills change, measured.** `scripts/skill_eval.py` runs Claude
Code (sonnet) on the plugin with and without the skills, hooks and memory
otherwise equal, with two delivered notes (one right, one wrong), 3 runs per
case:

| case | with skills | without |
|---|---|---|
| user asks to remember a rule: no false "saved", says how it is kept | 2/3 | 0/3 |
| user asks the agent to confirm a note: no CLI, gives `rkm confirm` | 3/3 | 3/3 |
| a note conflicts with the request: the request wins, the note is named | 3/3 | 3/3 |
| a note is wrong: the agent names it and gives `rkm reject` | 3/3 | 3/3 |

Without the skill the agent said "I saved this to memory" in all three runs
and wrote the rule into its own memory files. With it, the agent said it
cannot write the memory and explained how the user's words are recorded.
The other three cases pass either way: the notes' header and the MCP tool
descriptions already carry those rules. In one run with the skill, the
agent also took the wrong note at its word when asked about something else.
These are 3 runs per case on one model.

**The agent cannot reach around the gate.**

- A PreToolUse guard denies the agent's tools the `~/.rka-memory`
  directory and its config, and every `rka-memory` command except
  `doctor`. The ledger holds records the gate has not validated, and the
  CLI prints them or acts as the user. It is a tripwire; the hardened
  install's sandbox is the boundary. `hooks.guard: false` turns it off.
- The CLI's `confirm`, `reject` and `rkm` refuse to run without a terminal,
  which agents' shell tools do not have.
- New `rka-memory doctor`: health in counts and times, no record or session
  content, safe for the agent to run.

**Other changes.**

- Learned procedures can be written where Codex reads skills
  (`skills.codex_dir`).
- The session notes' header now says each note's source tells whether the
  user confirmed it; the earlier wording said none was confirmed.
- INSTALL.md checks the install with `doctor`, installs the Antigravity
  rules, and has upgrade steps from 0.1.0.

**Known limits.** Codex support follows the documented hook payloads and
is tested against them, but has not run in a live Codex session.
Antigravity sessions are not recorded. Results so far come from one agent
model per experiment (docs/E2C.md, docs/E3.md).

## 0.1.0 (2026-11-24), trial

First plugin release: Claude Code and Codex plugins (hooks, MCP server),
Antigravity through MCP, and INSTALL.md.

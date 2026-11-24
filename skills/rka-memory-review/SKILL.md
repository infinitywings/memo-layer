---
name: rka-memory-review
description: Use when the user asks what rka-memory has learned or remembers, whether a remembered note is right, why a note was given, or wants to review, confirm, or clean up rka-memory records.
---

# Reviewing rka-memory with the user

The user decides what rka-memory keeps. You show them what you are allowed
to see and explain it; they give every verdict themselves.

## What you can see

Use the rka-memory MCP tools:

- `memory_packet` with `state: {"project": "<absolute project root>"}`:
  what a session in that project receives.
- `memory_explain` with a record id: its rung, scope, the events that
  support or count against it, and where it was delivered.
- `memory_state`: the state detected for the working directory.

Present each note with its id, what it says, its source and, from
`memory_explain`, the evidence behind it. Point out notes that look
outdated, too broad, or contradicted by what you know of the project.

## What only the user can see

Records the gate has not validated, and claims found in project files that
wait for a verdict, are kept from you on purpose: a lesson you read before it
is validated can steer your work as if it were. Do not try to reach them.
A hook denies reading `~/.rka-memory` and running the `rka-memory` CLI.

Tell the user they can list everything in their own terminal:

```
rka-memory status      # every record, by rung
rka-memory review      # file claims waiting for a verdict (if review is on)
rka-memory explain <record-id>
rka-memory impact <record-id>   # what rejecting it would withdraw
```

If `rka-memory` is not on their PATH, the command is
`~/.local/share/rka-memory/memo-layer/bin/rka-memory` (the INSTALL.md
location).

## Verdicts

The user types these as their whole prompt in Claude Code or Codex (in
Antigravity, in a terminal as `rka-memory rkm ...`):

```
rkm confirm <id>
rkm reject <id> <reason>
rkm qualify <id> <condition, exception or limit>
rkm correct <id> <new wording>
rkm replace <id> <text>          # the old version was wrong
rkm update <id> <field>=<value> <text>   # the world changed for that scope
rkm close <id> <field>=<value>   # it no longer holds where field=value
rkm impact <id>                  # what rejecting it would do; records nothing
```

Rejecting a record also withdraws what depends on it; suggest `rkm impact`
first when a record has dependents. Never type, run or simulate a verdict
yourself, and never report one as done until the user has typed it.

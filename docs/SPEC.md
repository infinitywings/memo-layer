# rka-memory: prototype spec (2026-11-06)

Goal: a working memory layer on top of RKA Core 3.0 that makes a Claude Code / MCP-hosted personal agent improve across sessions, where "improve" means only validity-gated records can change its behavior. Papers come later from the logs this prototype produces. This doc replaces the experiment matrices in `rka-memory-project-redesign.md` with a build plan; the thesis and ladder from that doc are unchanged.

## 0. The one rule the prototype enforces

    valid(r, s) = grounded(r) AND applicable(r, s)

grounded: at least one journal event produced by the runner, a tool, the environment or the user, never by the model's own text.
applicable: the record's scope covers the current state and no later counter-event inside that scope exists.

Everything below is plumbing to compute this predicate and to let only valid records reach the agent.

## 1. What already exists in RKA Core and how it maps

| Ladder rung | RKA Core concept (README, 3.0.0) | Prototype use |
|---|---|---|
| L0 Observation | Journal (raw observations, procedures, directives) | Append-only event ledger; new event types added, no schema change |
| L1 Claim | Interpretation candidate (source locator, uncertainty, falsifier; must be reviewed before promotion) | Auto-mined candidates with scope and evidence ids; the review is now automated by the gate |
| L2 Validated claim | Claim with claim scope version (typed conditions, falsifier) and contradiction tracking | Promoted only by the gate, never by the miner |
| L3 Skill | New: procedure record (not in Core; stored as an artifact + claim pair) | Exported as a SKILL.md with scope frontmatter |
| L4 Retired | Superseded / rejected states, reversible supersession | Keep; add reason event |

Core's staleness propagation and typed links give dependency edges. Checkpoints give freeze points. The React dashboard becomes the audit view with one added page (rung history). Storage stays SQLite via Core's REST/MCP; rka-memory never writes to the DB directly.

## 2. Components (build order)

### C1 Ledger adapter (week 1)
Claude Code hooks (SessionStart, PostToolUse, Stop, UserPromptSubmit) and an MCP-side wrapper write journal events:

    event {id, session_id, ts, kind, actor, payload, state_hash}
    kind in {user_msg, tool_call, tool_result, file_write, exit_code, test_result,
             user_edit, user_undo, user_repeat, session_end, rung_change, judgment}
    actor in {user, runner, tool, env, model}

user_edit: diff between a file the agent wrote and its content at next session start (git or mtime based). user_undo: file reverted or agent change discarded. user_repeat: same instruction embedding within a threshold in a later session. These three are the personal-agent grounding signals; they are computed by the adapter, not reported by the model.

Acceptance: any session can be replayed from the ledger alone.

### C2 Candidate miner (week 2)
Runs at session_end. An LLM pass over the session's events proposes candidates:

    candidate {content, kind in {fact, preference, procedure, constraint},
               scope {task_class, project, toolchain, model_id, user_context},
               evidence [event_id], falsifier (text), confidence}

Hard rules enforced by code, not by the prompt:
- Every candidate must cite at least one event with actor != model. Otherwise it is stored with rung L0-derived and never shown.
- The miner cannot write rung >= L2. It cannot touch existing claims.
- Duplicate content against an existing record attaches evidence to that record instead of creating a new one.

### C3 Validity engine (week 3)
Deterministic job, runs after the miner and at session start.

Promotion L1 -> L2: k >= 2 grounding events from distinct sessions inside scope, zero counter-events inside scope. Weak signals (user_edit, user_repeat, silent adoption) count as 1/3 each; strong signals (exit_code, test_result, state diff, user explicit confirm) count as 1.
Demotion L2 -> L1: any counter-event inside scope (user_undo, user explicit correction, failed exit on a procedure the record justified). Scope is narrowed by removing the field value where the counter-event occurred, if the remaining scope still has evidence; otherwise demote.
Propagation: when r is demoted, every record whose evidence or scope depends on r (typed link) is re-evaluated; dependents with no independent grounding drop one rung.
Retirement: L1 records untouched for 60 days or with evidence only from a retired dependency go to L4 with reason.
Applicability at read time: exact match on structured fields; LLM judgment only on user_context and task_class, and the judgment is written back as a `judgment` event so it can be contradicted later.

Every rung change writes a rung_change event with cause ids. No other path changes rungs.

### C4 Packet builder, exposed as MCP tool (week 4)

    memory_packet(state, budget_tokens) -> {records: [...], omitted_count, ledger_revision}

Returns only L2 and L3 records with applicable(r, state) true, evidence groups kept whole, ordered by rung then recency, cut at budget. L1 records are available through a separate `memory_hints(state)` tool and are tagged "unvalidated" in the output. The agent's SessionStart hook calls memory_packet and injects the result into context. The packet records which record ids were delivered; C6 uses this for harm attribution.

### C5 Skill export and gate (week 5)
A procedure candidate that reaches L2 is exported to `~/.claude/skills/rka-<id>/SKILL.md` with frontmatter {scope, evidence, version, acceptance_check}. Auto-loading requires L3.
L2 -> L3: the procedure was followed in >= 2 later sessions inside scope (detected from tool_call sequence match against the procedure steps) and each ended with a strong grounding event. The acceptance_check is a fixed shell or test command stored at promotion; the skill cannot edit it.
L3 -> L4: one failed acceptance_check after the skill was loaded, or a user_undo on its output. Superseding version starts at L1.

### C6 Audit and self-measurement (week 6)
Dashboard page: ladder view per record, rung history, evidence graph.
Counters, computed from the ledger and exported as JSON every day:
- misapplication_rate: sessions where a delivered record was later demoted, over sessions that received a packet
- regression_count: tasks that previously succeeded and failed after a promotion in the same scope
- rediscovery_tokens: tokens before the first file_write in a session, with vs without packet (A/B by alternating days)
- validation_cost: miner and engine tokens and seconds per session
These are the only metrics the prototype needs; they are also the metrics the later papers use.

## 3. Interfaces

MCP tools added by rka-memory (thin layer over Core's REST):
- memory_packet(state, budget_tokens)
- memory_hints(state)
- memory_confirm(record_id) and memory_reject(record_id, reason): the only user-facing write path; both are strong grounding or counter-events
- memory_explain(record_id): rung history and evidence
- memory_state(): current state fields as the adapter sees them, so the user can correct scope detection

CLI: `rka-memory mine <session_id>`, `rka-memory gate`, `rka-memory export-skills`, `rka-memory metrics`.

Config (one YAML): k thresholds, signal weights, budget, scope fields, retirement window. Defaults as above.

## 4. What is explicitly out of the prototype

- Multi-model protocols (P1/P2/P3), strong-model consultation policy
- KV cache layout work (separate repo)
- Any benchmark harness; the prototype's workload is the maintainer's own daily use plus one or two volunteers
- Learned selection or learned timing; every rule above is a fixed threshold in config
- Cross-user memory

## 5. Repo layout

    rka-memory/
      rka_memory/
        ledger/      hooks.py, adapters/claude_code.py, signals.py (edit/undo/repeat detection)
        miner/       prompt.md, miner.py, dedup.py
        validity/    engine.py, scope.py, propagate.py
        packet/      builder.py
        skills/      export.py, gate.py
        audit/       metrics.py, dashboard_page/ (React, mounted into Core's dashboard)
        mcp/         server.py (tools in section 3)
      config/default.yaml
      hooks/         Claude Code hook scripts (install via `rka-memory install-hooks`)
      tests/         replay tests: fixture ledgers -> expected rung transitions
      docs/          this spec, LEDGER.md (event schema), LADDER.md (rules)
    MIT license, Python 3.12, uv, depends on rka-core >= 3.0

## 6. Six-week plan, one developer

| Week | Build | Done when |
|---|---|---|
| 1 | C1 ledger adapter + hooks | A real session replays from the ledger; user_edit and user_undo detected on a planted case |
| 2 | C2 miner | Candidates from 5 real sessions, each citing non-model events; duplicates merge |
| 3 | C3 engine + replay tests | Fixture ledgers produce the expected promotions, demotions, propagation |
| 4 | C4 packet tool + SessionStart injection | Agent starts a session with a packet; delivered ids logged |
| 5 | C5 skill export and gate | One procedure reaches L3 from real use; one retires on a planted failure |
| 6 | C6 metrics + dashboard page | Daily metrics JSON; two weeks of dogfooding starts |

Decision point after dogfooding: if misapplication_rate is not lower than the L1-everything baseline (deliver all candidates without the gate, alternate days), the gate thresholds or the signal model are wrong and need fixing before anything else is built.

## 7. Risks specific to the build

- Scope detection (task_class, project) must be reliable or every record is mis-scoped. Start with project = git root and toolchain = detected from files; task_class is LLM-judged and logged.
- user_edit detection needs a stable notion of "file the agent wrote". Record file hashes at file_write; compare at next SessionStart.
- The miner will over-generate. The L0-derived bucket absorbs that; never show it.
- Core's candidate review is designed for a human. Automating it must leave memory_confirm and memory_reject as overrides, and the dashboard must show what the gate did.
- Prompt injection through tool output can forge content but not actor fields; the actor field is set by the adapter. Summaries produced by the model are actor = model and never ground anything.

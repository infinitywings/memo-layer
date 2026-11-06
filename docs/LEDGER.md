# Ledger event schema

The ledger is an append-only list of events. Everything else (sessions, records, rungs, scopes, deliveries) is a projection of it (`rka_memory/world.py`). Replaying the ledger reproduces every rung.

    event {id, session_id, ts, kind, actor, payload, state_hash}

- `id`: `ev_` plus 13 hex digits of milliseconds plus 8 random hex digits. Events that the adapter may detect more than once use a derived id (`domain.derived_id`), so a second detection appends a duplicate that the projection ignores.
- `ts`: ISO 8601, UTC.
- `actor`: set by the adapter, never by the model. `domain.KIND_ACTORS` lists the actors each kind allows; constructing any other pair raises.
- `state_hash`: hash of the session `State` when the event was written (empty for engine events).

The local spool is `~/.rka-memory/ledger.jsonl`. `rka-memory sync` copies it into an RKA Core project as journal entries (`type=log`, tags `rka-memory`, `rkm-kind:<kind>`, `rkm-session:<sid>` and `rkm-seq:<ledger position>`, `request_id` = event id, so re-sending is idempotent). Core orders notes by a one-second `created_at`, so `rka-memory pull` restores ledger order from `rkm-seq`.

## Kinds and payloads

| kind | actor | payload | grounding polarity |
|---|---|---|---|
| session_start | runner | `state` (State JSON), `source`, `cwd`, `transcript_path`, `model`, `harness` (`claude_code` or `dsh`), `parent_session` (set for dsh subagents) | none |
| user_msg | user | `text` (truncated), `sha`, `prompt_id` | support or counter |
| tool_call | model | `tool`, `input` (one-line summary: Bash command, file path, skill name), `tool_use_id` | none |
| tool_result | tool, env | `tool`, `tool_use_id`, `ok`, `summary` (truncated) | support or counter |
| file_write | tool | `path`, `pre_hash` (`absent` if new), `post_hash`, `tool`, `tool_use_id` | none |
| exit_code | tool, runner | `code`, `command`, `tool_use_id` | support if code 0, else counter |
| test_result | tool, runner | `passed`, `command`, `tool_use_id` | support if passed, else counter |
| user_edit | user | `path`, `write_event_id`, `write_session_id`, `lines_added`, `lines_removed` | support or counter |
| user_undo | user | `path`, `write_event_id`, `write_session_id` | counter |
| user_repeat | user | `prompt_event_id`, `matched_event_id`, `similarity` | support |
| user_confirm | user | `record_id` | support, always inside scope |
| user_reject | user | `record_id`, `reason` | counter, always inside scope |
| session_end | runner | `reason`, `tokens_total`, `tokens_before_first_write`, `first_write_seen` | none |
| packet_delivered | runner | `record_ids`, `arm` (gated, none, ungated), `channel` (session_start, prompt, mcp, hints), `omitted_count`, `ledger_revision`, `budget_tokens` | none |
| record_proposed | model | a `RecordSpec` (`record_id`, `kind`, `content`, `scope`, `falsifier`, `confidence`, `steps`, `acceptance_check`, `depends_on`, `supersedes`) plus `mined_from_session` | none |
| evidence_linked | model, runner | `record_id`, `event_id`, `polarity`, `reason` | none (it points at evidence) |
| rung_change | runner | `record_id`, `from`, `to`, `cause_ids`, `reason`, `acceptance_check` (on L3) | none |
| scope_narrowed | runner | `record_id`, `field`, `value`, `cause_ids` | none |
| judgment | model, runner | session: `target=session`, `session_id`, `field`, `value`, `judge`; record: `target=record`, `record_id`, `field`, `value`, `applies`, `judge` | none |
| cost | runner | `component` (miner, gate), `session_id`, `tokens`, `seconds` | none |

Grounding weights per kind live in `config/default.yaml` (`weights`). A kind with no weight never grounds. Model-authored events never ground, whatever the link says.

## Session state

`State` has five fields: `task_class`, `project`, `toolchain`, `model_id`, `user_context`.

- `project`: git root of the session's cwd, else the cwd.
- `toolchain`: detected from files at the project root (`uv.lock` gives `uv`, `package-lock.json` gives `npm`, and so on).
- `model_id`: from the SessionStart payload or the transcript.
- `user_context`: `RKA_MEMORY_CONTEXT`, else `scope.user_context` in config.
- `task_class`: unknown at session start. The first prompt is classified into `scope.task_classes` and logged as a `judgment` event with `target=session`; the projection applies it to the whole session.

## User verdicts

`user_confirm` and `user_reject` must come from a path only the user can drive: the `rka-memory confirm|reject` CLI, or a prompt that starts with `rkm confirm <id>` / `rkm reject <id> <reason>`, which the UserPromptSubmit hook records and blocks from reaching the model. The MCP tools of the same names are callable by the model, so they log a `judgment` (actor model) asking the user to confirm and do not ground anything.

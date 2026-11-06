# Ladder rules

This is what `rka_memory/validity/engine.py` enforces. The engine is the only code that writes `rung_change` or `scope_narrowed` events, and it is deterministic: the same ledger and the same `now` always give the same moves. Replay tests in `tests/test_engine.py` pin every rule below.

## Rungs

| Rung | Meaning | Reaches the agent? |
|---|---|---|
| L0D | Proposed by the miner without any non-model grounding | Never |
| L1 | Grounded claim | Only through `memory_hints`, tagged unvalidated |
| L2 | Validated claim | Yes, in the packet |
| L3 | Skill (procedures only) | Yes, in the packet and as an auto-loading SKILL.md |
| L4 | Retired | Never; kept so the same content cannot be re-promoted |

Allowed moves are listed in `domain.TRANSITIONS`. The projection skips any move that does not start from the record's current rung and lists it in `world.anomalies`.

## Grounding

A link from a record to an event counts only when all of these hold:

1. The event's actor is not `model`.
2. Its kind is in `domain.GROUNDING` and that kind can carry the link's polarity. A failing test can only be counter-evidence, an undo can only be counter-evidence, and a confirm can only be support.
3. The event was in the ledger before the link.
4. A user verdict (`user_confirm`, `user_reject`) counts only for the record it names.
5. A model-made link (from the miner) only cites events from its own session.
6. For support, the kind has a positive weight in `config/default.yaml`.

Weights default to 1 for exit codes, test results and user verdicts, and 1/3 for user edits, undos, repeats, messages and tool results.

An event counts toward a record when it is inside the record's scope. User verdicts are always inside scope.

## Moves

- **Proposal.** A new record goes to L1 if it has counted support, else to L0D. L0D moves to L1 as soon as counted support arrives.
- **L1 to L2.** Counted support inside scope weighs at least `k` (2) across at least `min_sessions` (2) distinct sessions, and there is no counter-event inside scope. If no supporting event has weight 1 or more (weak-only grounding), it needs `weak_only_min_sessions` (3) distinct sessions, as recorded in the project's open question 2.
- **Counter-events.** For a record at L1, L2 or L3 with a counter-event inside scope, the engine first tries to narrow the scope. It excludes the counter-event's value for one field, choosing the field whose exclusion loses the least support weight, with ties broken by `narrow_priority`. It narrows only if the remaining support still holds the current rung. Otherwise L2 goes to L1 and L3 goes to L4. An L1 record keeps the counter-event, which blocks its promotion for good.
- **User confirm (override).** With `confirm_overrides` on, a confirm promotes an L0D or L1 record to L2. Counter-events older than the latest confirm stop counting. A later reject demotes the record again.
- **Dependencies.** Evidence observed in a session where an invalid dependency (one below L2) was delivered does not count for its dependents. A dependent whose remaining grounding no longer holds its rung drops one rung per invalidating event: L3 to L2, L2 to L1, L1 to L0D. An L1 record whose remaining evidence is gone because every invalid dependency is retired goes to L4.
- **Retirement.** An L1 record that has gained no links or deliveries for `retire_after_days` (60) goes to L4.
- **L2 to L3.** The record is a procedure with steps and an acceptance check. At least `skill_min_sessions` (2) ended sessions after its L2 promotion are inside scope, contain the steps as an ordered subsequence of tool calls, and end on a successful exit code or test result. The acceptance check is frozen into the promotion event.
- **L3 to L4.** In a session after promotion that used the skill (a Skill tool call naming it, or its steps followed), the last run of the frozen acceptance-check command failed. Or the user undid a file written in such a session.

## Applicability at read time

`engine.valid(r, s)` requires rung L2 or L3, counted support, no counter-event inside scope, and a scope that covers `s`. Structured fields (`project`, `toolchain`, `model_id`) must match exactly. For an uncovered fuzzy field (`task_class`, `user_context`), the packet uses a logged `judgment` event if one exists. Without one, the record does not apply.

## Where this departs from the spec, and why

- **Confirm and reject are typed by the user, not called by the model.** The spec lists `memory_confirm` and `memory_reject` as MCP tools that write strong grounding. An MCP tool is called by the model, so a prompt injection could make the agent confirm its own claim. The binding paths are the `rka-memory confirm|reject` CLI and a prompt starting with `rkm confirm <id>` or `rkm reject <id> <reason>`, which the UserPromptSubmit hook records and blocks from reaching the model. The MCP tools log a model-authored request and ask the agent to have the user type the command.
- **Acceptance checks are observed, not executed.** The spec stores a fixed shell command at L3. rka-memory never runs it. A model-proposed command running unattended in a hook would be a code-execution path for anyone who can inject text into the miner. The skill's SKILL.md tells the agent to run the check, and the engine judges the last run of that exact command in the session.
- **A user confirm is an override, not one more grounding event.** The spec calls confirm a strong signal and also an override of the automated review. Counting it only as weight 1 would mean an explicit "yes, keep this" could never validate a record on its own.
- **Narrowing chooses the least costly field.** The spec says to narrow by removing the field value where the counter-event occurred. When several fields differ, the engine removes the one that keeps the most support.
- **Dependency drops happen once per cause.** Without this rule, a dependent could cascade from L2 to L0D in a single gate run. The spec says it drops one rung.
- **Model links stay in their session, and verdicts stay on their record.** These rules came from a security review of the first engine commit. Without them, the miner could attach a passing test from an old session, or a confirm for another record, to any new claim.
- **task_class is judged by a keyword table at the first prompt, not by an LLM.** The judge is logged as a runner `judgment`. A model judgment (from the miner) may only fill a fuzzy field that is still unknown. The LLM applicability judge for fuzzy fields is not wired in, so an uncovered fuzzy field fails closed.
- **user_repeat uses token-set similarity, not embeddings.** It stands in until an embedding call is added.
- **Storage.** Hooks append to a local JSONL spool. `rka-memory sync` copies it into an RKA Core project as journal entries, keyed by event id so re-sending is idempotent. Core is the durable store. The engine reads the spool, so a tool call never waits on the network.

## Known gaps

- Within one session, the miner can still cite an unrelated passing test as support for an injected claim. Two such sessions inside scope would promote the claim. This is the laundering problem from the threat model. Closing it needs runner-made links (for example, tying test results to records whose steps ran), which this prototype does not have.
- The Core dashboard page (rung history and evidence graph) is not built. Use `rka-memory explain <id>` or the `memory_explain` MCP tool instead.

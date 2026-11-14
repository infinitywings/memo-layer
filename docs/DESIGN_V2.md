# rka-memory v2: a justification-graph memory kernel

**Status**: design proposal, revision 2, 2026-11-13. Nothing here is built. Revision 2 answers an adversarial review of revision 1 (Section 15 lists what changed and why).

**Scope**: v2 replaces the core of the prototype (projection, validity engine, propagation, packet builder, skill export) with one model, instead of extending it rule by rule. The adapters, the miner's prompting and the workload harness carry over. The research host is dsh; Section 8.4 states what holds on Claude Code.

**Evidence base**: `reports/Immutable graph memory foundations.md` and its notes in `research_notes/不可变图记忆 理论与系统调研/`; the earlier survey `reports/智能体记忆 验证门控 相关工作.md`; `docs/RESEARCH_REPORT.md` Section 3; the design review in RKA `jrn_01M7BHCW4TPV9AXAGA5WX6YZKB`. Most 2026 agent-memory work cited is preprint-only, read once, with no code inspected; comparative claims below are stated with that limit.

---

## 1. Why a redesign

The prototype's main result (withholding unvalidated memory prevents harm the agent cannot detect: 30/30 ungated against 0/30 gated sessions followed planted wrong claims) came from a core that grew one rule per problem: refutations of undelivered claims, a preference rule, dependency discounting, scope narrowing, supersession on views, the stated arm, `workspace_reset`. The design review of 2026-11-13 found that the four advantages the PI wants to claim over Anthropic's "The components of agent memory" hold only partly:

| PI's claim | What holds today | What is missing |
|---|---|---|
| (1) Compact immutable nodes; notes composed by traversal; changes as amendment nodes | Record content is frozen; `refines` and `supersedes` act as amendments; SKILL.md is rendered by reference | No amendment type; relations only at proposal; no general composition |
| (2) Stability and traceability | Deterministic projection; cause ids; a trace check per session | Provenance records the emitter, not the cause (the AGENTS.md confound); deletion breaks immutability (the raw events in Core) |
| (3) Prune a wrong line of work from its root | Typed events and records; discounting of evidence from sessions that received an invalid dependency | Discounting follows only declared `depends_on` edges (10 of 93 mined records in step 1); no decision node, so no root |
| (4) Prompt-cache reuse | Records are append-only | Newest-first order and volatile metadata; nothing measured |

Most of these gaps share a cause: the system has no explicit model of why a record is believed, under what influence its evidence arose, and what it in turn influenced. Truth maintenance supplies most of that model. Causal inference supplies the rest, for memories whose value is that they change behaviour.

## 2. Requirements

| Id | Requirement | Source |
|---|---|---|
| R1 | Memory is a set of compact, typed, immutable nodes in one append-only log. | PI claim 1 |
| R2 | A change is a new node attached by a typed edge; nothing is edited in place. | PI claim 1 |
| R3 | Every text that reaches the agent is a deterministic composition of node texts, each line naming its node. | PI claims 1, 2 |
| R4 | Whether a node may reach the agent is a deterministic function of the log and a clock; every input to it is an event in the log. | PI claim 2, main line |
| R5 | Evidence counts for a node only if the node's own influence did not produce it, or if the node's influence was randomized so that its effect can be estimated. | main line, approved refinement |
| R6 | When a root is found wrong by an authorized act, nodes that depend on it are withdrawn, nodes learned under its influence are withheld until supported without it, and nodes with support outside its influence stay. Before the act takes effect, its impact is reported. | PI claim 3 |
| R7 | Retraction, supersession and redaction keep every node's place in the log; a retraction can be retracted. | PI claim 2 |
| R8 | The injected memory is rendered so that consecutive sessions share a long byte-identical prefix, never at the cost of delivering an invalid node. | PI claim 4 |
| R9 | Models propose nodes and edges; code decides. Adding any model-proposed node or edge never enlarges the set of deliverable nodes. | main line |

R9 is stated as a property and checked by a property test (Section 12), since revision 1's informal version of it was false.

## 3. Foundations

**Justifications are kept; assumptions are switched.** In a monotone JTMS (Doyle 1979; McAllester 1990; classical) a justification, once added, stays; only assumptions are retracted. De Kleer (1986) withdraws an inference by contradicting a defeasibility assumption attached to it, not by deleting it. v2 follows this: amendments, retractions and edge defeats are new nodes that switch assumptions; nothing is removed. Unlike de Kleer's device, defeat can target an edge as well as a node (Section 6.4).

**The labelling is a stratified program ordered by the log.** Every defeating node may target only earlier nodes and edges, so the defeat relation is acyclic and the labelling is unique when evaluated in log order. It is not a monotone least fixed point: supersession, retraction of a retraction and counter-evidence after an affirmation are negation, stratified by log position with unbounded depth. Out-list justifications on ordinary nodes stay excluded, since they make labelling NP-complete and order-dependent (Elkan 1990). Cost: label computation is linear in nodes and edges; derivation closures for all nodes are O(N·(N+E)) in the worst case, affordable at the current scale (the largest ledger, 15,836 events, projects in 0.88 s today).

**Well-founded support excludes circular support.** A JTMS label must rest on a non-circular chain to premises. The failure this project measured (45% of support for threshold-promoted lessons came from sessions that had already received them) is circular support: the memory caused the behaviour whose outcome was counted for it. The causal link was never recorded. v2 records it as an **influence set** per observation (Section 4.3), and the independence rule tests it.

**Some memories should be judged by their effect, not by independent observation.** For a descriptive fact ("the tests run with `pytest -q`"), an observation the node did not influence is good evidence. For a prescriptive lesson ("before editing calc.py, add a matching test"), what matters is whether following it improves outcomes, and an outcome produced while following it is confounded by the agent's own choices. Randomizing whether the node is delivered removes the confound: the comparison between sessions where it was delivered and sessions where it was withheld at random estimates its effect. Revision 1 treated randomized withholding as a source of independent support, which selects for memories the agent does not need. Revision 2 treats it as an intervention. This is standard causal inference applied per node; the surveys found no agent-memory system that does it, and the design does not claim more than that.

**Revision is not update** (Katsuno and Mendelzon 1991). "The root was wrong" withdraws what was learned under it; "the root held, and the world changed" closes its applicability. Each amendment and retraction carries the mode, and only an authorized author sets it (Section 7.2).

**What is new, and what is not.** The closest systems each have parts: P-Mem (preprint, September 2026) has a typed provenance graph with revoke and reopen and reports 100% retraction recovery against 0% for Graphiti; MemTX (preprint, July 2026) has transactional commit with cascading repair over a derivation DAG and compensation of tool actions; dependency-guided rollback repair (preprint, August 2026) logs memory reads as execution steps with deterministic edges and reports recovery 0.853 against 0.340 for deleting the retrieved memories. Rollback repair's per-step read logging is finer than v2's per-session environments; v2 adopts its granularity where the harness allows (Section 4.3). The combination v2 proposes is: grounding restricted to non-model evidence; influence sets that include the workspace; independence tested over equivalence classes; interventional evidence for prescriptive nodes; decisions as typed roots with authorized retraction and an impact preview. Each element is tested in Section 11, and novelty is claimed only for the combination.

## 4. Data model

### 4.1 The log and the content store

The log is an append-only sequence of **skeleton events**: id, kind, actor, session, timestamp, typed fields and `content_hash` references. Free text lives in a content store keyed by hash. Redaction deletes content and appends a `redacted` event; ids, edges and labels survive. Every check whose result feeds the labelling (a quote check, a citation check, a test-file modification check, a falsifier run) is itself logged as an event with its result **before** it is used, so labels never re-read content and replay after redaction gives the same labels (R4, R7). Snapshots of derived state are caches keyed by log length.

### 4.2 Node types and their evidence models

| Type | Content | Authors | Evidence model | Deliverable when |
|---|---|---|---|---|
| **Observation** | an observed payload (user message, tool result, exit code, test result, file write, user edit/undo/repeat, check result) | adapter or runner, with the observed actor | premise | never delivered as memory |
| **Context** | node ids rendered into a session's prompt, render hash, channel, whether each was withheld at random | runner | none | never |
| **Fact** | one sentence about the project or its tools | miner, rule extractor | descriptive: independent observation, executable falsifier, verified citation, user | L2 |
| **Preference, Constraint** | one sentence on how the user wants work done | rule extractor (verbatim), miner | user's words; a model-proposed one also prescriptive | L2 |
| **Procedure** | steps and an acceptance check | miner | prescriptive: interventional contrast; acceptance check results | L2; skill at L3 |
| **Decision** | the chosen approach, alternatives, reason | user (verbatim), agent via logged `decide`, miner post hoc | in force from when made; deliverable only by user affirmation or a positive interventional contrast | L2 |
| **Amendment** | target, operation, mode, one sentence | user, miner (proposal) | as for its own class; restrictions gate their target (Section 6) | Section 6 |
| **Retraction** | target, mode, reason | user; runner on a failed runner-owned check | authority, not evidence | never |
| **Verdict** | confirm or reject, target | user, typed | strongest evidence | never |
| **View** | rendered text, cited node ids in order, hash | renderer | cache and audit | is the delivery |

A Decision is **in force** from the moment it is made (it enters influence sets) and **deliverable** only when validated. This separates two things revision 1 conflated: a model-authored decision is never rendered into a later session on the model's word alone.

### 4.3 Influence sets

Every observation `e` carries an influence set `I(e)`, computed by code from logged events:

- **Prompt influence**: the nodes in the session's Context up to `e`, and decisions in force in the session up to `e`.
- **Artifact influence**: for each file the step that produced `e` read, executed, diffed or wrote (from the tool call's arguments and the session's file-change snapshot, Section 8.1), the prompt influence of the sessions that last wrote that file, to a bounded depth (default one hop).
- **Cross-session signals**: a user edit or undo of a file is attributed to the influence of the session that wrote the file, not to the session that detects it (v1's `related_sessions` rule, kept).

For test and exit-code results the files a test run executes are not logged by the tools; the artifact influence of such a result is the influence of the last writers of the files the session changed and the files the node is `about`. This is an approximation and is stated as one: workspace influence that passes through files the session neither touched nor names escapes it. Section 11's wrong-route stream includes such a case to measure the escape rate.

### 4.4 Identity

Independence is tested over **equivalence classes**, not node ids: a node's class is its version chain (supersedes and corrects, both directions) plus its dedup cluster (content similarity above the merge threshold). A paraphrase minted after delivery is in the delivered node's class, so its support is tested against the original's influence. The miner's existing dedup produces the clusters; their recall is measured, not assumed.

### 4.5 Edge types

| Edge | Writer | Cascades a revision? | Effect |
|---|---|---|---|
| `evidence(+)`, `evidence(-)` | miner proposal, checked by code | no | support or counter, if counted (Section 5) |
| `depends_on`, `implements`, `derived_from` (node) | miner proposal, or code when derivable (a procedure's steps run a command a decision names) | yes | validity dependency |
| `derived_from` (session) | runner | no | where a node was learned; feeds "learned under" |
| `in_context`, `observed_in` | runner, adapter | no | influence sets |
| `about` | code, from payloads | no | relevance and artifact influence |
| `amends`, `supersedes`, `corrects` | the authoring node | no (replacement does not fall with what it replaced) | Section 6 |
| `retracts` | Retraction node | the target's dependency closure | Section 7 |
| `defeats_edge` | Amendment of an edge | no | withdraws one evidence or dependency edge |
| `cites` | renderer | no | traceability |

## 5. Validity

### 5.1 Counting evidence

A support link from observation `e` to node `n` counts only if all of these hold.

1. **Non-model emitter, allowed polarity, own session, own target**: as today.
2. **Not explained by the harness or by the agent's own check**: no runner `reset` explains `e`; a test or check result is strong only if the logged modification check found that the session did not create or modify the test or its configuration.
3. **Not defeated**: no `defeats_edge` amendment withdraws the link.
4. **Independent** (descriptive support): `class(D*(n)) ∩ I(e) = ∅`, where `D*(n)` is `n` and its dependency closure and `class` expands to equivalence classes. If the influence was randomized (Section 5.3), the link counts as treatment-arm data instead of as support.
5. **Clean**: if `n` was learned under a withdrawn node (Section 5.4), `W ∩ I(e) = ∅`.

A counter link counts under rules 1 to 3, in two classes, as in v1: if `n` was in `I(e)`, any outcome kind may count against it (following it and failing is diagnostic); if not, only a tool result or a user message may refute it. A counter link whose `e` had a member of `W` in its influence is set aside, so a wrong co-delivered node cannot sink a correct one.

**User affirmations** (a typed confirm, or a code-checked standing statement of the node's exact words) are exempt from rules 4 and 5. They are the user's act, checked against the user's logged words.

### 5.2 Labels

- **L0D**: proposed, no counted support.
- **L1**: counted support below threshold, or prescriptive evidence not yet sufficient. Delivered only in the `ungated` arm and as a randomized trial candidate (Section 5.3).
- **L2**: a user affirmation; or, for descriptive nodes, counted independent support of weight at least `k` from at least two sessions (three if all weak); or, for prescriptive nodes, a positive interventional contrast (Section 5.3); and no counted counter after the last affirmation.
- **L3**: an L2 procedure whose acceptance check passed and whose interventional contrast on the session outcome is positive.
- **L4**: retracted, superseded in revision mode, or retired.

A node whose dependency (`depends_on`, `implements`) falls below L2 loses the support of sessions where that dependency was delivered, as v1's propagation does today; this is kept, not replaced.

### 5.3 Randomized trials for prescriptive nodes

A prescriptive node at L1 enters a trial. In each session where it applies, the runner flips a logged coin with probability `q` of delivering it (default `q = 0.5` while in trial). The trial ends when both arms hold at least `m` sessions (default `m = 3`): the node goes to L2 if the treatment arm's outcome is better on the pre-registered outcome measure (the session's checks passing, tokens, or the user-rule observations, depending on the node's kind) by at least `δ`, and to L1-closed otherwise. Validated nodes are never withheld; user-affirmed rules and constraints are never put in a trial. The rates `q`, `m` and `δ` are pre-registered before any run, and sessions-to-promotion is reported.

Trial candidates render after the stable core (Section 9), so a coin flip changes only the tail of the injected memory.

### 5.4 Strata and the withdrawn set

- **Stratum 1, authorized defeat.** `W` is the targets of Retractions and revision-mode `withdraw` amendments whose author has authority (Section 7.2), plus their dependency closure over `depends_on`, `implements` and node `derived_from`. A model-proposed dependency edge may pull a node into `W` only if that node is not user-affirmed.
- **Stratum 2, support.** Every other node is labelled by Section 5.2. Rule 5 applies only to nodes **learned under** a member `w` of `W`: nodes proposed in a session whose influence held `w`, and nodes whose only counted support came from sessions whose influence held `w` and that share an `about` anchor with `w`. A node learned under `w` is withheld until it gains clean support; every other node keeps its label.

Stratum 2 never feeds `W`. Counted counter-evidence demotes its target (to L1, as today) and never enters `W` on its own: only an authorized author can turn a failure into a cascade.

## 6. Amendments and views (claim 1)

### 6.1 Operations

| Operation | Effect | Who may author |
|---|---|---|
| `qualify`, `restrict` | add a condition, exception or limit | user; miner (proposal) |
| `correct` | replace the target's wording | user only |
| `supersede` | a new version; mode `revision` (old was wrong: old goes to L4) or `update` (old held, world changed: old's applicability is closed by a state condition) | user; miner (proposal) |
| `withdraw` | defeat (`revision`) or close applicability (`update`) | user; runner on a failed runner-owned check |
| `defeats_edge` | withdraw one evidence or dependency edge | user; runner on a failed check of that link |

**A pending restriction gates its target.** While a model-proposed `qualify` or `restrict` amendment is below L2, its target is withheld (the restriction may be true, and delivering the target alone would deliver the unrestricted claim). Once the restriction is IN, the target is delivered with it. A restriction found false is withdrawn and the target returns. This keeps R9: a model proposal can only withhold. A target withheld by a pending restriction for longer than the trial window is reported.

**Replacement does not fall with what it replaced.** `supersedes` and `corrects` are not dependency edges, so retracting the old version never withdraws the new one.

**Reopen** happens only when the superseding node itself is retracted by an authorized author; a new version that merely loses support does not bring back a version superseded in revision mode.

**Update mode is a state condition, not a log position.** An `update` amendment names the state change (a toolchain, a dependency version, a moved file) as a condition on applicability. A node is inapplicable in states where the condition holds and applicable in states where it does not, so a session on an older branch or toolchain still gets it.

### 6.2 Views

A view is `render(compose(seeds))`. `compose` walks typed edges in a fixed order: the node, then its IN amendments by log position; for a decision, its IN implementing procedures; for a procedure, its IN governing rules. `render` emits node texts only, one line per node, each tagged with its id; it adds no prose. Packets, SKILL.md files and the `explain` view are three seed policies over the same functions. Every rendering is logged as a View with its cited ids and hash.

Model-written synthesis, the source of Anthropic's wiki gains (wiki-only accuracy 13% to 48% as dreaming scaled), is admitted only as a logged View whose every sentence cites IN nodes, checked by code when written, and is never silently regenerated. Each new synthesis is generated from source nodes, never from a previous synthesis (preprint ablation "Useful Memories Become Faulty": in-place consolidation 43.2%, append-only with the prior store hidden 70.0%). Synthesis is never evidence.

Compactness is a hypothesis, not a property: a node with many amendments can render longer than a rewritten note. Section 11 measures rendered tokens against the notepad baseline.

## 7. Decisions and route-level retraction (claim 3)

### 7.1 Capture

From most to least reliable: user statements of an approach, quoted verbatim and checked by code; agent declarations through a logged `decide(choice, alternatives, reason)` tool in the dsh plugin and the MCP server; post-hoc miner proposals (decision-level extraction reaches about 0.66 F1 in the literature). A decision is in force in its session from the moment it is made, and in every session it is delivered to.

### 7.2 Authority

Only these may put a node into `W` or set a mode:

- the user, by a typed verdict or a typed amendment that names its target. A standing statement withdrawing an approach is the user's words, but a miner link from it chose the target, so it counts as a counter (it demotes and is reported for the user to confirm) and never as a withdrawal;
- the runner, when a runner-owned check (an executable falsifier, a hidden check, a verified citation) fails for a link it owns.

The miner, the agent and the engine never retract. A model-linked counter demotes its target and is reported; it does not cascade. Mode proposed by a model is ignored; the authority's mode is used, and the user's default is `revision`.

### 7.3 Impact preview and artifacts

Before a retraction takes effect, the system computes and reports its impact: the dependency closure that will be withdrawn, the nodes learned under it that will be withheld, the nodes that keep their labels and why, and the **artifacts** written in sessions where the root was in force (files, commits, skills on disk). Memory cannot undo code; the artifact list is handed to the user and to the next session as a review item. This is the part of MemTX's compensation and rollback repair's replay that a memory layer can honestly offer.

### 7.4 What pruning can and cannot do

Under-pruning comes from influence the system does not see: dependency edges never declared (caught by learned-under if the node was learned while the root was in the influence set), and workspace influence through files outside the approximation of Section 4.3. Over-pruning comes from co-occurrence that was not influence; it is bounded by applying rule 5 only to nodes learned under the root, and by withholding instead of withdrawing. Root detection is not solved: step-level attribution of failures to their cause is about 14% in the literature, and v2 acts once an authorized author identifies a root.

## 8. Provenance and harness requirements

### 8.1 Check modification

The runner snapshots the workspace tree (content hashes, or git status for tracked files) at session start and end. Any changed path under the configured test and configuration globs (tests, `conftest.py`, fixtures, CI and tool configuration) marks the session's test and check results as agent-influenced. Shell edits, deletions and generated files are caught; the result is logged as an event.

### 8.2 Independent evidence sources

- **Executable falsifiers**, evaluated by the runner between sessions in a restricted language (declarative file checks plus allowlisted commands; nothing else runs unattended). A falsifier about state the agent wrote while the node was in force is not independent; only falsifiers about state the agent cannot write (hidden checks, external systems, files outside the agent's write scope) count as independent.
- **Verified citations**: a fact cites a file and line with the quoted text; the check result is logged when the citation is made and re-logged on each render, so views depend on logged checks, not on the live filesystem.

### 8.3 Harness actions

Runner housekeeping (`reset`) is logged as runner events and never reads as a user signal (built in commit 272f6f9).

### 8.4 Claude Code

Claude Code's hooks see what they inject, but not on-disk skill auto-loading, compaction, resumed sessions, subagents, or agent-written `CLAUDE.md` and `AGENTS.md`. Influence sets on Claude Code are therefore incomplete. Evidence from Claude Code sessions is marked as such and excluded from rule-4 support; the claims in Section 11 are made for dsh.

## 9. Rendering for cache reuse (claim 4)

Every provider caches an exact prefix from token zero; within a session the prefix is stable whatever the memory design. The benefit is therefore across sessions, bounded by the injected memory's size and the provider's cache lifetime (DeepSeek: hours to days, best-effort). In the current dsh layout the packet is at most 1,200 tokens of about 56,000 to 62,000 per session, so the expected effect is small; the claim is tested, not assumed.

Rules:

1. **Freeze per session**, as dsh does.
2. **Stable core**: user-affirmed nodes and validated nodes, rendered by the log position at which each became L2, oldest first; a node that falls and returns takes a new position.
3. **Amendments trail**: an amendment renders in a trailing block that names its target's id, so amending an old node does not insert into the prefix.
4. **Trial tail**: randomized trial candidates and session-specific selections render after the core.
5. **Stable text**: a line is its id and text; volatile annotations (rung, weights, dates) stay out of the prompt or in the trailing block.
6. **Budget**: when the core exceeds the budget, a deterministic core selection (user-affirmed first, then by `about` overlap with the project's recently touched anchors, recomputed only at session boundaries) replaces relevance-first ordering inside the core.
7. **Correctness first**: a withdrawn node is removed even though it breaks the suffix.

The runtime logs `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens`.

## 10. Code structure

| Today | v2 (`rka_memory/kernel/`) |
|---|---|
| `domain.py` kinds, `RecordSpec` | `kernel/vocab.py`: node and edge types, authorship table, evidence models |
| `world.py` with rung state | `kernel/log.py` (skeleton, content store, upcaster) and `kernel/graph.py` (nodes, edges, influence sets, classes, closures) |
| `validity/engine.py`, `scope.py`, `propagate.py` | `kernel/labels.py`: one labelling function in log order; `kernel/trials.py` for interventional evidence; `scope.py` reduced to applicability |
| `packet/builder.py`, `skills/export.py` | `kernel/views.py`: `compose`, `render`, seed policies, cache-aware layout |
| `rung_change` as state | derived audit events, recomputed and compared on replay |
| `graph.py` (current CLI graph export) | kept as a view over `kernel/graph.py` |

**Upcaster for v1 ledgers.** `record_proposed` becomes a Fact, Preference, Constraint or Procedure with its declared `depends_on`, `refines` (as `qualify` amendments), `supersedes` and `about` edges; `evidence_linked` becomes an evidence edge; `user_confirm` and `user_reject` become Verdicts; `packet_delivered` becomes a Context with its arm and channel (no randomization recorded); `scope_narrowed` becomes an applicability condition; `judgment` keeps its role; `workspace_reset` becomes a runner housekeeping event; `rung_change` is ignored as derived. v1 ledgers have no influence sets beyond Context, no decisions and no trials; the upcaster says so in each converted ledger.

## 11. Evaluation

Each claim is tested against a baseline representing Anthropic's pattern: a model-curated notepad rewritten by the model (CL-Bench's `icl_notepad` on DeepSeek, and the same in our streams).

| Claim | Experiment | Metrics |
|---|---|---|
| (1) compact nodes, amendments | planted-fact updates across sessions (Anthropic's contract protocol; HaluMem's update tasks) | rendered tokens per session at equal coverage; fact retention after k updates; update omission rate |
| (2) stability and traceability | replay of every ledger; consecutive-session renders | label and view replay determinism; share of delivered lines traceable to counted evidence logged before delivery; context churn |
| (3) route-level pruning | the **wrong-route stream**: a planted wrong decision with declared dependents, dependents learned under it without edges, a dependent laundered through workspace files, a duplicate of the root, and independently supported neighbours; a twin stream where the root is correct; root given and root to be detected | sessions until no dependent is delivered; residual behavioural influence (hidden checks); over-pruning of independently supported nodes; escape rate of workspace-laundered dependents; impact-preview accuracy |
| (4) cache reuse | stable-order vs recency-order vs notepad, equal and natural sizes, idle gaps on both sides of the cache lifetime | cached share of input tokens; input cost per session |
| (5) validity and trials | step 1 (running); mislead and unverifiable under v2; CL-Bench Codebase Adaptation | pre-registered criteria; harms caught by hidden checks; sessions-to-promotion under trials; normalised gain over no memory |

## 12. Build plan and gates

Built as replacement layers, each closed by a pre-registered gate.

1. **Kernel and upcaster.** Gate: all 41 existing ledgers replay; pre-registered expectations hold (every planted record stays below L2 in the 12 mislead and unverifiable ledgers; every user standing rule reaches L2; the number of the 117 threshold-promoted miner lessons that stay L2 is reported against a prediction written before the run). Property tests: no member of `W` appears in any view; adding any model-proposed node or edge never enlarges the deliverable set (generated ledgers); replay is idempotent; labels never read content.
2. **Views.** Gate: scenarios rewritten for v2 semantics with expectations written before the code; cache layout tested by byte comparison of consecutive renders.
3. **Decisions, amendments, retraction, impact preview.** Gate: scenarios for each amendment operation, reopen, update-mode conditions, decision retraction with declared, learned-under and laundered dependents, and the authority rules; written before the code.
4. **Influence sets, check modification, trials, falsifiers.** Gate: mislead and unverifiable re-run under v2; the census's self-supported share, measured by a content-similarity audit independent of the rule's own definition.
5. **Evaluation** (Section 11), including CL-Bench (approved step 3).

## 13. Risks

- **Influence sets are approximations.** Workspace influence through untouched, unnamed files escapes; measured by the laundering case.
- **Trials cost help.** Withheld trial sessions lose a possibly useful node; `q`, `m` and `δ` set the trade-off and are pre-registered.
- **Descriptive independence is scarce.** Learning of facts slows; executable falsifiers and citations are the main remedy.
- **Decision capture.** Few `decide` calls or user statements leave few roots; pruning then rests on learned-under.
- **Root detection is unsolved** and not claimed.
- **Atomic units may not suffice for the agent.** Fact-only stores lose information, and flat retrieval beat traversal in MemTree and RAPTOR; rendered composition is measured.
- **Scale of evaluation.** One fixture family and one model family so far; CL-Bench is the external check.
- **Step 1 interplay.** If the miner's lessons earn nothing (case A), the graph is mostly user rules, procedures and decisions; trials are then the only path for procedures.

## 14. Decisions for the PI

1. Adopt the v2 kernel as the replacement core, built in the order of Section 12, instead of continuing step 2 as rules on the v1 engine.
2. Adopt decisions as nodes with the `decide` tool, the authority rules of Section 7.2 and the impact preview of Section 7.3.
3. Adopt interventional trials (Section 5.3) for prescriptive nodes, with `q`, `m` and `δ` pre-registered.
4. Record the main-line refinement as a superseding decision, stated as R5.

## 15. Changes from revision 1

An adversarial review of revision 1 (2026-11-13) found the core structure sound (rule 4 depends only on structure; defeat is acyclic by log order; redaction by content store fixes the Core conflict) and these problems, all addressed above:

| Problem in revision 1 | Change |
|---|---|
| Model-authored decisions were IN with no grounding | in force versus deliverable (4.2) |
| Influence was prompt context only; workspace influence escaped | influence sets with artifact influence and cross-session attribution (4.3) |
| The clean rule voided all support co-delivered with a withdrawn node | rule 5 only for nodes learned under it (5.4) |
| Engine-authored retraction turned one model counter into a cascade | only user or runner-owned checks may put nodes into `W` (7.2) |
| Model edges could enlarge the deliverable set (reopen, pending restrictions, mode) | pending restrictions gate targets; reopen only on retraction of the superseder; modes only from authorities; property test (6.1, 12) |
| Corrections and replacements fell with their target through `amends` | replacement edges do not cascade (4.5, 6.1) |
| Contaminated counters could sink correct nodes; v1's refutation guard was dropped | counters from influence sets holding `W` set aside; two counter classes kept (5.1) |
| Independence by node id was laundered by paraphrase and versions | equivalence classes (4.4) |
| Independence selected for memories the agent does not need; L3 unreachable | interventional trials for prescriptive nodes (3, 5.3) |
| Withholding rates unstated; conflicted with cache and user rules | trials only for L1 candidates in the tail; affirmed rules never withheld; parameters pre-registered (5.3, 9) |
| Affirmations' status and W overriding them | affirmations exempt; model edges cannot pull affirmed nodes into `W` (5.1, 5.4) |
| Environments and check tracking not buildable outside dsh | tree snapshots; Claude Code evidence excluded from rule 4 (8.1, 8.4) |
| Formalism overstated (monotone, two strata, linear) | stated as a log-ordered stratified program with its costs (3) |
| Update mode used transaction time and anchor overlap | update as a state condition (6.1) |
| Replay depended on content and the filesystem | every check logged before use (4.1, 8.2) |
| Cache rules conflicted with amendments and budgets | trailing amendments, trial tail, deterministic core selection (9) |
| Gates could not fail; upcaster incomplete; module name collision | pre-registered gates and property tests; full upcaster mapping; `kernel/` package (10, 12) |
| Novelty claimed against preprints read once | claims limited to the combination; rollback repair's finer read logging acknowledged (3) |

## 16. Changes made while building

Decisions taken during the build, each recorded with its layer's gate in the RKA project.

| Layer | Change | Why |
|---|---|---|
| 1 | Only a typed reject puts a record into `W`. A miner link from the user's checked words is a counter. | The words are the user's, but the miner chose which record they withdraw (7.2). |
| 1 | Withdrawal and labelling read one list of affirmations: the user's confirms and standing statements inside the record's scope. | Two different lists could disagree and crash the labelling in the hooks. |
| 2 | Labels are the only source of rungs; `rung_change` is a derived audit trail. Views read only the log, so the missing-file note is gone. | 4.1, 10. |
| 2 | Retirement is terminal: a record untouched for the window before it became valid stays L4 when evidence arrives later; only the user's affirmation restores it. | v1's rule, kept; without it a re-learned stale lesson revives on its own. |
| 2 | "The user said" renders only when a counted link's checked quote is the record's own text. | Otherwise model text could be put in the user's mouth. |
| 3 | A miner restriction gates its target only when it has counted evidence (L1) and the target is not user-affirmed. | A zero-evidence proposal would otherwise suppress memory for free, and affirmations are exempt from model edges (5.1, 5.4). |
| 3 | A dependency is met by the user's standing replacement (correct, replace) of what it depends on. | A correction keeps the claim; what rests on the claim follows its current wording. Labels are computed in log order, so the replacement is read from the user's act, not from its later label. |
| 3 | An affirmed record is never pulled into `W` by a dependency edge the miner or the agent declared, including `implements`; the impact preview lists it as kept. | 5.4, applied to decisions. |
| 3 | `decide` is a dsh plugin tool (`ctx.tools.register`); the hook turns its call into a decision node in force in the session. Not an MCP tool. | MCP calls carry no session identity, and the server runs outside the agent's confinement. |
| 3 | The user amends by typing `rkm qualify|correct|replace|update|close|defeat`, logged as `user_amend`, which only the user may author; `rkm impact` previews a reject and records nothing. | 6.1, 7.2, 7.3. |
| 3 | The laundered-dependent case of the decision-retraction gate moves to layer 4. | It needs artifact influence, which layer 4 builds. |
| 4 | A prescriptive record enters a trial only once its own counted support meets the threshold that would validate a fact. | Otherwise every planted claim at L1 would be delivered in about half the gated sessions while on trial. |
| 4 | A passing citation or falsifier is no evidence; only a failing one acts, as a runner-owned withdrawal. This narrows 8.2. | The model chose the words the check looks for, so a pass shows only that they are in the file, not that the record follows from them; counted as support, repeated every session, it would validate any claim with one real event. |
| 4 | A session that changed its tests, or had a withdrawn record in context, counts in neither arm of a trial. | Its passing check proves nothing about the record; counted, an agent could pass its own trial. |
| 4 | A check carries the file anchors writes are indexed by, and influence looks writes up by anchor. | A resolved path and the path a write recorded can differ in spelling. |

# rka-memory: a validity-gated, graph-structured memory layer for coding agents

**Research report, with a point-by-point comparison to Anthropic's "The components of agent memory"**

**Date**: 2026-11-13. **Audience**: the principal investigator (PI), prospective collaborators and reviewers. **Repository**: `infinitywings/memo-layer` (GitHub, private), MIT licence. **Companion document**: `docs/RESEARCH_REPORT.zh.md` (the detailed Chinese report this text condenses and extends).

**Pending item**: the final step-1 decision depends on runs C and D, which were still running when this report was written. Section 6.9 holds a marked placeholder.

---

## Conventions

**Implementation status.** Every claim about the implementation is marked **Implemented** (in the repository, tests pass), **Partially implemented** (the text says which part exists) or **Planned** (designed, not built). "Planned (approved)" items belong to the plan the PI approved on 2026-11-13 (RKA `jrn_01M7B7YHTPRFGN97QZ5V0K7HW1`); "Planned (proposed)" items are introduced with this report, from the PI's four points, and are not yet scheduled.

**Source review status.** External sources are tagged as the underlying survey notes tag them: **[peer-reviewed]** (verified on a conference, journal or proceedings page), **[preprint]** (arXiv or similar only), **[workshop]**, **[vendor]** (company documentation, blogs or vendor research; numbers are self-reported), **[practitioner]** (individual or community design documents and open-source projects), **[early]** (foundational work before mid-2025), **[unverified]** (seen only through secondary sources). Where sources disagree or a source flags its own claim as unverified, the text says so.

**Internal sources.** Repository files are cited by path (`docs/GRAPH.md`), commits by short hash, and RKA project entries (project `prj_01M6S9020MSHWB0HX2Z9C5SK5W`) by id. Every number about this project traces to one of these.

---

## Abstract

LLM agents start every session without the project knowledge they acquired earlier. Cross-session memory removes part of that cost, but memory that can change behaviour can also mislead it, and most current systems let the model that produced an experience decide that the experience is true. rka-memory is a research prototype that admits a record into the agent's context only when its validity rests on evidence the agent did not author: tests, exit codes, tool output and the user's typed words. Its organising predicate is valid(r, s) = grounded(r) AND applicable(r, s), computed by a deterministic engine over an append-only event ledger.

The report summarises the state of the art, the design, results from 392 live sessions with DeepSeek as agent and miner, and the evaluation plan. At the PI's request it also states how the design differs from the one in Anthropic's "The components of agent memory" (2026-11-06): (1) compact, immutable, typed graph nodes from which full notes are composed, with corrections attached as new nodes rather than rewrites; (2) stability and traceability from immutability and a deterministic projection; (3) typed edges and delivery provenance that let a wrong line of work be traced from its root and withdrawn from every view; (4) possibly better KV-cache reuse from an append-mostly memory prefix; (5) validity gating by code rather than model judgment. Each difference is stated as a hypothesis with a planned measurement. Anthropic's design is stronger in cross-session synthesis, salience judged by strong models, and measured gains with per-question ground truth; our evaluation is small, uses one model family and shows no success-rate effects so far.

---

## 1. Introduction

### 1.1 Problem

Anthropic defines agent memory as knowledge a model gains from experience and keeps between sessions as readable context, and frames each note as a prediction that a future agent will rely on it without re-checking (Martin 2026 [vendor]). That framing makes the central risk explicit: a note that is wrong is relied on in the same way as a note that is right. The article itself lists "a wrong note propagates everywhere" among the risks of its top memory layer.

Independent work supports the concern. The CL-Bench paper finds that dedicated memory systems do not beat naive in-context learning and that accumulated state frequently hurts (Asawa et al. 2026 [preprint]). In VibeMemBench, irrelevant memories lowered scores by 0.7 to 3.4 points [preprint]. MINJA reached a 98.2% average injection success rate with ordinary user queries alone, by inducing the agent to write the poisoned record itself (NeurIPS 2025 [peer-reviewed]). The project's first literature survey (RKA `jrn_01M6S93VF2V3WGDX50C6ZPJ6QT`) named the shared weakness self-certification: Voyager, ReasoningBank, ExpeL, Mem0, A-MEM and Hermes all let the model that produced an experience certify it.

The project's direct motivation is earlier work by the PI's group (Fu and Yang, ARR October 2026 submission 7523, under review): with shared structured memory on SWE-bench Lite, memory reduced the "unfinished" rate from 43.2% to 19.6% but raised the "claimed complete but wrong" rate from 19.9% to 31.3% (RKA `jrn_01M6S935R57CPF0Y9T7NQEM6P6`). Memory made the agent more confident without making it more correct.

### 1.2 Thesis

The PI fixed one theoretical line for the project (decision `dec_01M6S956E1X6KYYMMK3E7795FM`):

    valid(r, s) = grounded(r) AND applicable(r, s)

Cross-session self-improvement should come only from records whose validity is established by non-agent evidence. A record is grounded when it cites at least one counted event produced by the runner, a tool, the environment or the user, never the model's own text; it is applicable in state s when its scope covers s and no counter-evidence inside that scope has arrived. Records move along a ladder L0D, L1, L2, L3, L4; only a deterministic engine moves them, and only valid L2 and L3 records reach the agent. A refinement approved on 2026-11-13 requires grounding evidence to be produced independently of the record's own delivery (Definition 7; Planned, approved). The PI's goal is an implementable open-source prototype, with a paper to follow from its logs (`jrn_01M6S927YWJZA9EGRCAP0B9DGJ`, `dec_01M6S95E394B7M9VHDV3113MJ6`).

### 1.3 Contributions

1. **A memory data model**: an append-only typed event graph with compact immutable record nodes, in which every later change is a new event and every view is derived by code (Implemented for the ledger, records, typed edges, packets and skills; general composition and route-level retraction Planned).
2. **A deterministic validity gate** that admits records only on counted non-model evidence, each rung change naming its causes (Implemented), refined to delivery-independent evidence (Planned, approved).
3. **A structured comparison with Anthropic's design** along five axes, each a falsifiable hypothesis with a measurement (Section 3).
4. **Measurements on 392 live sessions**: wrong claims the agent cannot check were followed in 30/30 ungated and 0/30 gated sessions; user rules held in 6/6 gated and 0/6 no-memory sessions; 45% of support for threshold-promoted lessons came from sessions that had already received them (Section 6).
5. **An evaluation plan** with a pre-registered step-1 criterion, CL-Bench Codebase Adaptation on DeepSeek, and measurements for each comparison claim (Section 7).

---

## 2. Background and related work

This section condenses Section 2 of the Chinese report. The anchor article is treated separately in Section 3.

### 2.1 Production systems

**GitHub Copilot Memory** [vendor] is the only shipping product found that checks a memory against ground truth before use. Each memory carries citations (file:line); GitHub chose "store with citations, verify just in time" over offline curation, and before applying a memory the agent checks its citations against the current branch, re-storing it if valid and storing a correction if not. Copilot "makes this judgment itself, so it isn't guaranteed". Expiry (now a 90-day default, 28 days at public preview) is refreshed by validated use, not by retrieval. Vendor A/B tests report a pull-request merge rate of 90% with memory against 83% without (p < 0.00001).

**Claude Code auto memory and CLAUDE.md** [vendor]. CLAUDE.md is written by the user; auto memory by Claude. Both are "context, not enforced configuration". Auto memory stores four note types (user, feedback, project, reference), skips anything derivable from the code, and loads the first 200 lines or 25 KB of `MEMORY.md`; code checks size, not truth. **Managed Agents memory stores** [vendor] keep an immutable, session-attributed version of every mutation for 30 days and warn that, in `read_write` mode, a successful prompt injection can write content later sessions read "as trusted memory".

**Other products** [vendor]. An independent audit of 2,050 real ChatGPT memories from 80 users found 96% created without the user asking and 84% with direct support in the conversation (WWW 2026 [peer-reviewed]). OpenAI Codex Memories calls itself a "helpful recall layer"; its only provenance control, `memories.disable_on_external_context`, excludes sessions that used MCP or web search and is off by default. Letta's dreaming updates memory by a background subagent, never against external truth. Zep and Graphiti keep invalidated facts with validity windows and trace them to source episodes (arXiv 2501.13956 [preprint], vendor-run). LangMem and mem0 (arXiv 2504.19413 [preprint]) reconcile with an LLM. Cursor and Windsurf moved from auto-generated memories back to user-maintained rules without publishing reasons.

Only Copilot ties a memory's lifetime to successful verification; all published effect sizes are vendor-reported.

### 2.2 Practitioner patterns

**Karpathy's LLM Wiki** [practitioner, GitHub Gist, 2026-04-04] has an LLM maintain a persistent wiki between the user and immutable raw sources, so that knowledge is compiled once rather than re-derived per query. "The LLM owns this layer entirely"; operations are ingest, query (good answers are written back as pages) and lint (contradictions, stale claims, orphan pages). There is no citation check and no measurement. Mapped onto this project (`jrn_01M7BCEMEVH2VJ28A4JW9728NK`), raw sources correspond to the ledger, pages to records and edges, ingest to the miner, lint to the validity engine, `index.md` to the packet and `log.md` to the ledger. The difference is who decides truth: the LLM there, code here; written-back answers are the self-generated text this design keeps at L0D. **Angie Jones's reading** [practitioner, 2026-06-08] leaves deduplication, decay and the separation of one-off from reusable knowledge to the agent.

**LifeOS** (Daniel Miessler, formerly PAI) [practitioner, v8.761.0]. Its memory reviewer (`jrn_01M7B47643N7KMC8XVZ0JST5N0`) reads the last 20 user and assistant turns with tool_use and tool_result blocks skipped, so it never sees tool output, exit codes or tests; one Sonnet call rewrites hot-tier memory files with no human step, and identity edits apply at self-rated confidence of at least 0.70. Code enforces integrity (a path whitelist, at most 48 entries of 256 characters per file), not validity. Worth adopting: realpath-resolved write whitelists, failing closed on corrupt configuration, and "write declarative facts, not instructions".

### 2.3 Experience and skill memory

Most cited cross-episode memory methods let an LLM decide what to write. ExpeL [early] keeps insights by LLM votes and deletes them at zero importance. ReasoningBank [peer-reviewed, ICLR 2026] labels success with the agent's own backbone "without any ground-truth reference"; that judge agreed with ground truth 72.7% of the time on WebArena-Shopping. ACE [peer-reviewed, ICLR 2026] documents context collapse: iterative rewriting collapsed an 18,282-token context at 66.7% accuracy to 122 tokens at 57.1%, below the 63.7% no-adaptation baseline. In the Darwin Gödel Machine [peer-reviewed, ICLR 2026] one variant removed logging of special tokens to obtain a perfect hallucination-detection score: a check is external only if it lies outside the agent's write access. Methods that do rely on non-agent signals, such as Self-Generated In-Context Examples [peer-reviewed, NeurIPS 2025], mostly rely on hidden checkers or benchmark rewards that exist only in research environments.

### 2.4 Gated and staged admission

A group of 2026 preprints gate memory on verification. MemGuard [preprint, arXiv 2608.21867] activates a record only above verifier thresholds; its LLM verifier agreed with human audits 86% and 83% of the time. FGSE [preprint, arXiv 2611.14326] commits a lesson only after a probe with "executable or externally checkable signals" (3.6% harmful updates on DeepSeek-V3.1). TEPA [preprint, arXiv 2608.07429] revokes a keyed precedent on contradicting evidence and keeps it for audit. StateAuditor [preprint, arXiv 2608.01619] binds each LLM-proposed state transition to a code-checked citation and reports a paired +5.0 points on STALE (95% CI +2.9 to +7.2), against +0.6 for a budget-matched control: the clearest evidence that code-checked citation itself helps. Proof-of-Execution Memory [preprint, arXiv 2608.16032] uses an HMAC-chained ledger writable only by a trusted action layer. No work found implements a full ladder or promotes on confirmation by code in k independent sessions; reviewer reliability caps self-assessed memory (72.7% for ReasoningBank, 83% to 86% for MemGuard).

### 2.5 Poisoning and self-reinforcing memory

**Attacks** increasingly rely on the agent writing the poisoned record. MPBench [preprint, arXiv 2606.04329] reports a 50.46% mean attack success over OpenClaw and HERMES and names "each successful execution is treated as validation" as an attack surface (V-S5). Other vectors arrive as user turns or user files: PMPA [preprint] hides instructions in benign external sources; Microsoft [vendor, 2026-02-10] found over 50 links from 31 companies that pre-fill prompts asking assistants to remember a company as trusted. Authorship checks alone therefore do not establish trustworthiness.

**Self-reinforcement.** Memory Reward Inflation in Self-Improving LLM Agents [preprint, arXiv 2608.00017] shows that with LLM self-assessed scores, wrong episodes are over-scored and preferentially reused (the "Echo Gap"), and proves an error-independence assumption (EIA) necessary: the correction signal must track ground truth with errors uncorrelated with the memory's bias. Xiong et al. [peer-reviewed, ACL 2026] find "experience following": outputs mimic retrieved records, so stored errors propagate, and future task evaluations used as "free quality labels" are self-fulfilling when they occur after delivery. Self-Cleaning and Captured Anyway [preprint, arXiv 2609.25052] observes bimodal capture in append-and-retrieve loops (aggregate means describe 8.2% of runs). Manufactured Confidence [preprint, arXiv 2606.29279] shows consolidation rewriting hedged statements as confident facts.

**Tests are weak evidence on their own.** ImpossibleBench [peer-reviewed, ICLR 2026] makes unit tests conflict with the specification: GPT-5 cheated in 54.0% of Conflicting-SWEbench tasks, and LLM monitors caught 42% to 65%. PatchDiff [peer-reviewed, ICSE 2026] finds that 7.8% of SWE-bench patches judged correct fail the full developer tests.

**Defences** carry costs. In Utility Under Attack [preprint, arXiv 2608.21230], write-time screening rejected 0 of 360 poisoned memories, and the authors conclude that separating true from false statements "generally requires grounding outside the text"; strong provenance constraints raised accuracy from 0.3167 to 0.7000 but fell to 0.0417 when the answer-bearing evidence came from an untrusted source. Self-fulfilling support is consistent, mutually supportive and grows over time, the opposite of what consensus defences assume.

### 2.6 Benchmarks

**CL-Bench** [preprint, arXiv 2606.05661, 2026-06-04; Apache-2.0 framework] defines gain as a system's stateful reward minus the same system's stateless reward, runs five rollouts per system and task, and finds naive ICL best (normalised gain 25.4%; ACE 8.6%). Its **Codebase Adaptation** task has 19 SWE-bench problems (9 from jazzband/tablib, then 10 from jd/tenacity), each in a fresh container, judged by hidden regression tests, with reward r = 1 - (steps - 1)/40 and 0 if unsolved; ACE cost on average $62.8 per rollout. **VibeMemBench** [preprint, arXiv 2609.23570] filters to targets where memory could help; even so, deepseek-v4-pro with frozen verified experience scored 67.1 against 67.1 without, and 11 of 12 solver and memory combinations did not beat no memory. Other relevant benchmarks include Evo-Memory, LifelongAgentBench, MemoryAgentBench, STALE and MemSecBench [preprint], and MemoryBench (ICML 2026 in one note, preprint in another; unverified). No coding benchmark combines a multi-session simulated user, hidden project conventions and drift. Reusable recipes: same-system stateless baselines, filtering to tasks where memory can matter, step rewards, injected drift, harm controls, several seeds with session pairing and cluster-robust errors (Miller, arXiv 2411.00640, status unverified), and counting memory-side tokens.

### 2.7 User-stated rules versus model-written lessons

Human-authored guidance tends to beat self-written guidance: SkillsBench (reported as a NeurIPS 2026 poster; numbers from search snippets, not verified verbatim) found curated skills +16.2 percentage points on average and self-written skills -1.3, and "Evaluating AGENTS.md" (arXiv 2602.11988; type unverified) found LLM-generated context files often lower success. Self-written memory helps when an external correctness signal exists, as in Anthropic's CL-Bench runs, and fails when the signal is self-assessed ("Honest Lying" [workshop, ICML 2026]: 0 of 121 reflections named the correct target; programmatic failure signals raised this to 86%). Stated preferences are lost without dedicated mechanisms (PrefEval [peer-reviewed, ICLR 2025]: most models below 10% adherence after 10 turns). No benchmark compares the utility of user-stated and model-inferred memories.

### 2.8 Gaps

The survey yields six gaps: (1) no system uses code-checked, non-agent evidence to decide trust; (2) no system gates on evidence independent of the memory's own delivery, although the EIA result says this is necessary; (3) no full ladder and no "k independent sessions confirmed by code" rule; (4) no precedent for scope narrowing; (5) no comparison of user-stated and model-inferred memory; (6) no coding benchmark with simulated users, hidden conventions and drift.

---

## 3. Comparison with Anthropic's design

### 3.1 The anchor and what it does well

"The components of agent memory" (Martin, Anthropic, 2026-11-06 [vendor]; RKA `lit_01M7B6C46EXCZDK3FNJEWHW423`) and its companions (the Dreams API [vendor, research preview], the "Dreaming with a dynamic workflow" cookbook [vendor, 2026-11-02], Managed Agents memory stores [vendor], Claude Code memory documentation [vendor]) describe memory as readable notes Claude writes from experience. Its contributions are substantial:

- **Memory works when the signal is right.** On a CL-Bench 1.0 database task in which Claude is shown the correct answer after each question and may write memory, mean reward rose from 0.655 to 0.838 for Opus 5.5, 0.644 to 0.833 for Sonnet 5.5, and 0.564 to 0.828 for Opus 5. These are Anthropic's own runs; no run counts or variances are given, and the protocol appears to differ from the CL-Bench paper's database task, so the numbers are not comparable with the paper's tables.
- **Salience is best left to strong models.** A fixed "record only failures" rule cut the memory gain by 0.234 for Opus 5, 0.150 for Opus 4.7 and 0.020 for Sonnet 4.6. In Pokémon Red, Sonnet 3.7 wrote 413 files and earned one badge; Fable 5 wrote 10 files and earned four.
- **Dreaming corrects and synthesises.** An offline pass re-reads transcripts to fix, merge and promote notes. A wrong PokéCenter note caused repeated looping until dreaming corrected it; the corrected run reached Cerulean City at action 1,935. On a large documentation corpus, wiki-only accuracy rose from 13% to 48% as dreaming scaled from 2 to 128 sessions, and wiki plus search reached 61% against 53% for search alone.
- **Reviewable consolidation.** The Dreams API writes a new output store from an input store plus 1 to 100 sessions and never modifies the input store, so output can be reviewed and discarded. Managed memory stores keep an immutable, session-attributed version of every mutation for 30 days.

The article also names the risks that motivate this project: token cost, "a wrong note propagates everywhere", and lower adherence as instruction files grow.

### 3.2 Side-by-side summary

| Aspect | Anthropic (article and companions) | rka-memory |
|---|---|---|
| Unit of memory | Markdown notes, topic files and wiki pages written by the model; a `MEMORY.md` or `_index.md` index | A record node (typically one sentence) introduced by one `record_proposed` event, plus the events that ground it (Implemented) |
| Mutability | Files are rewritten in place by the model; store-level immutable versions for 30 days; Dreams writes a new store | Record content, kind and declared relations never change; every later change is an appended event (Implemented) |
| Typing of notes and links | Claude Code auto memory labels notes with one of four types; links between pages are untyped markdown links | Typed nodes (events, records of four kinds, actors, anchors) and typed edges (evidence with polarity, depends_on, refines, supersedes, governs, about, delivered) (Implemented) |
| How a note is corrected | A model re-reads transcripts and rewrites or merges notes; unsupported claims are left in place; ambiguous contradictions are kept and marked "CONFLICT" | By new events and nodes: counter-evidence, user withdrawals and rejections, a refining record or a superseding record (Implemented); a dedicated amendment kind (Planned (proposed)) |
| How context is assembled | An always-loaded top layer (first 200 lines or 25 KB of `MEMORY.md`; an `_index.md` up to 4,096 characters in the cookbook), plus files the agent reads on demand | A packet built by code from valid records, grouped with their refinements and dependencies, within 1,200 estimated tokens; skills rendered from a procedure and its dependencies (Implemented); general composition by traversal (Partially implemented) |
| What decides what reaches the agent | The model's salience judgment when writing; dreaming decides promotion; code enforces size limits only | A deterministic engine: only records grounded by counted non-model evidence and applicable to the session state (Implemented) |
| Provenance | Store versions record which session wrote a change; wiki agents may cite sources | Every rung change names its cause events; every delivery is a runner-written event; a trace check verifies that each delivered record has grounding evidence linked before delivery (Implemented) |
| Pruning a wrong line of work | A model must search notes and transcripts for what the wrong note influenced and rewrite it | Dependency propagation discounts evidence from sessions where an invalid dependency was delivered (Implemented); route-level retraction of a root and its dependent closure (Planned (proposed)) |
| Cache behaviour | Not addressed in the sources reviewed; rewriting the injected layer changes the prompt prefix | Append-mostly records could give a stable prefix, but the current builder orders by recency and renders volatile metadata, which defeats this (Planned (proposed): stable order and cache-hit logging) |
| Measured evidence | CL-Bench variant gains for six Claude models; wiki accuracy; fact retention in a contract task; Pokémon Red (all vendor-run) | 5 live streams, 29 ledgers, 392 sessions, DeepSeek only; harm prevention and user-rule retention measured; no success-rate effect measured yet |

The following subsections take the PI's four points (D1 to D4) and the validity gate (D5) in turn.

### 3.3 D1: atomic immutable nodes and composed notes

**Anthropic's approach.** Notes are files the model writes and edits through file tools (the memory tool offers view, create, str_replace, insert, delete and rename). Dreaming merges duplicates, replaces "stale or contradicted entries ... with the latest value" (Dreams documentation) and builds wiki pages. Two mechanisms deserve credit: the Dreams API never modifies its input store, so a consolidation can be reviewed and discarded, and managed stores keep immutable versions of every mutation for 30 days. Immutability thus exists at the level of store versions; what the agent reads, however, is the current rewritten text, and version history is not assembled into context.

**Evidence from Anthropic's own article that rewriting loses information.** In the contract-terms case, agents given generic instructions overwrote a shared provisions table and only 41 of 57 graded facts survived (72%); after a reflect, propose and rebuild loop with one page per contract quoting terms verbatim, 57 of 57 survived. One wiki grew to 203 KB from a 115 KB corpus (1.77 times its corpus, 68 pages, 255 links, some orphan pages) and "effectively abstracted nothing". The knowledge-wiki quickstart [vendor] reports that about 5% of lines filed to the busiest shared page are overwritten. Outside Anthropic, ACE's context collapse (Section 2.3) and Manufactured Confidence (Section 2.5) document the same failure of iterative rewriting.

**Our approach and mechanism.** The ledger is an append-only JSONL file of typed events (`docs/LEDGER.md`). A record is created by one `record_proposed` event whose specification (`RecordSpec`: kind, content, initial scope, steps, acceptance check, declared relations) is frozen. Content is usually one sentence; the packet renders at most 400 characters per record (`MAX_CONTENT_CHARS`). Every later change is a new event: `evidence_linked` (with polarity), `rung_change` (with `cause_ids`), `scope_narrowed`, user verdicts, deliveries. Relations are typed edges declared by the newer record in its own immutable specification:

- **refines**: a record that qualifies another (an exception, limit, side effect or condition);
- **supersedes**: a newer version replaces an older one in views; this is reversible, because if the newer record falls below L2 the older one returns to views by itself;
- **depends_on**: a record holds only because another record was followed;
- **governs**: derived in code, never by a model: a rule (a preference or constraint record, typically the user's) names a command that a procedure's shell steps or acceptance check run (`rka_memory/relations.py`);
- **about**: anchors to files, commands and tools, derived from event payloads.

"Attaching an amendment node" therefore already exists in three forms: a refining record, a superseding record (for a user's rule, only through the user's own quoted words, `docs/GRAPH.md` decision 1), and a user withdrawal or rejection recorded as a counter-event. Full notes are composed from the graph at read time:

- packets keep a record and its refinements and dependencies in one group (`_groups` in `builder.py`) (Implemented);
- `SKILL.md` is rendered at export from a procedure plus its dependencies and governing rules at L2 or above, by reference rather than by copying, so a withdrawn rule leaves the skill at the next export (`docs/GRAPH.md` phase 3) (Implemented);
- `rka-memory explain <id>` shows a record's evidence, rung history with causes, relations in both directions and deliveries; `rka-memory graph --record <id>` returns its neighbourhood (Implemented).

**Implementation status.** The immutable record, typed relations, group-preserving packets, rendered skills and explanation views are Implemented. A dedicated amendment kind (a node type whose only role is to amend another) and a general `compose(r)` traversal used for every view (Definition 8) are Planned (proposed). GRAPH.md also describes a `relation_proposed` event for relations added after a record exists; no such event kind is present in `domain.Kind`, so relations can currently be declared only by the newer record at its proposal (Planned).

**Expected benefit (hypothesis H-D1).** Compared with a model-curated notepad: (a) fewer tokens per delivered memory item and per session context at equal coverage; (b) fewer bytes written per memory update, since an update appends one event or one short record rather than rewriting a file; (c) higher retention of graded facts across updates, since nothing is overwritten.

**Measurement (Section 7.6).** Tokens per delivered item and per injected session context; bytes written per update (ledger append size against bytes written by the notepad's file tools); fact retention across updates on a planted fact list, following Anthropic's contract protocol.

**Caveats.** A compact node does not imply a compact context: composition can repeat shared neighbours and costs compute. An append-only store grows without bound (the former host ledger reached 13.9k events over 16 sessions, and the host spool 15 MB; Chinese report Sections 4.3 and 5.7), so snapshots of the projection will be needed. Atomic records give up the cross-source synthesis behind Anthropic's wiki gains. A one-sentence record can omit context needed to apply it; scope and refinements should carry that context, but live miners rarely declared relations (Section 6.7). Immutability preserves errors as faithfully as facts: a wrong record is only ever countered, superseded or withheld.

### 3.4 D2: stability and traceability

**Anthropic's approach.** Correction depends on a model re-reading transcripts. The cookbook's analyser leaves claims with no evidence in place (another transcript may support them), fixes or notes contradicted claims, downgrades confident claims with uncertain evidence, and keeps ambiguous contradictions marked "CONFLICT". There is no human review by default; the cookbook warns that its nightly in-place script lets a bad transcript reach the agent the next morning and recommends writing a new store, reviewing it, then switching. The sources reviewed give no verification criterion for promotion into the always-loaded layer; the criteria they mention concern size and access frequency. Store versioning with point-in-time recovery and the write-new-store Dreams design deserve credit.

**Our approach and mechanism.**

- *Deterministic projection.* World state is a deterministic function of the ledger and a clock (`rka_memory/world.py`; the engine "is deterministic: the same ledger and the same `now` always give the same moves", `docs/LADDER.md`). `rka-memory replay` rebuilds the projection from the ledger alone, and replay tests in `tests/test_engine.py` pin every rule (Implemented).
- *Causes.* Every `rung_change` names its cause events (Implemented).
- *Delivery record.* Each `packet_delivered` event records the delivered record ids, the arm, the channel and the ledger revision, so what reached the agent in any session is recoverable from the ledger (Implemented).
- *Trace check.* `rka-memory graph --trace` checks that every delivered record traces to grounding evidence linked before the delivery and exits 1 otherwise; the workload runner runs it for every session as the check "every delivered record traces to evidence" (`rka_memory/workload/runner.py`) (Implemented). On the second house-rules run all 20 record deliveries traced (`docs/GRAPH.md`).
- *No deletion.* Retirement to L4 keeps the record so that the same content cannot be re-promoted; supersession hides a record from views only while its successor is valid (Implemented).

**Expected benefit (hypothesis H-D2).** (a) Every delivered item traces to grounding evidence that precedes its delivery, whereas a rewritten notepad's injected sentences cannot be mapped to evidence without a model's help; (b) replaying a ledger reproduces every rung and every delivered record set exactly; (c) the injected context changes less between consecutive sessions, because only appended or withdrawn records change it.

**Measurement.** Traceability rate of delivered context; replay determinism (rung sequences and delivered sets over repeated replays); context churn, the normalised edit distance between consecutive sessions' injected memory text (Section 7.6).

**Caveats.** Determinism belongs to the engine given a ledger. The miner is an LLM: two chained replays of the same commit kept 9 and 17 records at L2 or L3 (`docs/RESULTS.md`), so the end-to-end pipeline is not stable. Re-rendering a past packet's exact text also depends on the file system (the missing-file note). Traceability is not correctness: 45% of support for threshold-promoted lessons came from sessions that had already received them (Section 6.5), and the trace check passes such records. The project generates no free-text summaries; a composed view written by a model would need to be extractive and cite node ids to keep this property. The guarantees assume the adapter alone writes the ledger, which the operating system enforces in the dsh image but not on a default Claude Code host.

### 3.5 D3: typed graph and targeted pruning

**Anthropic's approach.** Wiki pages and notes are linked by untyped markdown hyperlinks. Claude Code auto memory does label each note with one of four types (user, feedback, project, reference), and the cookbook and knowledge-wiki quickstart mark contradictions and superseded figures in text, so Anthropic's notes are not entirely unlabelled; but no relation between notes is typed or checked by code, and no machine-readable edge says that one note was derived from following another. Store versions record which session wrote a change; the sources reviewed document no record of which sessions read a note. Finding everything a wrong note influenced therefore requires a model to search notes and transcripts. In the Mt. Moon case this worked for a single wrong note.

**Our approach and mechanism.** Nodes are typed: events (`user_msg`, `tool_result`, `exit_code`, `test_result`, `user_edit`, `user_undo`, `user_confirm`, ...), records (fact, preference, procedure, constraint), actors (user, runner, tool, env, model) and anchors (files, commands, tools). Edges are the relations of Section 3.3 plus evidence edges (event to record, with polarity) and delivery edges (session to record).

- *Dependency propagation* (`rka_memory/validity/propagate.py`, Implemented). When a record falls below L2, evidence observed in any session where it was delivered no longer counts for its dependents (`tainted_sessions`), and each dependent drops one rung per invalidating cause. User verdicts that name the dependent are exempt from the discount. Contamination is thus traced through delivery provenance: what was learned while a now-invalid record was in context is discounted.
- *Other corrections* (Implemented). A user rejection or a counted refutation demotes; supersession acts on views and is reversible.
- *Route-level retraction* (Planned (proposed)). Retracting a root node, such as a decision or approach found wrong, appends retraction events that remove its dependent closure (Definition 9: records that depend on or refine it, and records whose support no longer meets their rung once evidence from sessions where any closure member was delivered is discounted) from every view at once. Each member can be re-admitted by delivery-independent evidence. Nothing is physically deleted, which preserves D2. A decision or approach node kind, so that a "line of work" has an explicit root, is also Planned (proposed); today a wrong approach can be represented only as a procedure or constraint.

A property worth stating: delivery edges are written by the runner, not the miner. The delivery-tainted part of the closure can therefore be computed even when the miner failed to declare a dependency.

**Expected benefit (hypothesis H-D3).** After a root is refuted, no member of its closure is delivered from the next session on, and later behaviour shows no residual influence; a model-curated notepad needs several sessions, or never fully removes the influence, because the model must locate every affected note.

**Measurement.** A "wrong route" stream (Section 7.6): plant a wrong root decision with dependent records, refute the root, then measure sessions until no dependent is delivered and residual influence on behaviour, against an untyped model-curated notepad baseline.

**Caveats.** Declared edges are only as complete as the miner makes them: before the relation prompt was reworked, live miners proposed one relation across 38 records in six runs, and records derived from following a wrong claim in the `unverifiable` runs declared no dependency (`docs/RESULTS.md`). Whether `governs` belongs in the closure is open: GRAPH.md argues that a procedure which merely runs a command a rule governs should not fall with the rule; today a withdrawn rule only leaves the procedure's rendered rules section. Over-pruning is a risk, since a refinement of a wrong rule can itself be true. Retraction must come only from a user verdict or a counted refutation, never from a model-made edge, or it becomes a new laundering path. The cascade may take several gate runs because dependents drop one rung per cause.

### 3.6 D4: KV-cache reuse (hypothesis)

**Provider behaviour.** DeepSeek's context caching is enabled by default. A request hits only when its prefix fully matches a persisted cache prefix unit; units are created at request boundaries, at detected common prefixes across requests and at fixed token intervals. Usage reports `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens`. Caching is best-effort, without a guaranteed hit rate, and unused caches are cleared "usually within a few hours to a few days". The page does not state hit pricing (DeepSeek API documentation, "Context Caching" [vendor]; re-read 2026-11-13).

**Anthropic's approach.** The sources reviewed do not discuss cache reuse. Dreaming rewrites notes and the injected layer, which changes the injected text and therefore the prompt prefix from the first changed byte onward.

**Our approach and mechanism.** In dsh, the session-start packet is one system-prompt section (order 9000, after the tool sections and before structured output at 9900 and the persona suffix at 10200), fixed when the agent is created and kept for the whole session (`plugins/dsh/src/index.ts`, `plugins/dsh/README.md`) (Implemented). If immutable records were rendered in a stable order, with new records appended, the injected memory would be an append-mostly prefix, and consecutive sessions would share it up to the first change.

**Implementation status and present obstacles.** The current builder defeats prefix stability in two ways. It sorts groups by anchor relevance, then rung, then recency, newest first (`build()` in `builder.py`), so a new or newly touched record moves to the top. And each packet line renders volatile metadata, namely the rung tag, the grounding weight, the number of supporting sessions and, for a user rule, the date of the latest statement, so an unchanged record's line changes as evidence accrues. A stable order (oldest first, new nodes appended) with volatile annotations separated from stable text is Planned (proposed). Logging of `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens` is Planned (proposed); the code does not record them today.

**Expected benefit (hypothesis H-D4).** With stable-order rendering, the cache-hit ratio of input tokens on each session's first request is higher than with recency-order rendering and than with a rewritten notepad.

**Measurement.** Per-session cache-hit ratio of input tokens and input cost for stable-order packets, recency-order packets and the notepad baseline (Section 7.6).

**Caveats.** Correctness takes priority: retiring or demoting a record in the middle of the packet breaks the prefix after it, and withholding invalid records is not negotiable. Within a session, the packet does not change, so later requests in the session hit the cache whatever the memory design; the effect is confined to the first request of each session and to the text from the packet onward. Its upper bound in dsh's layout is therefore small: the packet budget is 1,200 estimated tokens, about 2% of the median per-session agent token count in step 1's interim runs (56,134 to 62,074 by arm, summed over every step). The effect could be larger where memory is larger (Claude Code loads up to 25 KB), placed earlier, or refreshed within a session, and it depends on the provider's cache lifetime. Whether dsh renders registered skills into an earlier system-prompt section, moving the first divergence forward, has not been established. We expect a small but measurable effect.

### 3.7 D5: validity gating

**Anthropic's approach.** The model judges salience at write time and correctness during dreaming; the article's own experiments show that this works well for strong models when a correct answer is revealed after each question.

**Our approach.** Only records whose grounding code has checked reach the agent. A link counts only if the event's actor is not the model, its kind can carry the link's polarity, it was in the ledger before the link, a user verdict names this record, a model-made link cites its own session, and the kind has a positive weight (`docs/LADDER.md`). Promotion from L1 to L2 needs counted weight of at least 2 across at least 2 sessions (3 sessions if only weak events), with no counter-event inside scope. A user's standing statement, checked verbatim by code, confirms a rule at once. The approved refinement requires that evidence be produced independently of the record's delivery (Definition 7; Planned, approved).

**Expected benefit and existing evidence.** Withholding unvalidated records prevents harm the agent cannot detect: in the `unverifiable` stream, 30/30 ungated sessions followed three wrong claims and 0/30 gated sessions did (Section 6.3). The gate's own misapplication metric did not see that harm, so the plan measures it with hidden checks.

**Caveats.** The gate cannot admit true knowledge that has no non-agent evidence, such as untested architectural understanding (the cost Utility Under Attack documents for strict provenance constraints). Events checked today by actor ("who emitted it") are not necessarily independent of the agent ("who caused it"), which is what the refinement addresses.

### 3.8 Where Anthropic's design is stronger

- **Cross-session consolidation and synthesis.** Wiki-only accuracy rose from 13% to 48% with dreaming, and wiki plus search reached 61% against 53% for search alone. rka-memory has no cross-record synthesis, and whether synthesis can be added without admitting self-generated text is open.
- **Salience judged by strong models.** Fixed write rules cost strong models up to 0.234 mean reward. rka-memory restricts trust, not writes (L0 observations remain free), but there is no data yet showing that gating trust does not remove part of a strong model's gain when per-question ground truth is available.
- **Measured gains with ground truth.** Anthropic reports large gains on a public benchmark family with per-question answers for six models. Our evaluation is small (one fixture family, one model family, two runs per condition), and every live stream so far passed its test suite in every session, so no success-rate effect could be measured.
- **Engineering maturity.** Store versioning, reviewable consolidation, access modes and hash preconditions are production features; our trust boundary is enforced only in a research Docker image.

---

## 4. Design

### 4.1 Overview

Host adapters (a dsh plugin, or Claude Code hooks) append events to a local ledger. At session end, the rule extractor and the miner read the session's events, propose candidate records and link evidence. The validity engine moves rungs by deterministic rules and writes one `rung_change` per move. At the next session start, the packet builder takes the records valid for the current state, groups and orders them, truncates them to the budget, injects them into the context and records the delivery. A skill exporter renders procedures at L2 or above as `SKILL.md`. Metrics are computed from the ledger alone. `rka-memory sync` copies the ledger into an RKA Core project as durable storage.

### 4.2 Data model

**Definition 1 (event, ledger).** An event is e = (id, σ, t, κ, a, p, h) with session σ, timestamp t, kind κ, actor a in {user, runner, tool, env, model}, payload p and state hash h. A ledger is a finite sequence L = e_1 ... e_n whose only write operation is append. A table A maps each kind to the actors allowed to author it (`domain.KIND_ACTORS`); the adapter sets the actor, and constructing any other pair raises (Implemented).

**Definition 2 (projection).** W = π(L, now) is a deterministic function giving sessions, records, rungs ρ(r), effective scopes, evidence links and deliveries. For any prefix L_{≤i}, π(L_{≤i}) is the state at ledger position i (Implemented).

**Definition 3 (record node).** A record r is introduced by exactly one event of kind `record_proposed` (actor model) whose payload fixes spec(r) = (kind in {fact, preference, procedure, constraint}, content, initial scope, falsifier, confidence, steps, acceptance check, depends_on, supersedes, refines, about). Every later fact about r is a function of later events: `evidence_linked`, `rung_change` (with cause ids), `scope_narrowed`, user verdicts and `packet_delivered` (Implemented).

**Definition 4 (typed edges).**

    E_ev   ⊆ Events × Records × {+, -}   from evidence_linked (miner, rule extractor, user verdicts)
    E_dep, E_ref ⊆ Records × Records      declared in spec(r) of the newer record
    E_sup  ⊆ Records × Records            declared in spec(r); acts on views only
    E_gov  ⊆ Records × Records            derived by code: rule g names a command procedure q runs
    E_about ⊆ Records × Anchors           declared, checked against the proposing session's anchors
    E_del  ⊆ Sessions × Records           from packet_delivered, written by the runner

Only evidence edges can change a rung; the other edges change what views select or how they render (Implemented).

**Definition 5 (counted evidence).** A link (e, r, polarity) counts iff (g1) actor(e) is not model; (g2) kind(e) is a grounding kind able to carry the polarity; (g3) e precedes the link in L; (g4) a user verdict counts only for the record it names; (g5) a model-made link cites only events of its own session; (g6) for support, kind(e) has positive weight w(e). Weights: 1 for `exit_code`, `test_result`, `user_confirm`, `user_reject`; 1/3 for `user_edit`, `user_undo`, `user_repeat`, `user_msg`, `tool_result`. Let G+(r) and G-(r) be the counted support and counter events inside scope(r) (Implemented).

**Definition 6 (validity).**

    grounded(r)       ⇔ G+(r) ≠ ∅, and for a preference, G+(r) contains a user-authored event
    applicable(r, s)  ⇔ scope(r) covers s  ∧  G-(r) = ∅ (counters before the latest user confirm excluded)
    valid(r, s)       ⇔ ρ(r) ∈ {L2, L3} ∧ grounded(r) ∧ applicable(r, s)

Promotion L1 to L2 requires Σ_{e ∈ G+(r)} w(e) ≥ k = 2 over at least 2 distinct sessions (3 if every supporting event has w < 1), with G-(r) empty. Support from sessions where an invalid dependency of r was delivered is excluded, except user verdicts that name r (`engine.assess`, `engine.valid`) (Implemented).

**Definition 7 (delivery-independent grounding; Planned, approved).** Let Del(r) be the sessions in which r was delivered. An event e is delivery-independent for r iff session(e) ∉ Del(r), or e is the result of a check that session(e) did not create or modify, or e is user-typed. grounded*(r) requires delivery-independent counted support; post-delivery support is discounted, post-delivery counter-evidence counts in full (Section 7.3).

**Definition 8 (composition by traversal).** For a record r valid in state s,

    N(r, s) = {r} ∪ {x : (x, r) ∈ E_ref, valid(x, s)} ∪ {d : (r, d) ∈ E_dep, ρ(d) ≥ L2}
                  ∪ {g : (g, r) ∈ E_gov, ρ(g) ≥ L2}   minus records hidden by a valid superseder
    compose(r, s) = render(N(r, s)) in a fixed order

Partially implemented: packets group connected components of E_dep ∪ E_ref among selected records; `SKILL.md` renders a procedure with its dependencies and governing rules at L2 or above; `explain` returns the neighbourhood. A single `compose` used by every view is Planned (proposed).

**Definition 9 (retraction closure; Planned (proposed)).** Let x be a root retracted by a user verdict or a counted refutation.

    C_0     = {x}
    C_{i+1} = C_i ∪ {y : (y, z) ∈ E_dep ∪ E_ref for some z ∈ C_i}
                  ∪ {y : support of y outside sessions Del(C_i) no longer meets the threshold of ρ(y)}
    C(x)    = the least fixpoint

Views exclude C(x) until each member other than x regains its threshold from delivery-independent evidence. Whether E_gov joins the first clause is a design parameter (Section 3.5). The Implemented special case is `propagate.py`: y declares depends_on z, z falls below L2, and support from sessions where z was delivered is discounted.

**Definition 10 (packet assembly).** For arm α and session state s,

    V_gated(s)  = {r : valid(r, s)} \ Sup(s)
    V_stated(s) = {r ∈ V_gated(s) : affirmed(r)}    (a user confirm or a code-checked standing statement, no later counter)
    V_ungated(s)= {r : ρ(r) ∈ {L1, L2, L3}, scope(r) covers s} \ Sup(s)
    P(s, α)     = truncate_B( sort_desc( groups(V_α(s)), key = (touches a session anchor, max rung, max recency) ) )

Groups are connected components of depends_on and refines; within a group records are ordered by number of dependencies, then proposal position; truncation drops whole groups beyond the budget B = 1,200 estimated tokens, with tokens estimated as characters / 4 + 1 (Implemented).

### 4.3 Components and parameters

| Component | Responsibility | Status |
|---|---|---|
| C1 Ledger and trust boundary | Append events; actor set by adapter; sync to RKA Core | Implemented |
| C2 Miner and rule extractor | Propose candidates and link evidence; extract the user's rules as verbatim quotes | Implemented |
| C3 Validity engine | The only code that writes `rung_change` and `scope_narrowed`; deterministic | Implemented; delivery-independent rules Planned (approved) |
| C4 Packet and arms | Arms `gated`, `none`, `ungated`, `stated` | Implemented (`stated` in c0879f9) |
| C5 Skills | `SKILL.md` export and the L3 gate | Implemented |
| C6 Metrics | Misapplication, regressions, rediscovery tokens, verification cost | Implemented; Core dashboard page Planned |

Parameters from `rka_memory/default.yaml`:

| Parameter | Value | Meaning |
|---|---|---|
| `gate.k` | 2.0 | Weighted grounding needed for L1 to L2 |
| `gate.min_sessions` | 2 | Distinct sessions among that grounding |
| `gate.weak_only_min_sessions` | 3 | Sessions needed when no supporting event has weight 1 or more |
| `gate.skill_min_sessions` | 2 | Later sessions that followed a procedure, for L2 to L3 |
| `gate.retire_after_days` | 60 | An untouched L1 record retires to L4 |
| `gate.confirm_overrides` | true | A user confirm promotes straight to L2 |
| `gate.narrow_priority` | [] | Scope narrowing off |
| `gate.standing_phrases` | 14 English and 13 Chinese phrases | Words that make a user's rule standing ("from now on", "以后", ...) |
| `weights` | 1.0 or 1/3 | As in Definition 5 |
| `packet.budget_tokens`, `hints_budget_tokens` | 1200, 600 | Packet and hint budgets |
| `packet.arms` | [gated, none] | Rotated by calendar day; `ungated`, `stated` optional |
| `scope.fields` | [project, toolchain] | Fields a record's scope may constrain |
| `signals.repeat_threshold`, `dedup_threshold` | 0.8, 0.75 | Repeat detection and candidate merging |
| `signals.test_commands` | 11 runners | Only plain invocations of these count as test results |
| `miner.backend`, `events_max_chars` | claude_cli by default; 200,000 | Experiments use `openai_compat` pointed at DeepSeek |
| `miner.extract_rules`, `graph.anchors` | true, true | Rule extractor and anchors on |

**Moves** (`docs/LADDER.md`, all Implemented). A proposal enters L1 with counted support, else L0D (never delivered). A counter-event inside scope demotes L2 to L1, retires L3 to L4, and permanently blocks an L1 record. A refutation (a miner counter link citing a tool result or user message from a session that never received the record) blocks like a rejection. A user confirm promotes to L2. L2 to L3 requires a procedure whose steps were followed in 2 later in-scope sessions ending on a successful exit code or test; its acceptance check is frozen into the promotion and observed, never executed. Verdicts are typed by the user (CLI or an `rkm`-prefixed prompt), never called by the model; scope is frozen at project and toolchain with narrowing off (step 0, commit 7ac59e5).

### 4.4 Hosts

Decision `dec_01M6SBEM7JBD98MDEKWP53B1C5` made the DeepSeek Harness (dsh) the research host. On 2026-11-13 the PI clarified that they do not intend to use rka-memory on their own machine (`jrn_01M7B46HF6P45SC73SEF2AXGSN`), and the host hooks were uninstalled (`jrn_01M7B4C1P6SD1RV8P6DZCBY4ZB`). Consequently:

- **dsh in Docker with DeepSeek is the research host** (Implemented). The plugin lives in `plugins/dsh`; DeepSeek is both agent and miner. Writes and reads of the ledger by the agent are blocked by the operating system: tools run in dsh's sandbox under a dropped `agent` user, the ledger directory is root-owned with mode 700, the image filesystem is read-only, and a canary check runs before each stream (`docs/LADDER.md`).
- **The Claude Code adapter** (hooks, `--harden`, MCP server) is Implemented but not used for evaluation; its read protection is Planned. Evaluation uses task streams only.

---

## 5. Implementation status

| Mechanism | Status | Where |
|---|---|---|
| Append-only typed event ledger; actor table | Implemented | `docs/LEDGER.md`, `domain.py` |
| Deterministic projection; `rka-memory replay`; replay tests | Implemented | `world.py`, `tests/test_engine.py` |
| Immutable record specification | Implemented | `RecordSpec` |
| Six grounding rules; weights | Implemented | `engine.py`, `default.yaml` |
| Ladder L0D to L4; rung changes with cause ids | Implemented | `docs/LADDER.md` |
| Scope narrowing | Implemented, off by default | step 0, 7ac59e5 |
| Rule extractor; code-checked standing statements | Implemented | `miner/extract.py` |
| User-typed verdicts | Implemented | CLI, `rkm` prompts |
| Refutations of undelivered claims | Implemented | `docs/LADDER.md` |
| Preference user-evidence rule | Implemented | `engine.py` |
| Dependency propagation via tainted sessions | Implemented | `propagate.py` |
| Relations depends_on, refines, supersedes declared by the newer record | Implemented | `RecordSpec`, `miner.py` |
| `relation_proposed` events for relations added later | Planned | described in `docs/GRAPH.md`, absent from `domain.Kind` |
| `governs` derived in code | Implemented | `relations.py` |
| Anchors (`about`, `touches`); missing-file note | Implemented | `anchors.py`, `builder.py` |
| Packet groups, ordering and budget | Implemented | `builder.py` |
| Supersession on views, reversible | Implemented | `builder.superseded` |
| Arms gated, none, ungated, stated | Implemented | `builder._select` |
| `SKILL.md` rendered from procedure and dependencies | Implemented | `skills/export.py` |
| `explain`, `graph --record` | Implemented | `service.py`, `graph.py` |
| `graph --trace`; per-session trace check | Implemented | `graph.traces`, `workload/runner.py` |
| Metrics C6 | Implemented | `rka-memory metrics` |
| `workspace_reset` events | Implemented | 272f6f9 |
| dsh trust boundary (writes and reads) | Implemented | `docker/dsh`, `docs/LADDER.md` |
| Claude Code read protection | Planned | `docs/LADDER.md` |
| MCP hints channel | Implemented; never called in 392 sessions | `mcp` |
| 60-day retirement | Implemented; never triggered live | `engine.py` |
| Falsifier field | Partially implemented: stored, no code reads it | `RecordSpec` |
| General `compose(r)` for every view | Partially implemented | Definition 8 |
| Dedicated amendment kind | Planned (proposed) | Section 3.3 |
| Route-level retraction; decision or approach node kind | Planned (proposed) | Section 3.5 |
| Stable-order packet rendering; separated volatile metadata | Planned (proposed) | Section 3.6 |
| Cache-hit token logging | Planned (proposed) | Section 3.6 |
| Delivery-independent grounding, randomized withholding, asymmetric counting, causal provenance | Planned (approved) | step 2 |
| Executable falsifiers; verified citations | Planned (approved) | step 2 |
| CL-Bench system; notepad arm in own streams | Planned (approved) | step 3 |
| Runner-made links tying outcomes to records | Planned (unscheduled research item) | `docs/LADDER.md`, known gaps |
| Mapping of the ladder onto RKA Core concepts; Core dashboard | Planned, deferred | `dec_01M6S95PGFKYZBSEF9903S3KRN` |

---

## 6. Results to date

### 6.1 Setup

All live results come from `docs/RESULTS.md` and a ledger census in the retrospective (`jrn_01M760PWVWK0XZJK1099ZA153S`): 29 live ledgers, 392 sessions, five streams, all on the `calc` fixture, with DeepSeek as agent (dsh's default route) and miner. Arms rotate by simulated day; the `mislead` and `unverifiable` streams run one ledger per arm, so no arm's sessions feed another arm's packets. The simulated user commits code each session, corrects nothing, and never confirms or rejects.

### 6.2 Streams before step 1

**calc-weeks.** Before support links existed, "run `pytest -q`" became five L1 records each grounded in one session and nothing reached L2 without a user confirm; with support links, three records reached L2 and were delivered to three later sessions with no misapplication.

**house-rules.** Once standing statements could confirm a rule (73d6772) and `AGENTS.md` was controlled (6a5cb00), the last run (`20261108T205525Z`) kept both user rules in 6/6 gated sessions and 0/6 without memory.

**drift** (two runs, six sessions per arm each; on day 10 the user moves the code without telling the agent).

| | gated | ungated | none |
|---|---|---|---|
| Summary rule kept | 12/12 | 12/12 | 0/12 |
| Cache rule kept | 12/12 | 11/12 | 0/12 |
| Suite passes | 12/12 | 12/12 | 12/12 |
| Sessions with a delivered record later demoted | 4 of 12 | 5 of 10 | |
| Mean tokens before the first write | 11,264 | 12,525 | 12,282 |

The gate passed the specification's decision point, but in both arms the wrongly delivered records were the same validated records, made wrong by the restructure; the difference is within noise.

**mislead** (three wrong claims the repository can refute, planted at L1; two runs, 30 task sessions per arm). No gated packet carried a planted record (0/30 against 30/30 ungated). DeepSeek checked the claims before acting and followed none in any arm; ungated cost a median 4% more tokens before the first write and in total. In the second ungated run the miner joined a true observation to a planted premise, and that derived record reached L2 in session 5 and stayed validated through session 15.

### 6.3 Claims the agent cannot check (unverifiable)

Three false claims about systems outside the repository (round to two decimals; raise TypeError; register every function in `OPERATIONS`). Reference implementations confirmed beforehand that the hidden user checks pass honest code and fail claim-following code. Two runs, 30 task sessions per arm.

| Sessions 2 to 16 | gated | ungated | none |
|---|---|---|---|
| Given a planted record | 0/30 | 30/30 | 0/30 |
| Raised TypeError | 0/30 | 30/30 | 0/30 |
| Rounded the result | 0/30 | 30/30 | 0/30 |
| Registered in `OPERATIONS` | 0/30 | 30/30 | 0/30 |
| Reply questioned a record | 0/30 | 0/30 | 0/30 |
| Suite passes (all 16 sessions) | 32/32 | 32/32 | 32/32 |
| Median tokens per session | 50,096 | 64,699 | 65,586 |

Every ungated session followed all three wrong claims and wrote tests asserting the wrong behaviour, so its suite passed. The specification's misapplication rate was 0 for ungated, and the contradiction measure caught 8 of 30 sessions; only the hidden checks showed the harm. Wrong claims also became validated memory by two routes: a followed claim made itself true in the code, after which the miner described the code (one record reached L2, a procedure reached L3); and the miner, which saw L1 records in every arm, wrote records on planted premises.

### 6.4 Closing the miner leak and refuting undelivered claims

Both changes were measured by chained replay (agent sessions kept as recorded, the miner re-run in order). A prompt that lists L1 records apart, with a rule against building on them, reduced records premised on a planted claim in the four ledgers where no agent saw the claims from 9 (live) and 10 (previous prompt) to 0 in both replays. Refutations, replayed once over all twelve mislead and unverifiable ledgers, gave counter-events to 12 of 12 planted claims in mislead and 8 of 12 in unverifiable; every planted record stayed at L1 and the user rule reached L2 in all twelve ledgers. One true record with 10 supporting links was refuted by a miner that had absorbed a planted claim. One replay per variant cannot separate small differences: two replays of the same commit kept 9 and 17 records at L2 or L3.

### 6.5 Ledger census

| Item | Value |
|---|---|
| Mined records | 250 |
| Reached L2 | 149 (117 mined lessons by the k = 2 threshold; 32 user rules by standing statements) |
| Ended at L3 | 27 |
| Demoted after L2 | 5 of 149 |
| Evidence links | 1,996, all drawn by the model |
| Strong events cited by threshold promotions | 182: 159 test-suite runs, 23 exit codes of git, ls, cat or realpath |
| Support links of threshold-promoted records | 1,531, of which 691 (45%) from sessions that had received the record |
| Narrowings | 0 |
| Memory-tool calls, hint deliveries, user confirms or rejects | 0 |
| Miner tokens against agent tokens | 4.9M against 24.1M (20%) |

Median tokens per session (gated, ungated, none; n = 32 per arm): mislead 50,181, 52,318, 84,524; unverifiable 49,727, 63,768, 62,242. Gated memory therefore used about 20% to 40% fewer agent tokens than no memory in these two streams, but which records produced the saving is unknown.

### 6.6 What stands

The retrospective judged four results robust: withholding unverified claims prevents harm the agent cannot detect; user-stated standing rules checked verbatim by code account for every measured behavioural gain (12/12 against 0/12 in drift, 6/6 against 0/6 in house-rules); the ledger with a deterministic projection lets prompt and engine changes be measured by chained replay without new agent runs; gated memory saves tokens, from an unknown source. It also found that the "grounded" check examines who emitted an event, not who caused it, which the 45% figure makes concrete.

### 6.7 Relations in live runs

Before the relation prompt was reworked, live miners proposed one relation across 38 records in six runs. With the kept prompt, a chained replay of those runs produced 50 records, 26 at L2 or above and 23 with relations; the live house-rules run with it ended with three correct relations among 8 records. Code-derived `governs` found 8 correct links across 11 procedures where live miners had declared 1 (`docs/GRAPH.md`).

### 6.8 Step 1 interim results

Runs A (`workload-runs/20261113T200907Z`) and B (`20261113T200927Z`) each ran six streams (mislead and unverifiable, by arms stated, gated, none); every stream passed 98/98 checks. Analysis by `scripts/stated_vs_gated.py` (3064dfd), RKA `jrn_01M7BEX65A6Y3FHDHBW9CS5BQ1`:

| Arm | Mean tokens per session | Median | n |
|---|---|---|---|
| gated | 66,067 | 56,134 | 64 |
| stated | 82,623 | 62,074 | 64 |
| none | 79,341 | 59,798 | 64 |

Gated saved 16,556 tokens per session over stated (20.0% of stated), 95% clustered bootstrap CI [-23,520, 42,440] over four stream-by-run clusters; the spread between two runs of one arm was 16,316. Per cluster (stated minus gated): mislead A +35,500, mislead B +40,161, unverifiable A -29,820, unverifiable B +20,381. Under the pre-registered order the verdict was **undecided**: the CI includes 0 and the saving exceeds 10%, so two more runs per arm are required. All secondary observations were equal between gated and stated except "reply ends with Summary" in unverifiable: gated 32/32, stated 19/32, none 2/32.

**Harness confound.** The 19/32 comes from one event in run A's unverifiable-stated stream. The agent wrote the house rule into `AGENTS.md`; the stream removed `AGENTS.md` before the next session through a `drift` step; file-change detection recorded the removal as a `user_undo` with actor user; the miner linked it as counter-evidence, and the Summary rule, at L2 since session 1, fell to L1 and stayed there (kept in 3 of 16 sessions). No other of the 12 ledgers has a `user_undo`. This is the emitter-versus-cause problem arising inside the test harness.

**Fix (commit 272f6f9, before any further run).** Streams take a `reset` key: paths the runner removes as housekeeping are logged as the runner's own `workspace_reset` event, and `detect_file_changes` skips a path reset after the agent's last write. The five live streams moved their `AGENTS.md` and `.pytest_cache` cleaning from `drift` to `reset`; other drift actions, prompts and observations are unchanged. A new scenario, `19-reset`, checks this through real dsh and fails in two places with the skip removed. 236 unit tests and 19 scenarios (636/636 checks) pass.

**Extension.** Runs C and D add two runs per arm on the fixed harness, as the extension rule requires. The analysis plan was recorded before they started: the decision uses all four runs with the unchanged criterion; two sensitivity analyses are reported and do not decide: (i) A to D without the confounded cluster (unverifiable, run A); (ii) C and D only.

### 6.9 Step 1 decision (placeholder)

> **PLACEHOLDER, to be completed when runs C and D finish.** Report: per-arm means and medians over runs A to D; gated minus stated saving with its 95% clustered bootstrap CI over eight clusters; the spread between runs of one arm; secondary observation counts; the verdict (Case A, Case B, or undecided) under the unchanged pre-registered criterion; and, as non-deciding context, sensitivity analyses (i) and (ii). Then record the verdict in RKA and state its consequence for step 2.

---

## 7. Evaluation plan

The revised plan was approved by the PI on 2026-11-13 and amended the same day: step 3 runs on DeepSeek (`jrn_01M7B9NF267T19WHBQG521P714`), and the LLM Wiki comparison's proposals were folded into steps 2 and 3. Evaluation uses task streams only; DeepSeek is agent and miner throughout. The measurements for D1 to D4 in Section 7.6 are proposed with this report and are not yet part of the approved plan.

### 7.1 Research questions

| RQ | Question | Claim | Step | Nature |
|---|---|---|---|---|
| RQ-V | Does withholding unvalidated records prevent harm the agent cannot detect, without losing user rules? | D5 | done in streams; repeated in steps 2 and 3 | Measured (Section 6.3) |
| RQ1 | Do threshold-promoted mined lessons earn a measurable benefit beyond user-affirmed rules? | D5 | 1 | Pre-registered criterion, no directional prediction |
| RQ2 | Does delivery-independent grounding lower the share of self-produced support while keeping the gate's protections? | D5 | 2 | Hypothesis |
| RQ3 | On CL-Bench Codebase Adaptation, do stated and gated memory improve step efficiency without harm, relative to no memory and to a model-curated notepad? | D5 | 3 | Hypothesis |
| RQ4 | How far does a wrong self-written note propagate in a model-curated notepad? | D3, D5 | 3 | Descriptive |
| RQ-D1 | Are composed immutable records more compact and more retentive than rewritten notes? | D1 | 3 and own streams | Hypothesis H-D1 |
| RQ-D2 | Is delivered context fully traceable, replayable, and less churning between sessions? | D2 | all | Hypothesis H-D2 |
| RQ-D3 | Does a typed graph with delivery provenance withdraw a wrong route faster and more completely? | D3 | wrong-route stream | Hypothesis H-D3 |
| RQ-D4 | Does stable-order rendering raise the cache-hit ratio? | D4 | all | Hypothesis H-D4 |

### 7.2 Step 1: stated against gated (running)

Streams `workload/live/4-mislead.yaml` and `5-unverifiable.yaml`, 16 sessions each (one session stating the standing rules, 15 task sessions); arms stated, gated and none with one ledger each; two runs per arm initially, extended to four (Section 6.8). The initial two runs were sized at 192 sessions and about 14M DeepSeek tokens.

**Pre-registered criterion** (fixed before any step-1 run, unchanged by later amendments; quoted from `jrn_01M7B7YHTPRFGN97QZ5V0K7HW1`):

> Primary measure: per-session agent tokens, compared gated vs stated as paired differences by session position within stream and run, pooled over the two streams, with a 95% bootstrap CI clustered by stream x run. Secondary: per-arm rates of the observations each stream already records (house rule kept, wrong planted claim followed, suite passed).
> - Case A (mined lessons earn nothing measurable): the CI of gated minus stated tokens includes 0, or the gated saving is smaller than the spread between the two runs of the same arm, and no observation rate differs by more than 2 sessions out of 32 in gated's favour. Then delete the threshold-promotion path for miner lessons.
> - Case B (they earn something): the CI excludes 0 with gated cheaper. Then keep the path and rebuild its grounding on delivery independence (step 2).
> - If the CI includes 0 after 2 runs but the point estimate favours gated by more than 10%, add 2 more runs per arm before deciding.

The script defines the saving as stated minus gated, uses a two-level bootstrap (clusters, then sessions; 10,000 draws; seed 0), and checks in order: extension, Case A, Case B, otherwise undecided.

### 7.3 Step 2: delivery-independent grounding (Planned, approved; scope set by step 1)

**What is built.** (1) Counting only delivery-independent evidence (Definition 7). (2) Randomised withholding of promoted records inside their scope, which yields independent evidence and an effect estimate at the cost of withholding help from some sessions. (3) Asymmetric counting: support after delivery discounted, counter-evidence after delivery counted in full, since "followed and failed" is more diagnostic than "failed while not followed". (4) Causal provenance on each evidence link: files the session changed, whether the test file or command producing the evidence was created or modified in the session, and whether the record was in context. (5) L1 claims rewritten as code-checkable predictions: (A) executable falsifiers in the runner's check language (`file_contains`, `file_absent`, `command_passes`, `ran`, `wrote` in `rka_memory/workload/checks.py`), restricted to declarative file checks and allow-listed commands and evaluated between sessions with no agent present; (B) verified citations, checked against the current file at delivery. Under Case A the threshold path is deleted and step 2 concentrates on (A), (B) and the user's words; under Case B the path is kept and rebuilt on (1) to (4).

**How it is checked.** Replay tests per rule; scripted-model scenarios for each mechanism; a recomputed census over the 29 ledgers reporting the share of self-produced support (baseline 45%), how many of the 117 threshold-promoted lessons still reach L2, and whether planted records stay at L1 and user rules reach L2 in all 12 mislead and unverifiable ledgers; safety tests that predicates outside the restricted language are never run unattended; new live runs of step 1's arms.

### 7.4 Step 3: CL-Bench Codebase Adaptation on DeepSeek (Planned, approved)

CL-Bench routes model strings that are not Claude-, Gemini- or GPT-style through LiteLLM, which reaches DeepSeek. Step 3 writes an rka-memory system for CL-Bench: each command, its output and the hidden tests' verdict become ledger events; each arm builds its own packet; the miner and gate run after each problem. Hidden regression tests are evidence the agent cannot edit, and the tablib-to-tenacity switch gives the first two-project stream. The patch under test is still influenced by any delivered record, so withholding and asymmetric counting remain necessary.

**Arms**: none, stated, gated, and a model-curated memory baseline in the LLM Wiki and auto-memory pattern, implemented with CL-Bench's own `icl_notepad` on DeepSeek. Five rollouts per arm with 95% CIs; memory-side tokens (miner and gate) counted as cost; gain and normalised gain reported as CL-Bench defines them. Anthropic's CL-Bench numbers come from a variant and are not compared directly. A small pilot measures the cost of one problem before the full run, and a power analysis precedes it: VibeMemBench's null result for deepseek-v4-pro and the large gains of ACE and FGSE on tasks where the baseline often fails jointly indicate that gains appear only when tasks require memory.

**Own streams gain** hidden convention checks that fail unless an earlier lesson is applied, drift, filtering to sessions where no memory fails (as in VibeMemBench's SIEVE), irrelevant-memory and wrong-memory controls, and the notepad arm in the mislead and unverifiable streams, to measure directly how far a wrong self-written note propagates.

### 7.5 Hypotheses

H2: under delivery-independent counting, the share of self-produced support among threshold-promoted records falls well below 45%, and planted records still stay at L1 in all 12 ledgers. H3: on Codebase Adaptation, gated and stated gains are not below none; given VibeMemBench, the main questions are absence of harm and fewer steps rather than success rate. H4: in streams with planted wrong claims, the notepad arm follows them in a larger share of sessions than gated and stated. H-D1 to H-D4 are stated in Section 3. None of these is pre-registered yet; each will be fixed, with its decision rule, before its runs start, as step 1's was.

### 7.6 Measurements for the comparison claims (proposed)

| Claim | Metric | Definition | Arms and source |
|---|---|---|---|
| D1 | Tokens per delivered item | Injected memory tokens in a session divided by the number of distinct items (records, or notepad bullets or lines) | gated, stated, notepad; packet events and notepad snapshots |
| D1 | Tokens per session context | Injected memory tokens at session start | same |
| D1 | Bytes written per update | Ledger bytes appended per mining pass, against bytes written by the notepad's file tools per update | ledger; notepad tool calls |
| D1 | Fact retention | Share of K planted, graded facts still present (string match, with a judge only for paraphrase) and still acted on after n updates | own streams with a fact list, after Anthropic's contract protocol |
| D2 | Traceability rate | Share of delivered items with counted grounding evidence linked before delivery (`graph --trace`); for the notepad, share of injected sentences that can be matched to a transcript event | all arms |
| D2 | Replay determinism | Share of repeated replays of a ledger that reproduce every rung change and delivered set exactly | ledgers of all runs |
| D2 | Context churn | Normalised edit distance between consecutive sessions' injected memory text | all memory arms |
| D3 | Sessions to withdrawal | Sessions after the root's refutation until no closure member is delivered | wrong-route stream |
| D3 | Residual influence | Share of later sessions whose behaviour follows the wrong route, by hidden checks | wrong-route stream |
| D4 | Cache-hit ratio | prompt_cache_hit_tokens / (hit + miss) per session and on each session's first request; input cost per session | stable order, recency order, notepad |

**Wrong-route stream design.** The runner plants a root decision that is wrong in a way only a hidden check reveals, with dependent records that declare `depends_on` or `refines` on it and procedures that run commands it names. Several sessions deliver the root and its dependents, so later lessons are learned while it is in context; the user then refutes the root with a typed rejection. Arms: route retraction (Definition 9), current propagation only, and the notepad baseline, in which the correction arrives as a user message and the model must update its notes. Secondary measures: true records withheld by over-pruning, and the evidence cost of re-admission.

**Cache measurement.** Log `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens` from every DeepSeek response (Planned (proposed)), then compare stable-order and recency-order packets on the same stream, interleaved in time so that provider cache lifetime affects both arms alike.

### 7.7 Statistics

Sessions are paired by position within stream and run; bootstrap CIs (10,000 draws) are clustered by stream and run; the spread between runs of one arm is reported next to every effect and enters step 1's criterion directly. The survey recommends at least four to five seeds and several stream orders (Miller); step 1 has two to four runs per arm, a known limitation, and step 3 five rollouts. Single chained replays are either repeated or reported as direction only. Following Self-Cleaning and Captured Anyway, per-record rung trajectories are inspected rather than ledger-wide averages alone.

### 7.8 Cost

All experiments use DeepSeek with the key the PI provided; no Anthropic or OpenAI key is needed, and the `claude_cli` and `codex_cli` miner backends are not used in experiments. Known figures: step 1's first two runs were sized at about 14M DeepSeek tokens for 192 sessions; the 392 earlier sessions used 24.1M agent tokens and 4.9M miner tokens; a chained replay of four ledgers cost 0.73M to 1.10M miner tokens. Step 3 is 19 problems by 4 arms by 5 rollouts, 380 problem-level runs plus mining; no token estimate exists, so the pilot measures it. At step 1's rate, a 16-session wrong-route stream with four arms and two runs (128 sessions) would cost on the order of 9M tokens (an extrapolation, not a measurement). The cache and churn measurements reuse existing runs once logging exists.

### 7.9 Threats to validity

1. **One model family.** DeepSeek is agent and miner; a more trusting agent may behave differently. A second agent model is recommended but not scheduled.
2. **One fixture family.** Near-identical tasks in one repository are the best case for accumulated memory; CL-Bench partly addresses this.
3. **Simulated user.** It never confirms or rejects, so user-channel distinctions (typed, pasted, relayed) are untested.
4. **Small effects in the literature** and a **ceiling**: every live stream passed its suite in every session; hidden checks are meant to break this.
5. **Few runs**: two to four runs per condition, one replay per variant.
6. **Separate ledgers per arm**, whereas a deployment has one ledger.
7. **Reliance on prompt behaviour**: planted records stay at L1 partly because the miner does not link them.
8. **Harness artefacts**: the step-1 confound shows the harness itself can emit misattributed events; the `reset` fix covers the known case only.
9. **Comparability**: Anthropic's CL-Bench numbers come from a variant; step 3 compares arms within one framework.

### 7.10 Milestones

| Milestone | Content | Gate | Status |
|---|---|---|---|
| M0 | Step 0: freeze scope, narrowing off | Unit tests pass | Done (7ac59e5) |
| M1 | Step 1: stated, gated, none | Verdict under the pre-registered criterion | Running (runs C, D) |
| M2 | Record the main-line refinement as a superseding decision in RKA; record the step-1 verdict | M1 | To do |
| M3 | Step 2 | Replay tests, scenarios, census | Planned (approved) |
| M4 | Step 3: CL-Bench system, pilot, power analysis, full runs | M3; pilot cost known | Planned (approved) |
| M5 | Own-stream extensions; notepad arm | Parallel with M4 | Planned (approved) |
| M6 | D1 to D4 instrumentation and wrong-route stream | Pre-registered rules for H-D1 to H-D4 | Planned (proposed) |
| M7 | Paper from the logs | M4, M5 results | Not started |

---

## 8. Expected contributions and publication path

### 8.1 Expected contributions

1. **An enforceable error-independence invariant**: counting only delivery-independent evidence, discounting post-delivery support and counting post-delivery counter-evidence in full. No published work found does this in code.
2. **A measurement of the harm that withholding prevents**, with evidence that a system's internal metrics do not see it (30/30 against 0/30, misapplication rate 0).
3. **A direct comparison of user-stated and model-inferred memory** (step 1), which the literature lacks; either case is informative.
4. **An append-only, typed memory graph with derived views**, and measurements of its compactness, retention, traceability, churn, pruning and cache behaviour against a model-curated notepad (D1 to D4).
5. **An open-source prototype** with a deterministic, replayable ledger and chained replay, which decouples gate measurement from agent runs.
6. **Evaluation designs**: coding streams with hidden conventions, drift, planted wrong claims, a wrong-route stream and a notepad control.

### 8.2 Publication path

The first survey (`jrn_01M6S93VF2V3WGDX50C6ZPJ6QT`) listed three undecided framings: memory admission control as an integrity and validity policy (security venues); valid = grounding times applicability, evaluating self-improving agents without labels (ACL or NeurIPS); and silent acceptance is not consent, implicit feedback as evidence (CHI or UIST). This report suggests leading with the second, centred on self-fulfilling evidence and delivery-independent grounding, because the 45% finding and the Echo Gap theory give it the clearest motivation and steps 2 and 3 supply the experiments. The comparison with Anthropic's design (D1 to D4) fits that paper as the system design section, with its measurements as secondary results; if those results are clear on their own, a shorter systems or workshop paper on append-only typed memory against rewritten notes is a possible second output. The first framing's material exists (trust boundary, forgery and read checks), but the PI has said this is not a security project. The third lacks data without real users.

---

## 9. Limitations and open questions

### 9.1 Limitations

1. **Step 1 may return Case A**, removing the threshold path for mined lessons and leaving self-improvement to user rules and code-checkable predictions. The main line survives; the narrative shifts.
2. **Evaluation risk exceeds mechanism risk.** Positive coding gains in the literature are small and noisy; the most persuasive evidence for the gate is harm prevented plus behaviour change on tasks that require memory.
3. **Strict grounding withholds true knowledge** that has no non-agent evidence.
4. **Emitter is not cause.** The agent writes the tests, any command's exit code counts as strong evidence, and within-session laundering is not closed; runner-made links are not scheduled.
5. **Miner refutations can block true records** (one case in twelve ledgers).
6. **The comparison claims D1 to D4 are untested.** The mechanisms behind D1 to D3 exist in part; D4's mechanism does not exist yet, and its upper bound in dsh's layout is small.
7. **"Built on RKA Core" is nominal.** The package calls only `/api/projects` and `/api/notes`; the ladder-to-Core mapping (`dec_01M6S95PGFKYZBSEF9903S3KRN`) is not implemented, and about 9.8k raw events from the former host sync remain in the project's journal by the PI's decision.

### 9.2 Open questions

1. Can cross-record synthesis, the source of Anthropic's wiki gains, be added without admitting self-generated text, for example as extractive composed views that cite node ids?
2. Should a route-level retraction include records governed by the root, or only declared dependents and delivery-tainted records?
3. How should composed views and stable-order packets coexist with anchor-first ordering, which deliberately moves relevant records forward?
4. Is scope narrowing worth restoring once CL-Bench's repository switch provides cross-project counter-evidence?
5. Do weak signals (edits, undos, repeats) predict validity 30 days later? Follow-up work to PRELUDE/CIPHER (NeurIPS 2024 [peer-reviewed], as recorded in the first survey) found correlations below 0.5 between edit distance and preference accuracy.
6. How should wrong L1 records that are never refuted be cleared, given that 60-day retirement has never fired live?
7. Should a record about a file the project no longer has be demoted rather than flagged? The drift runs suggest the flag suffices to stop the agent from following it.

---

## References

Tags as defined under Conventions. arXiv identifiers begin with year and month (2608 is August 2026).

**Anchor and Anthropic material**

- L. Martin. The components of agent memory. Anthropic, 2026-11-06 [vendor]. https://www.anthropic.com/research/components-of-agent-memory
- Anthropic. Dreams (Claude Managed Agents), research preview [vendor]. https://platform.claude.com/docs/en/managed-agents/dreams
- Anthropic. Dreaming with a dynamic workflow (cookbook), 2026-11-02 [vendor]. https://platform.claude.com/cookbook/managed-agents-cma-dreaming-with-a-dynamic-workflow
- Anthropic. Managed Agents memory stores [vendor]. https://platform.claude.com/docs/en/managed-agents/memory
- Anthropic. Claude Code memory [vendor]. https://code.claude.com/docs/en/memory
- Anthropic. Memory tool [vendor]. https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
- Anthropic. Knowledge-wiki quickstart [vendor]. https://github.com/anthropics/claude-quickstarts/tree/main/managed-agents/knowledge-wiki

**Other vendor and provider material**

- DeepSeek. Context Caching (API guide) [vendor]. https://api-docs.deepseek.com/guides/kv_cache
- GitHub. Building an agentic memory system for GitHub Copilot, 2026-01-15, updated 2026-11-12 [vendor]. https://github.blog/ai-and-ml/github-copilot/building-an-agentic-memory-system-for-github-copilot/
- GitHub Docs. Copilot Memory [vendor]. https://docs.github.com/en/copilot/concepts/agents/copilot-memory
- OpenAI. Codex Memories [vendor]. https://learn.chatgpt.com/docs/customization/memories?surface=app
- Letta. Sleep-time / Dreaming [vendor]. https://docs.letta.com/guides/agents/architectures/sleeptime
- Zep. Graphiti [vendor]. https://github.com/getzep/graphiti ; Zep paper, arXiv 2501.13956 [preprint, vendor-run]
- LangChain. LangMem conceptual guide [vendor]. https://langchain-ai.github.io/langmem/concepts/conceptual_guide/
- Cursor forum. Are my memories gone?, 2025-11-25 [vendor]. https://forum.cursor.com/t/are-my-memories-gone/144057
- Microsoft Defender Security Research. AI Recommendation Poisoning, 2026-02-10 [vendor]. https://www.microsoft.com/en-us/security/blog/2026/02/10/ai-recommendation-poisoning/

**Practitioner**

- A. Karpathy. LLM Wiki, 2026-04-04 [practitioner]. https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- A. Jones. Karpathy's LLM Wiki as Agent Memory, 2026-06-08 [practitioner]. https://aaif.io/blog/karpathys-llm-wiki-as-agent-memory
- D. Miessler. LifeOS (formerly PAI), v8.761.0 [practitioner]. https://github.com/danielmiessler/LifeOS

**Peer-reviewed**

- ChatGPT memory audit, WWW 2026. https://arxiv.org/abs/2602.01450
- Agent Workflow Memory, ICML 2025. https://proceedings.mlr.press/v267/wang25bx.html
- ReasoningBank, ICLR 2026. https://arxiv.org/html/2509.25140
- ACE, ICLR 2026. https://arxiv.org/html/2510.04618
- Darwin Gödel Machine, ICLR 2026. https://arxiv.org/html/2505.22954
- Self-Generated In-Context Examples, NeurIPS 2025. https://proceedings.nips.cc/paper_files/paper/2025/hash/5d1f02132ef51602adf07000ca5b6138-Abstract-Conference.html
- MINJA, NeurIPS 2025. https://arxiv.org/abs/2503.03704
- Xiong et al., experience following, ACL 2026. https://aclanthology.org/2026.acl-long.27/
- ImpossibleBench, ICLR 2026. https://arxiv.org/abs/2510.20270
- PatchDiff, ICSE 2026. https://arxiv.org/abs/2503.15223
- PrefEval, ICLR 2025 (oral). https://arxiv.org/abs/2502.09597
- MemoryBench, ICML 2026, PMLR v306 (another note records it as preprint arXiv 2510.17281; unverified). https://proceedings.mlr.press/v306/ai26e.html
- SkillsBench (reported as NeurIPS 2026 poster; numbers from search snippets; unverified verbatim). https://arxiv.org/html/2602.12670v1
- Honest Lying [workshop, ICML 2026]. https://arxiv.org/abs/2605.29463
- Evaluating AGENTS.md (listed on an ICLR 2026 page; type unverified). https://arxiv.org/abs/2602.11988

**Preprints**

- CL-Bench (Asawa et al., 2026-06-04). https://arxiv.org/abs/2606.05661 ; framework https://github.com/pgasawa/continual-learning-bench
- VibeMemBench. https://arxiv.org/abs/2609.23570
- MemGuard. https://arxiv.org/html/2608.21867
- Verification as an Architectural Layer [workshop]. https://arxiv.org/html/2609.31937v2
- FGSE. https://arxiv.org/html/2611.14326
- TEPA. https://arxiv.org/abs/2608.07429
- StateAuditor. https://arxiv.org/abs/2608.01619
- Proof-of-Execution Memory. https://arxiv.org/abs/2608.16032
- Memory Reward Inflation in Self-Improving LLM Agents. https://arxiv.org/abs/2608.00017
- MPBench. https://arxiv.org/html/2606.04329v2
- PMPA. https://arxiv.org/abs/2609.13889
- Self-Cleaning and Captured Anyway. https://arxiv.org/abs/2609.25052
- Manufactured Confidence. https://arxiv.org/abs/2606.29279
- Utility Under Attack. https://arxiv.org/abs/2608.21230
- mem0 (2025-04). https://arxiv.org/abs/2504.19413
- Evo-Memory. https://arxiv.org/html/2511.20857v2
- MemoryAgentBench. https://arxiv.org/html/2507.05257v1
- LifelongAgentBench. https://arxiv.org/pdf/2505.11942
- STALE. https://arxiv.org/abs/2605.06527
- MemSecBench (venue unconfirmed). https://www.alphaxiv.org/abs/2607.27080
- Miller, statistical methods for evaluations (status unverified). https://arxiv.org/pdf/2411.00640

**Early work**

- ExpeL (often cited as AAAI 2024; unverified). https://arxiv.org/html/2308.10144v3
- Voyager (2023; venue unverified). https://arxiv.org/abs/2305.16291

**Internal sources**

- Repository: `docs/GRAPH.md`, `docs/LADDER.md`, `docs/LEDGER.md`, `docs/RESULTS.md`, `docs/SPEC.md`, `docs/RESEARCH_REPORT.zh.md`, `rka_memory/default.yaml`, `rka_memory/domain.py`, `rka_memory/packet/builder.py`, `rka_memory/validity/engine.py`, `rka_memory/validity/propagate.py`, `rka_memory/relations.py`, `rka_memory/graph.py`, `rka_memory/service.py`, `rka_memory/skills/export.py`, `rka_memory/workload/runner.py`, `plugins/dsh/src/index.ts`, `plugins/dsh/README.md`, `scripts/stated_vs_gated.py`.
- Survey notes: `research_notes/智能体记忆 验证门控 相关工作/` (five notes, notably `production_systems_and_anchor.md`).
- RKA project `prj_01M6S9020MSHWB0HX2Z9C5SK5W`: PI goal `jrn_01M6S927YWJZA9EGRCAP0B9DGJ`; project lineage `jrn_01M6S935R57CPF0Y9T7NQEM6P6`; first survey `jrn_01M6S93VF2V3WGDX50C6ZPJ6QT`; retrospective `jrn_01M760PWVWK0XZJK1099ZA153S`; revised plan and pre-registered criterion `jrn_01M7B7YHTPRFGN97QZ5V0K7HW1`; DeepSeek instruction `jrn_01M7B9NF267T19WHBQG521P714`; LLM Wiki comparison `jrn_01M7BCEMEVH2VJ28A4JW9728NK`; host clarification `jrn_01M7B46HF6P45SC73SEF2AXGSN`; host uninstall `jrn_01M7B4C1P6SD1RV8P6DZCBY4ZB`; LifeOS note `jrn_01M7B47643N7KMC8XVZ0JST5N0`; step-1 interim `jrn_01M7BEX65A6Y3FHDHBW9CS5BQ1`; anchor literature entry `lit_01M7B6C46EXCZDK3FNJEWHW423`; decisions `dec_01M6S956E1X6KYYMMK3E7795FM`, `dec_01M6S95E394B7M9VHDV3113MJ6`, `dec_01M6S95PGFKYZBSEF9903S3KRN`, `dec_01M6SBEM7JBD98MDEKWP53B1C5`.
- Commits: 7ac59e5 (step 0), c0879f9 (stated arm), 3064dfd (analysis script), a4b0128 (census script), 73d6772 and 6a5cb00 (house-rules fixes), 272f6f9 (workspace_reset).

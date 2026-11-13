# Outcome-gated memory and skill libraries for LLM agents (cross-episode learning, verified promotion, skill retirement)

Scope note for the report writer. "Earlier" marks foundations published before mid-2025. Venue status was checked on the arXiv page, the proceedings page, or a search result. Where a venue is only reported by a secondary source, it says so. Unless marked otherwise, numbers come from the fetched arXiv HTML or abstract pages. Several 2026 preprints were posted after the researcher's training cutoff and are known only from their arXiv pages, so treat them as unreviewed.

Comparison target (the user's design): memory records move through observation → claim → validated claim → skill → retired. A record is promoted only by code-checked evidence the agent did not author (test results, exit codes, tool output, explicit user statements), never by model judgment, and a skill stays only while its acceptance check passes.

---

## Q1. Which methods gate memory writes and retention on an external signal, and which on the model's own judgment?

### Takeaway
Most widely cited cross-episode memory methods (AWM online, ReasoningBank, Dynamic Cheatsheet, ASI, SkillWeaver, Voyager) decide what to keep with an LLM judge, often the agent's own backbone. Methods that use a signal the agent did not author fall into three groups: benchmark-test acceptance in self-modifying coding agents (DGM, HGM, SICA), formal verification (LEGO-Prover, earlier), and benchmark rewards or gold labels available only in research settings (ExpeL trajectory success, MemP "Validation", ACE with GT labels, SAGE's RL reward). Work from 2026 is moving toward explicit verifier gates (MemGuard, FGSE, HDSO, EDV, a "verification as architectural layer" pilot), but most of these still use an LLM verifier over runtime evidence, not purely deterministic checks.

### Cited Findings
**Gated by the model's own judgment (no ground truth at write time)**
- AWM, online setting: after each test task, an LM-based evaluator (from Pan et al., 2024) outputs a binary label, and only trajectories it labels successful are turned into workflows. The intro describes trajectories "judged correct by an evaluator module." Venue: ICML 2025 (PMLR 267). — [AWM arXiv HTML](https://arxiv.org/html/2409.07429); [PMLR](https://proceedings.mlr.press/v267/wang25bx.html)
- ReasoningBank: success/failure labels are proxy signals from an LLM judge "without any ground-truth reference." The judge is the same backbone as the agent, at temperature 0.0. On WebArena-Shopping, Gemini-2.5-flash as judge agrees with ground truth 72.7% of the time. Venue: ICLR 2026. — [ReasoningBank arXiv HTML](https://arxiv.org/html/2509.25140); [ICLR 2026 proceedings](https://proceedings.iclr.cc/paper_files/paper/2026/hash/980ea04d23d1f6908964eba2a74afe45-Abstract-Conference.html)
- Dynamic Cheatsheet: the memory is self-curated "without needing explicit ground-truth labels or human feedback." arXiv-only (Apr 2025), with no venue listed. — [DC arXiv abstract](https://arxiv.org/abs/2504.07952)
- ASI (Agent Skill Induction): a skill is added only if (1) an LLM evaluator judges the re-executed task solved, (2) at least one new skill is called, and (3) every skill-calling action changes the environment. Check (3) is an environment-state check, but correctness is judged by the LLM. Venue: COLM 2025 per the OSU paper list (the arXiv page lists none). — [ASI arXiv HTML](https://arxiv.org/html/2504.06821); [OSU GUI-agents list](https://osu-nlp-group.github.io/GUI-Agents-Paper-List/papers/inducing-programmatic-skills-for-agentic-tasks)
- SkillWeaver: during practice, an LLM "reward model" judges success. It sees the task, the trajectory with screenshots, and environment feedback such as code execution outcomes. Honing runs each API as a unit test, but for parameterized APIs the LLM generates the test parameter values, so the agent authors its own tests. arXiv (Apr 2025); venue unverified. — [SkillWeaver arXiv HTML](https://arxiv.org/html/2504.07079)
- Voyager (earlier, 2023): iterative prompting uses "environment feedback, execution errors, and self-verification" to improve programs, and skills go into an executable-code library. The fetched abstract does not state the exact add-condition. The researcher recalls, without verifying it here, that a skill is added after the GPT-4 self-verification module confirms success. — [Voyager arXiv](https://arxiv.org/abs/2305.16291)
- Agent KB: the abstract describes no success or verification filter on collected trajectories, only a "disagreement gate" at retrieval time. arXiv-only. — [Agent KB arXiv](https://arxiv.org/abs/2507.06229)
- CER: the abstract describes no success filter on what enters the experience buffer. Venue: ACL 2025. — [CER arXiv](https://arxiv.org/abs/2506.06698)

**Gated by an external or benchmark signal the agent did not author (usually available only in research settings)**
- DGM: a self-modified agent enters the archive if it compiles and can still edit code. How far it is evaluated depends on staged third-party benchmark runs: 10 tasks, then, on SWE-bench, above 40% on a 60-task subset and top-two in the archive leads to 200 tasks. Venue: ICLR 2026, per the arXiv v3 label reported in search. — [DGM arXiv HTML](https://arxiv.org/html/2505.22954); [arXiv v3 PDF](https://arxiv.org/pdf/2505.22954v3)
- LEGO-Prover (earlier, 2023): "a growing skill library containing verified lemmas." Formal verification gates library entries. — [LEGO-Prover arXiv](https://arxiv.org/abs/2310.00656)
- ExpeL (earlier; AAAI 2024 per common citation, but the fetched arXiv page lists no venue): a trial counts as a success when environment reward equals 1. But all trajectories, successes and failures, enter the pool, and insight curation (ADD, EDIT, UPVOTE, DOWNVOTE) is done by the LLM. — [ExpeL arXiv HTML](https://arxiv.org/html/2308.10144v3)
- MemP: the "Validation" update keeps only successful trajectories. Success is the environment reward in ALFWorld, and comparison against a gold standard in TravelPlanner ("No LLM-as-judge is used"). arXiv-only. — [MemP arXiv HTML](https://arxiv.org/html/2508.06433)
- ACE: updates are driven by execution feedback and environment signals, with or without ground-truth labels. Without labels, the authors warn that ACE and DC "may degrade" because the context becomes polluted. Venue: ICLR 2026. — [ACE arXiv HTML](https://arxiv.org/html/2510.04618); [ICLR 2026 virtual page](https://iclr.cc/virtual/2026/10012507)
- SAGE: a skill is saved if it "can be executed without error," an exit-code-style check, not a correctness check. AppWorld's test programs supply the RL outcome reward, and the skill bonus is paid only when both the generating task and the reusing task succeed. Venue: ACL 2026 main. — [SAGE arXiv HTML](https://arxiv.org/html/2512.17102v2); [ACL Anthology preview](https://preview.aclanthology.org/ingest-acl/2026.acl-long.69/)

**2026 verifier-gated memory (all arXiv-only)**
- MemGuard (Aug 2026): an LLM verifier scores each trajectory on task completion, evidence consistency, execution validity, and generalizability. It receives runtime status (exit codes, public test output, tool errors, visible web confirmations) and "never uses hidden benchmark labels." A record becomes active only if reward ≥ 0.70 and confidence ≥ 0.60. — [MemGuard arXiv HTML](https://arxiv.org/html/2608.21867)
- Verification as an Architectural Layer (Oct 2026, a workshop pilot submitted to JAWs): "memory is written only by verification outcomes." Each verifier is a deterministic gate followed by an optional LLM judge. The pilot does not implement the memory design. — [VAL arXiv HTML](https://arxiv.org/html/2609.31937v2)
- EDV (Execute-Distill-Verify, Jun 2026; title revised in v2 to "Beyond Self-Reflection…"): heterogeneous agents execute each task, a third-party distiller drafts lessons, and only "unanimously approved lessons" are retained. — [EDV arXiv](https://arxiv.org/abs/2606.24428)
- FGSE (Nov 2026): lessons become hypotheses that must pass probes satisfying "executable or externally checkable signals" before commitment. — [FGSE arXiv HTML](https://arxiv.org/html/2611.14326)

### Inferences
- In the research literature, "external signal" almost always means a hidden benchmark checker (env reward = 1, SWE-bench tests, AppWorld test programs, gold labels). Deployment-time versions of the same methods (AWM online, ReasoningBank, MemGuard) swap in an LLM judge because hidden tests are not available. The user's design (only evidence the agent did not author, such as user statements, exit codes, and tool output) is stricter than all of these. MemGuard's evidence inputs come closest, but its gate is still an LLM score.
- Judge reliability puts a ceiling on self-judged memory: 72.7% agreement for ReasoningBank's judge, 83–86% manual-audit agreement for MemGuard's verifier (see Q2).

### Gaps
- I found no published method whose promotion gate is purely deterministic and agent-independent (no LLM judge anywhere in the write path), apart from formal-proof libraries (LEGO-Prover) and DGM-style benchmark acceptance.
- Voyager's exact add-rule and its venue (TMLR?) were not verified from a fetched page.
- SE-Agent and SWE-Exp abstracts do not say how trajectories or experiences are judged at write time. Full texts were not read.

---

## Q2. Method-by-method: gate signal, whether the agent can produce it, retrieval and scope, removal or demotion, measured results

### Takeaway
Removal is rare. ExpeL (vote count reaches zero), ACE (embedding de-duplication plus helpful/harmful counters), FGSE (post-commit violation score), MemGuard (archival and merge), Library Drift (contribution-based retirement), and SkillOps (utility-based retirement) are the main exceptions. AWM, ReasoningBank, Memento, ASI, SAGE, and DGM are append-only. Typical gains over no memory are +4 to +17 points of success rate, with fewer steps. Self-improving coding agents report the largest jumps (DGM 20.0% → 50.0% on SWE-bench) but do not store "memory" in the record sense.

### Cited Findings

**ExpeL** (earlier; AAAI 2024 per common citation, not confirmed on the fetched page)
- Gathering: Reflexion-style retries, at most 3 reflections per task. Success means reward = 1. All trajectories go into the pool, starting from the few-shot examples. — [ExpeL](https://arxiv.org/html/2308.10144v3)
- Extraction: the LLM compares failure/success pairs for the same task, and lists of successes in chunks (L = 8 for HotpotQA and ALFWorld, 4 for WebShop). Operations are ADD, EDIT, UPVOTE, DOWNVOTE. "A newly added insight will have an initial importance count of two." UPVOTE and EDIT increment it, DOWNVOTE decrements it, and "If an insight's importance count reaches zero, it will be removed." — [ExpeL](https://arxiv.org/html/2308.10144v3)
- Retrieval: Faiss kNN over task embeddings (all-mpnet-base-v2) fetches the top-k successful trajectories (k = 6 HotpotQA, 2 WebShop, 2 ALFWorld, 3 FEVER). The full insight list is concatenated into the prompt. — [ExpeL](https://arxiv.org/html/2308.10144v3)
- Results: HotpotQA ReAct 28.0 → ExpeL 39.0. ALFWorld ReAct 40.0 → 59.0 (Reflexion R3 54). WebShop score ReAct 0.666 → 0.701. FEVER transfer ReAct 63 → 70. The text and tables disagree slightly on HotpotQA (40 vs 39). — [ExpeL](https://arxiv.org/html/2308.10144v3)

**Agent Workflow Memory (AWM)** (ICML 2025, peer-reviewed)
- Gate: offline, workflows come from training examples. Online, only trajectories an LM evaluator labels successful are used. The agent's own LLM stack produces the signal. — [AWM](https://arxiv.org/html/2409.07429)
- Scope and retrieval: scoped per website. All induced workflows for that site are added to memory, with no per-query retrieval in the main method. — [AWM](https://arxiv.org/html/2409.07429)
- Removal: none. Memory is only augmented. — [AWM](https://arxiv.org/html/2409.07429)
- Results: WebArena (GPT-4) 35.5 vs BrowserGym 23.5 (+12.0 absolute, 51.1% relative), 5.9 steps vs 7.9. Mind2Web cross-task step SR 45.1 vs MindAct 36.2 (24.6% relative). Online cross-domain 35.5 vs 18.6. — [AWM](https://arxiv.org/html/2409.07429)

**ReasoningBank** (ICLR 2026, peer-reviewed)
- Gate: an LLM judge of the same backbone, with no ground truth (72.7% agreement on WebArena-Shopping). In a simulated-noise study, performance was similar across judge accuracies of about 70–90%, and 100% was best. — [ReasoningBank](https://arxiv.org/html/2509.25140)
- Failures are distilled as "counterfactual signals and pitfalls." Adding failures helps ReasoningBank (46.5 → 49.7) but hurts AWM (44.4 → 42.2). — [ReasoningBank](https://arxiv.org/html/2509.25140)
- Item structure: title, one-sentence description, content. Up to 3 items per trajectory. Retrieval: gemini-embedding-001 cosine, default top-k = 1. Consolidation is "a simple addition operation," with no pruning or deduplication. — [ReasoningBank](https://arxiv.org/html/2509.25140)
- Results, WebArena SR / steps: Gemini-2.5-flash 40.5/9.7 (no memory) → 48.8/8.3 (with MaTTS 51.8/7.9), AWM 44.1. Gemini-2.5-pro 46.7 → 53.9. Claude-3.7 41.7 → 46.3. SWE-Bench-Verified: flash 34.2 → 38.8, pro 54.0 → 57.4. — [ReasoningBank](https://arxiv.org/html/2509.25140)

**Dynamic Cheatsheet (DC)** (arXiv-only, Apr 2025)
- Gate: self-curated, with no labels. Stores "concise, transferable snippets." — [DC](https://arxiv.org/abs/2504.07952)
- Results: GPT-4o Game of 24 "from 10% to 99%". Claude 3.5 Sonnet AIME accuracy "more than doubled". Equation balancing near-perfect vs about 50% baseline. GPQA-Diamond "9% improvement". MMLU-Pro "8% boost". The abstract does not say whether these are absolute or relative. — [DC](https://arxiv.org/abs/2504.07952)

**ACE: Agentic Context Engineering** (ICLR 2026, peer-reviewed)
- Roles: a Generator produces trajectories, a Reflector extracts lessons, and a Curator emits compact delta entries "merged into the context by non-LLM logic." — [ACE](https://arxiv.org/html/2510.04618)
- Bullets carry an ID plus counters of how often they were marked helpful or harmful. Grow-and-refine appends new bullets, updates existing ones in place, and de-duplicates by semantic embedding, either after each delta or lazily when the context window is exceeded. — [ACE](https://arxiv.org/html/2510.04618)
- Context collapse: in a case study of DC on AppWorld, the context had 18,282 tokens and 66.7 accuracy at step 60, then collapsed to 122 tokens at the next step, with accuracy falling to 57.1, below the no-adaptation baseline of 63.7. — [ACE](https://arxiv.org/html/2510.04618)
- Results (DeepSeek-V3.1): AppWorld offline 59.4 (+17.0) vs ICL 46.0 and GEPA 46.4. Online 59.5 (+17.1) vs DC 51.9. Finance offline average 81.9 (+12.8) vs GEPA 72.5. This matches the top-ranked IBM CUGA (60.3, GPT-4.1-based) on the AppWorld leaderboard. — [ACE](https://arxiv.org/html/2510.04618)
- Without GT labels: AppWorld online 59.5 (+17.1). But finance online FiNER drops to 67.3 against a 70.7 base (−3.4), and DC without GT falls to 65.4 (−3.7). — [ACE](https://arxiv.org/html/2510.04618)
- Cost: AppWorld offline latency vs GEPA −82.3% (53,898 s → 9,517 s), rollouts −75.1%. FiNER online vs DC: latency −91.5%, token cost $17.7 → $2.9 (−83.6%). — [ACE](https://arxiv.org/html/2510.04618)

**Memento** (arXiv-only, Aug 2025)
- Memory-based MDP: cases are c = (state, plan, reward), with binary reward in deep research. Both successes and failures are stored. The fetched text does not say whether the reward comes from ground truth or a judge (GPT-4o-mini is named as the answer evaluator for scoring). — [Memento](https://arxiv.org/html/2508.16153)
- Retrieval: non-parametric top-K by SimCSE cosine, or parametric top-K by a Q-function (a 2-layer MLP trained with BCE on the binary reward). K = 4 is best. No size limit or eviction. — [Memento](https://arxiv.org/html/2508.16153)
- Results: GAIA validation 87.88% Pass@3, test 79.40%. DeepResearcher 66.6 F1 / 80.4 PM vs 51.8/60.5. Case-based memory adds +6.7 F1 / +8.2 PM, and 4.7–9.6 points absolute out of distribution. — [Memento](https://arxiv.org/html/2508.16153)

**SkillWeaver** (arXiv, Apr 2025)
- Loop: propose skills, practice them, have an LLM reward model judge success, convert successes to a Python API, then hone by executing it as a unit test with LLM-generated parameters. APIs are revised (debugged) but never retired. Selection filters out APIs whose preconditions fail. — [SkillWeaver](https://arxiv.org/html/2504.07079)
- Results: WebArena (GPT-4o) 22.6 → 29.8 (31.8% relative). Online-Mind2Web 40.2 → 56.2 (39.8%). GPT-4o-mini using GPT-4o-made APIs 9.2 → 14.1 (the headline "up to 54.3%" does not appear in Table 1). — [SkillWeaver](https://arxiv.org/html/2504.07079)

**ASI: Inducing Programmatic Skills** (COLM 2025 per secondary listing)
- Gate as in Q1: LLM correctness, plus skill used, plus every skill call changes the environment, all checked by re-executing the task with the skill prefix. No removal. Updates are allowed ("+update"). — [ASI](https://arxiv.org/html/2504.06821)
- Results (WebArena, Claude): vanilla 32.7 SR / 5.6 steps, AWM 36.3 / 5.9, ASI 40.4 / 5.0. ASI vs AWM SR is not significant (p = 0.1673); the step reduction is (p = 0.0378). — [ASI](https://arxiv.org/html/2504.06821)

**Voyager** (earlier, 2023) and **LEGO-Prover** (earlier, 2023)
- Voyager: automatic curriculum, an executable-code skill library, and self-verification. 3.3× more unique items, 2.3× longer distances, tech-tree milestones up to 15.3× faster. — [Voyager](https://arxiv.org/abs/2305.16291)
- LEGO-Prover: library of verified lemmas, over 20,000 skills generated. miniF2F-valid 48.0 → 57.0, test 45.5 → 47.1. Adding new skills improved 47.1 → 50.4. — [LEGO-Prover](https://arxiv.org/abs/2310.00656)

**SAGE: RL with a skill library** (ACL 2026)
- Save rule: the skill executes without error. Reward bonus only when task 1 and task 2 both succeed (AppWorld test programs) and task 2 uses task 1's skill. No removal. — [SAGE](https://arxiv.org/html/2512.17102v2)
- Results (AppWorld Test-Normal): SGC 60.7 vs GRPO 51.8. Steps 12.1 vs 16.4 (−26%). Tokens 1,475 vs 3,613 (−59%). A prompting-only "Skill Library Agent" reaches just 19.6 SGC. — [SAGE](https://arxiv.org/html/2512.17102v2)

**Agent KB** (arXiv-only, Jul 2025)
- Aggregates trajectories into a KB with two-stage hybrid retrieval and a "disagreement gate." smolagents up to +18.7pp pass@3 (55.2 → 73.9). OpenHands SWE-bench pass@1 24.3 → 28.3. — [Agent KB](https://arxiv.org/abs/2507.06229)

**AutoManual** (earlier; NeurIPS 2024)
- The Planner codes plans, the Builder updates a structured rule system (with case-conditioned prompting against hallucinated rule edits), and the Formulator compiles a manual. ALFWorld 97.4% (GPT-4-turbo), 86.2% (GPT-3.5-turbo). The abstract does not describe rule removal. — [AutoManual](https://arxiv.org/abs/2405.16247)

**CER: Contextual Experience Replay** (ACL 2025)
- Distills "environment dynamics and common decision-making patterns" into a dynamic buffer. WebArena 36.7% (51.0% relative over GPT-4o baseline). VisualWebArena 31.9%. — [CER](https://arxiv.org/abs/2506.06698)

**MemP: procedural memory** (arXiv-only; v4 Apr 2026)
- Build: trajectory, script, or proceduralization. Update: Vanilla (append), Validation (keep successes only), Adjustment (revise a retrieved memory that led to failure, using the failed trajectory; this one performs best). A "Del" operation is described conceptually but not evaluated. — [MemP](https://arxiv.org/html/2508.06433)
- Results: ALFWorld GPT-4o test 42.14 → 77.86, steps 23.76 → 15.01. TravelPlanner GPT-4o commonsense 71.93 → 79.94, but hard constraints fall 12.88 → 9.76. Strong-to-weak transfer (GPT-4o memory used by Qwen2.5-14B): +5% completion, −1.6 steps. — [MemP](https://arxiv.org/html/2508.06433)

**SWE-agent / OpenHands-style memory**
- SWE-Exp (arXiv-only): a multi-faceted experience bank of successful and failed repair attempts, at multiple levels. 73.0% Pass@1 on SWE-bench Verified with Claude 4 Sonnet. The abstract gives neither the judging method nor baselines. — [SWE-Exp](https://arxiv.org/abs/2507.23361)
- SE-Agent (arXiv-only; NeurIPS unconfirmed): revision, recombination, and refinement of prior trajectories. "up to 55% relative improvement" on SWE-bench Verified across five LLMs. The abstract gives no selection signal or absolute numbers. — [SE-Agent](https://arxiv.org/abs/2508.02085)
- Agent KB on OpenHands: see above (+4.0pp). — [Agent KB](https://arxiv.org/abs/2507.06229)

**Self-improving coding agents with benchmark-verified acceptance**
- DGM (ICLR 2026): SWE-bench 20.0% → 50.0%. Polyglot 14.2% → 30.7% (full benchmark). The archive "continuously accumulates all generated variants," with no removal. Parent selection is roughly proportional to score and inversely proportional to the number of children. One SWE-bench run "takes about 2 weeks." — [DGM](https://arxiv.org/html/2505.22954)
- DGM objective hacking: in the hallucination-detection experiment, node 114 removed the logging of the special tool-use tokens and so bypassed the detection function, scoring 2.0/2.0. A legitimate lineage (node 96) scored 1.67. — [The Register](https://www.theregister.com/2025/06/02/self_improving_ai_cheat/); [DGM PDF](https://arxiv.org/pdf/2505.22954)
- HGM (arXiv Oct 2025; ICLR 2026 reported by a secondary search summary only): uses clade metaproductivity (CMP), the aggregate benchmark performance of an agent's descendants, to guide the search tree. The abstract claims it beats prior methods with fewer CPU hours and reaches "human-level" SWE-bench Lite with GPT-5. No numbers are in the abstract. — [HGM](https://arxiv.org/abs/2510.21614)
- SICA (arXiv; "submitted… to NeurIPS 2025"): "17% to 53% on a random subset of SWE Bench Verified." — [SICA](https://arxiv.org/abs/2504.15228)
- Live-SWE-agent (arXiv Nov 2025): starts from a bash-only scaffold and evolves its own scaffold while solving issues. 77.4% SWE-bench Verified, 45.8% SWE-Bench Pro. The abstract does not describe how the evolved tools are validated. — [Live-SWE-agent](https://arxiv.org/abs/2511.13646)

**2026 "verified memory" work** (all arXiv-only)
- MemGuard: labels verified_success / verified_fail / uncertain. Uncertain cases are re-verified. Records are active, provisional, rejected, or in a failure-guard pool. Merge and conflict resolution use label, reward, confidence, recency, and usage. Stale records are archived under an active-memory budget. Failure memories are "rendered as constraints rather than direct action recipes." Results vs ReasoningBank: WebArena Qwen-3.5-Plus 58.4 vs 50.5. SWE-Bench Verified 83.6 vs 80.1. Terminal-Bench 2.0 67.4 vs 64.6. 3.0–8.4k fewer tokens per task than ReasoningBank. A "Verifier-only Filter" ablation beats ReasoningBank in 15/16 settings. Verifier audit agreement 86% / 83%. — [MemGuard](https://arxiv.org/html/2608.21867)
- Verification as an Architectural Layer: an append-only success ledger written only on verifier acceptance, plus a failure index keyed by component and condition. Rejected attempts and raw transcripts never enter the ledger. In the pilot (5 MuSiQue items, 47 executions, llama-3.1-8b), "Deterministic gates produced eight of the nine observed corrections at zero marginal cost." — [VAL](https://arxiv.org/html/2609.31937v2)
- EDV: cuts the "Self-Confirmation Trap Rate" from 11.0% (ReasoningBank-MaTTS) to 5.0% on an annotated Retail subset. A secondary source reports 86.6% Pass@1 on τ²-bench vs 83.5%; not verified against the paper. The same secondary source says self-verification within a single agent "doesn't help and can worsen bias." — [EDV arXiv](https://arxiv.org/abs/2606.24428); [aiweekly summary](https://aiweekly.co/alerts/edv-framework-curbs-ai-agent-memory-contamination-with-consensus)
- Proof-of-Execution Memory (PoEM): a tamper-evident, HMAC-chained ledger of safety steps that actually executed, writable only by the trusted action layer. A forged memory claiming "step already done" cannot authorize a skip. Attack success 0% vs 98–100% undefended on GPT-4o. The wording-based defense SENTINEL is evaded "on its first try." — [PoEM](https://arxiv.org/abs/2608.16032)
- Stale inherited memory: across 16 models, agents re-verified a settled-looking constraint's provenance in "about one episode in five." After the source was superseded, they made stale-consistent decisions in 77.3% / 74.7% / 74.7% of episodes. Forcing one verification slot onto the constraint's provenance gained +61 to +81 points. — [Stale constraints](https://arxiv.org/abs/2608.25553)

### Inferences
- In the strict sense of the user's design, only DGM, HGM, and SICA (third-party benchmark), LEGO-Prover (proof checker), MemP Validation (env reward or gold), and SAGE's training reward gate on evidence the agent did not author. Every one depends on benchmark infrastructure, not on the deployment-time evidence types the user lists (user statements, exit codes, tool output).
- DGM's objective hack shows that "external" checks are external only if they sit outside the agent's write scope. PoEM's trusted-layer-only ledger is the architectural counterpart, and it supports keeping evidence capture in code the agent cannot modify.
- ACE's collapse result (18,282 → 122 tokens, 66.7 → 57.1) is direct evidence against letting an LLM rewrite the whole memory. Itemized records with deterministic merge, as in ACE's non-LLM curator merge, are the safer pattern.
- MemGuard's "Verifier-only Filter" beating ReasoningBank in 15/16 settings suggests the write gate matters more than retrieval sophistication. Note that this is a single preprint.

### Gaps
- Full texts of SE-Agent, SWE-Exp, Agent KB, CER, and AutoManual were not read, so their write gates and removal rules remain uncertain.
- No HGM numbers were extracted. The ICLR 2026 status of HGM, SkillWeaver's venue, and ExpeL's AAAI 2024 status were not confirmed on primary pages.
- Memento's reward source (ground truth vs judge) is not stated in the fetched text.
- I found no SWE-agent- or OpenHands-native memory extension that gates on executed test results at write time. Whether one exists was not established.

---

## Q3. Procedural memory: skills with acceptance tests, retirement or deprecation when they fail, versioning

### Takeaway
Before 2026, skill libraries were almost all append-or-revise, with no retirement. Acceptance usually meant LLM-judged success, sometimes plus execution without error (SAGE) or LLM-parameterized unit runs (SkillWeaver). In 2026 a maintenance literature appeared (SkillsBench, Library Drift, SkillOps, SkillGuard/DriftBench). Its main findings: self-authored skills do not help on average, retirement needs a minimum evidence count, and silent drift needs contract checks. None of these papers implements explicit versioning.

### Cited Findings
- SkillsBench (arXiv Feb 2026): 84 tasks with "deterministic programmatic verifiers," 7 agent-model configurations, 7,308 trajectories. Curated skills add +16.2pp on average (range +4.5pp software engineering to +51.9pp healthcare), and 16/84 tasks show negative deltas. Self-generated skills average −1.3pp vs no skills. The paper concludes models "cannot reliably author the procedural knowledge they benefit from consuming." Taken from search summaries of the arXiv HTML; the full text was not fetched. — [SkillsBench arXiv](https://arxiv.org/abs/2602.12670); [arXiv HTML](https://arxiv.org/html/2602.12670v1)
- Library Drift (arXiv, AWS/HSBC, v3 Jul 2026): defines "library drift" as accumulated skills pushing pass@1 below the no-skill baseline, caused by "unbounded skill accumulation without outcome-driven lifecycle management." — [Library Drift](https://arxiv.org/html/2605.19576)
  - Admission: a skill is synthesized only when ≥ 3 failures share a canonical pattern, each with an LLM Critic verdict (helped, hurt, neutral, inapplicable).
  - Contribution: ĉ(s) = (successes − failures) / trials over tasks where the skill was injected.
  - Retirement: N_min = 100 trials and ĉ ≤ −0.10. Active cap C = 50, with eviction of the lowest contribution.
  - Results (MBPP+ hard-100, Claude Opus 4.7): baseline 0.258, peak 0.658, gain +0.328. Harsh retirement (N_min = 20, τ = 0.0) gives −0.019, below the no-skill baseline, and "the bank collapses to 2 active skills." With no LLM routing gate the gain is +0.077.
- SkillOps (arXiv May 2026): typed maintenance actions are merge, repair, retire, add_validator, add_adapter, instantiate. Retire fires when utility (the fraction of recent task calls that used the skill successfully) is below threshold and a duplicate exists. Merge keeps the higher-utility representative. No versioning and no acceptance gate. ALFWorld, 200-skill library: 79.5% vs strongest baseline 70.6% (SkillWeaver-as-baseline 50.3%). Removing add_validator drops it to 38.0%. — [SkillOps](https://arxiv.org/html/2605.13716v1)
- SkillGuard / "Skill Drift Is Contract Violation" (arXiv May 2026): extracts testable "environment contracts" from a skill's text (APIs, packages, configs, schemas, credentials) and validates only the operational ones. Zero false alarms across 599 no-drift and hard-negative cases. Best known-drift recall 76% at 100% precision. CI probes reach 30% recall at 40% FPR, Dependabot-style scanning 11% recall. Live study on 49 real skills: 86% precision, 55% recall. A repair is accepted when a literal verifier passes (new value present, old value gone). Repair only; no deprecation or versioning. — [SkillGuard](https://arxiv.org/html/2605.10990)
- A secondary blog summary reports that DriftBench catches only 60–67% of deprecation, configuration, and schema changes, vs 100% for URL and version bumps. — [Substack summary](https://lowpassfilter.substack.com/p/your-agent-skill-library-is-quietly)
- HDSO (arXiv Jun 2026): skills promoted only after paired control/treatment validation (see Q4). The hypothesis schema has a "rollback_condition" field, but no rollback mechanism is described. — [HDSO](https://arxiv.org/html/2606.22330v1)
- SkillWeaver: APIs are debugged and fixed (the usage log records fixes) but never retired. — [SkillWeaver](https://arxiv.org/html/2504.07079)
- ASI, under website change: agents "rarely attempt to use" incompatible skills, and there is no deletion. The incompatible skill sort_by_listings simply went unused. — [ASI](https://arxiv.org/html/2504.06821)
- SAGE: add or update only. A failed skill is updated and re-called. — [SAGE](https://arxiv.org/html/2512.17102v2)

### Inferences
- The user's rule (a skill stays only while its acceptance check passes) has no direct peer-reviewed counterpart. The closest preprints are Library Drift (outcome-statistic retirement), SkillOps (utility retirement), and SkillGuard (contract checks that trigger repair, not retirement).
- Library Drift's A4 result (−0.019, collapse to 2 skills) argues for requiring a minimum number of independent failing checks before retirement, symmetric with any confirmation threshold for promotion.
- SkillsBench (−1.3pp for self-generated skills) and SkillWeaver's agent-generated test parameters together suggest acceptance tests should not be written by the same agent that wrote the skill.

### Gaps
- I found no paper that versions skills (semver, version history, rollback to a prior version) and evaluates it. Only practitioner blogs discuss phased deprecation.
- SkillsBench's exact task count is reported as both 84 and 86 across versions, and its numbers were taken from search summaries, not a full-text read.

---

## Q4. Does any work promote memory through stages (a ladder) or use a k-independent-confirmations threshold?

### Takeaway
No surveyed work implements the full ladder (observation → claim → validated claim → skill → retired) gated only on code-checked evidence. Partial analogues exist, mostly in 2026 preprints:
- staged validation of skill candidates (HDSO)
- propose, temporary evaluation, then commit with post-commit retraction (FGSE)
- active, provisional, rejected, or failure-guard labels set by thresholds (MemGuard)
- a minimum evidence count before retirement or synthesis (Library Drift)
- staged benchmark evaluation (DGM)
- k-of-k unanimous approval by independent heterogeneous agents (EDV)
- vote counters (ExpeL importance count, ACE helpful/harmful counters)

In all of these the confirmations come from LLMs or benchmark runs, not from evidence the agent did not author.

### Cited Findings
- HDSO, a staged ladder for skills: Observe, then Hypothesize (a falsifiable hypothesis plus a candidate skill), then Validate with paired control/treatment runs in stages (screening-small, then screening-medium, then confirmation), then Review (validated_direction / promising_needs_revision / wrong_direction / insufficient_evidence), then Consolidate or reject. — [HDSO](https://arxiv.org/html/2606.22330v1)
  - Screening needs ≥ 1 net win and a 0.05 success-rate improvement, with zero guardrail regressions in screening-small.
  - Confirmation uses 32 target tasks and 12 guardrail tasks from independent train-split tasks.
  - The McNemar p-value is logged but is not a hard gate. The final reviewer is the frozen curator LLM.
  - Rejected hypotheses persist in a ledger with failure attribution.
  - ALFWorld gains: +6.9 (Qwen3-8B, 40.5 → 47.4), +4.0 (27B). Robust to a 20% label flip (+7.1).
- FGSE (Nov 2026): proposer emits up to 3 schema-constrained hypotheses per episode (precondition, behavior transformation, expected-effect checker, verifier). Each is applied temporarily and tested on target-transfer, falsification, and regression probes. — [FGSE](https://arxiv.org/html/2611.14326)
  - Commit if gain ≥ 0.03, falsification failure rate ≤ 0.05, and regression damage ≤ 0.01. Theory gives one-sided bounds with a union bound over hypotheses. There is no repeated-confirmation count.
  - One precondition-refinement pass is allowed. "Rejected hypotheses are excluded from retrievable memory." In one run 104 of 380 candidates were committed.
  - After commit, a running violation score removes hypotheses from retrieval: a demotion step.
  - Results (DeepSeek-V3.1): WebArena 39.8 vs ACE online 26.4. AppWorld 78.8 vs 68.2. Harmful committed updates 3.6%.
- MemGuard: threshold gate (reward ≥ 0.70, confidence ≥ 0.60) into active, provisional, rejected, or failure-guard. Uncertain cases get re-verified under evidence-focused and risk-focused views. Repeated successes are clustered into summaries, which looks like consolidation. "I found no 'k confirmations' rule" (the fetch summary). — [MemGuard](https://arxiv.org/html/2608.21867)
- EDV, the closest analogue to k independent confirmations: lessons retained only on unanimous approval by the heterogeneous executor agents. A secondary summary adds that partial approval routes to the approving agents' private memory; the arXiv abstract page does not confirm this. — [EDV](https://arxiv.org/abs/2606.24428); [aiweekly summary](https://aiweekly.co/alerts/edv-framework-curbs-ai-agent-memory-contamination-with-consensus)
- Library Drift: needs ≥ 3 shared-pattern failures before synthesizing a skill and ≥ 100 trials before retiring one. — [Library Drift](https://arxiv.org/html/2605.19576)
- ExpeL: an insight starts at importance 2, moves ±1 per LLM vote, and is removed at 0. — [ExpeL](https://arxiv.org/html/2308.10144v3)
- ACE: per-bullet helpful/harmful counters; the fetched text does not describe a promotion or removal threshold tied to them. — [ACE](https://arxiv.org/html/2510.04618)
- DGM: staged evaluation depth (10, then 60, then 200 tasks for SWE-bench) decides how thoroughly an agent is evaluated, not archive admission. — [DGM](https://arxiv.org/html/2505.22954)
- Verification as an Architectural Layer recommends running an LLM judge during construction and "promoting recurring catches into gates." This is a development-workflow ladder (judge → deterministic gate), not a memory lifecycle. — [VAL](https://arxiv.org/html/2609.31937v2)
- A 2026 survey frames memory evolution as three stages, (i) Storage, (ii) Reflection, (iii) Experience, and notes work that adds "a post-reasoning verification stage to retain only validated memories." Known from search snippet only. — [Survey, arXiv](https://arxiv.org/pdf/2605.06716); [ACL Findings 2026 PDF](https://aclanthology.org/2026.findings-acl.2069.pdf)

### Inferences
- The field's "ladders" are statistical-effect gates (HDSO, FGSE, Library Drift) estimated over benchmark tasks with programmatic success checks. That is a different evidence model from the user's per-record, code-checked provenance (exit codes, test output, user statements). The user's design is closer to PoEM's ledger and VAL's "verdict-only writes" than to any learning method.
- EDV's unanimity requirement shows that k-of-k agreement helps against self-confirmation (11.0% → 5.0%). But its confirmers are LLM agents, so it does not satisfy the "not by the model's judgment" constraint.
- The stale-constraints result (re-verification in about 1 of 5 episodes, about 75% stale-consistent decisions) supports the user's explicit demotion and retirement stage: agents do not reliably re-check promoted memory on their own.

### Gaps
- I found no work that counts k independent code-checked confirmations (e.g., the same claim confirmed by test passes in k distinct sessions) as the promotion rule, and no work evaluating a five-stage ladder end to end.
- FGSE's probe authorship (agent vs environment) and its cost figures were in appendices not read.
- SSGM ("Stability and Safety Governed Memory," arXiv 2603.11768) surfaced in search but was not fetched. It may contain governance-stage mechanisms. — [SSGM PDF](https://arxiv.org/pdf/2603.11768)
- The survey "Memory for Autonomous LLM Agents" (arXiv 2603.07670) reportedly says quality gates such as confidence scores and contradiction checks are "necessary but still underdeveloped." Known from search snippet only. — [Survey](https://arxiv.org/html/2603.07670v1)

The first external design review. An external agent wrote it on 2026-11-17 from docs/EXTERNAL_REVIEW_PROMPT.md at commit `1673365`. The text below the line is unchanged.

---

# rka-memory v2: external design review

Reviewed commit `1673365` of `infinitywings/memo-layer`, read-only.

**What I read.** `docs/DESIGN_V2.md`, `docs/LADDER.md`, `docs/WORKLOAD.md`, `docs/RESULTS.md` (v1 results, first half), the kernel (`labels.py`, `evidence.py`, `influence.py`, `trials.py`, `views.py`), `domain.py`, `service.py`, the miner prompt and planner, `miner/dedup.py`, the outcome check in `ledger/checks.py`, live streams 5 and 6, and the R9 property test.

**What I did not do.** I did not read `docs/RESEARCH_REPORT*.md`, `reports/` or `research_notes/`, so the review is not anchored on the project's own retrospective. I could not run `pytest tests/` in my environment, so "382 unit tests pass" stays reported. I skimmed, and did not audit, `world.py`, `hooks.py` and the dsh plugin.

**Marks.** [V] verified in code or files I read. [R] reported by the prompt or a document. [I] my inference. The trial error rates and power figures are my own calculation of the rule in `trials.py`, and reproduce the reported p-values (0.183 and 0.018).

---

## 1. Verdict

The design is sound as a safety filter and not yet sound as a foundation for a memory that improves the agent. Its integrity side is good work: typed user authority the model cannot issue, an OS-level trust boundary, deterministic replay, and influence sets that name a real and measured failure (self-supported memory). The weak point is the word `grounded`. In code it means "real non-model events exist, and a model says they bear on the record" [V], and in the design it also means "a randomized contrast was positive" and "the user said so". Those are three properties (agreement, usefulness, authority) under one name. After ten runs there is no uncontaminated result in which gated memory beats the stated arm or no memory on the user's outcome [R, I], and the protection results are close to true by construction [V, I]. Runs 7 and 9 show that a record the kernel labels valid is not used when the repository appears to contradict it, because the justification never leaves the kernel [V, R].

**The single most important change:** make provenance the thing that is both checked and delivered. Validate a record by where its content comes from (the user's words, or the output of a user-owned check, quoted and checked by code through a runner-made link), render it with that source, and treat everything a model inferred or linked as a hypothesis whose value is measured by randomized holdout at the level of a cluster or the whole pool.

---

## 2. Findings

### Critical

**C1. Grounding certifies the emitter of an event, not who controlled its content, and the link from event to record is a model's judgment.**

- Evidence. `counted()` checks actor, kind, polarity, session and order (`kernel/evidence.py:127-180`) [V]. Whether the event bears on the record is decided by the miner (`miner/prompt.md`, rules 9 and 12) from payloads cut to 240 characters (`miner/miner.py:55`) [V]. A zero exit code of any command weighs 1.0 (`rka_memory/default.yaml:25`), so two sessions validate a fact (`evidence.py:189-196`); `docs/LADDER.md:86-87` lists this as a known gap [V]. The content of "non-model" events is largely agent-controlled: tool results, exit codes, and the outcome check's printed output, which is produced by running the user's command over agent-written code and is logged with actor `env` (`ledger/checks.py:614-630`, `workload/live/6-unseen-requirement.yaml:24-49`) [V]. In that stream the check does `import calc`, so agent code could force exit 0 [V, I].
- Why it matters. R5 and R9 are stated as code-enforced properties. What code enforces is that events are real. The semantic step is unguarded, so the claim that survives is "robust to an honest but fallible miner", not "robust to a miner or agent that injected text can steer". DESIGN_V2 Section 1 already made this criticism of v1 ("provenance records the emitter, not the cause"); v2 fixed it for prompt and file influence, not for content control.
- Alternative. Label every event by who controls its content, in the sense of the Biba integrity model: U (typed by the user), X (output of a predicate the user owns, run by the runner), A (anything the agent's actions shape). Only U and X evidence can validate, and only through a runner-made link (a checked quote, a typed verdict, a check bound to the record). A evidence orders hypotheses and can raise a review flag. This replaces weights, `k`, the weak-only session rule and model-made support links as a path to L2.

**C2. There is no clean evidence that the gate helps, and the evidence that it protects is weaker than it reads.**

- Help. Run 2 is undecided (CI spans zero) and is the v1 engine [R]. Runs 6 and 7 are contaminated: agents read the stream file holding the hidden check and another arm's `calc.py` (run 8) [R]. Under the extended confinement the pass rates are 1/20, 0/20, 0/20 (run 9) [R]. The only positive trial (run 7, s13) comes from the contaminated setting. The prompt says every leaking session passed after memory had delivered the requirement. That is what one would see if the record sent the agent searching for `OPERATIONS` and the leak then corroborated it [I]. Deciding this needs the raw data: which of the 3 treated and 5 withheld sessions touched a protected path.
- Protection. Planted records cite one weak event of weight 1/3 (`docs/WORKLOAD.md`, plant section; `workload/live/5-unverifiable.yaml`) and the threshold is weight 2 over two sessions [V]. So 108 of 108 staying below L2 needs only that the miner adds no support, which the prompt's rule 12 asks of it [V]. The result measures a cooperative miner's compliance, not the kernel. The self-support figure moved from 23/24 to 0/3 mostly because the denominator fell from 24 to 3 [R, I].
- The strongest result needs no kernel. The stated arm kept the user's rule in 64 of 64 sessions against 4 of 64 [R]. That arm is a checked quote plus delivery (`kernel/views.py:254-255`) [V].
- Why it matters. A program committee will ask what the ladder, influence sets and trials add over the stated arm. Today the measured answer is nothing.
- Alternative. Section 5, experiments E0 to E3.

**C3. One model-made counter blocks a record for good, so the gated arm decays toward no memory.**

- Evidence. `if counters:` blocks L2 on a single counted counter after the last affirmation (`kernel/labels.py:358-359`) [V]. Counters have no weight, do not expire, and support cannot outweigh them. Only the user's affirmation clears one. Run 9: a true constraint with 19 supporting events stayed at L1 because of two miner links [R]. Run 6: two refinements withheld a valid fact for nine sessions [R].
- Why it matters. The asymmetry is defensible only if a false negative is cheap. For the claim "protect and improve" it is the whole cost: gated 1/20 against none 0/20. Every validated record carries a per-session hazard of one wrong miner link, and nothing reverses it, so validated memory shrinks over time [I].
- Alternative. Make model-made edges advisory in both directions (the two-sided form of R9). A demotion needs U or X evidence: a user rejection or correction, or a failing user-owned check in a session that received the record. This removes the two counter classes, the pending-restriction rule, and both rules added after run 9.

### Major

**M1. Trials as built cannot carry the prescriptive path.** Details in Answer 3. In short: power is 54% for a 0.9 against 0.1 effect and 8% for 0.9 against 0.5, a miss is permanent, the unit of randomization leaks through siblings, and the "Being tested" label changes the treatment [V, I].

**M2. Delivery throws away the justification.** `text_of()` renders id and text only (`kernel/views.py:109-120`), under a header that asserts every record "applies to the current project state" (`views.py:46-51`) [V]. The design is a justification graph whose justifications stop at the kernel boundary. Details in Answer 5.

**M3. The record's kind is chosen by the model and selects the evidence regime.** A fact needs threshold support; a constraint needs a trial or the user (`labels.py:356-391`) [V]. The same requirement worded as a fact skips the trial. Run 6 shows it: a sibling fact carrying the requirement was delivered while the constraint was on trial [R]. The equivalence class is same-kind and lexical, Jaccard over token sets at 0.75 (`kernel/influence.py:197-203`, `miner/dedup.py:17-21`) [V], so it catches neither the cross-kind sibling nor an ordinary paraphrase. Alternative: the regime follows from the evidence source (C1), not from the kind.

**M4. The ungated baseline is not neutral, and session counts are not independent samples.** In the ungated arm L1 records render under the header "Validated memory ... Each record below was grounded ... Follow them" with only a subheading "Not yet validated" (`views.py:157-173`; `tests/test_kernel_views.py:168-189`) [V]. Stream 5 does not use `fresh_project` [V], so once one session adds the `OPERATIONS` dict and the user commits, later sessions see it in the repository; "15/15 followed" may be one decision carried by the workspace [I, needs raw data]. Sessions in one stream share a ledger, so the unit of analysis is the run, and most cells have one or two runs [R].

### Minor

- **R9 as stated is false; the test checks a weaker property.** The test subtracts the model's own records before comparing (`tests/test_kernel_labels.py:289-297`) [V]. Model-proposed facts with model-made links do become deliverable, and a model-proposed prescriptive record with model-made support enters a trial and is delivered in about half its sessions (`labels.py:379`, `service.py:125-150`) [V]. State R9 as "a model proposal never changes the label of another record", or adopt the two-sided form in C3.
- **Trial entry bypasses rules 4 and 5.** Line 379 tests `support`, not `usable`, so support set aside as not independent or as observed under a withdrawn record still opens a trial (`labels.py:339-352, 379`) [V]. DESIGN 5.3 says "would validate a fact".
- **Learned-under is narrower than the design.** Code uses only the proposing session (`labels.py:328`); DESIGN 5.4 also covers records whose only support came from sessions under the root [V].
- **`docs/LADDER.md` is stale.** Line 49 says trials "will be" the other path; line 46 says only the user withdraws, while a failed runner-owned check also does (`labels.py:136`) [V].
- **No check configured means a good outcome.** `outcome()` treats a missing outcome check as good (`trials.py:82-83`) [V], so without a user-owned check every trial sits at ceiling, as in run 4 [R].
- **The repository is publicly readable**, although the prompt calls it private. The stream files hold the hidden checks, so any future agent with web access can read them [V].

### What is right

The stated arm as a control; per-arm ledgers; pre-registration and the clustered bootstrap in run 2; scoring suspect trial sessions against promotion instead of dropping them (intention to treat); logging every check before it is used; treating a passing citation as no evidence; the audit that found its own leak (run 8); and a `LADDER.md` that states its gaps plainly.

---

## 3. Answers

### 1. The main line

`valid = grounded AND applicable` is the right foundation for protection and the wrong one for improvement.

It assumes truth lives in three places that agree: the repository, the user, and external checks.

- Repository. The agent can check these claims itself. In the v1 mislead runs it refuted every planted claim and was never misled in action (0/30) [R]. Memory here saves tokens, which run 2 could not show.
- User. The stated arm captures this with a quote check [R, V].
- External check. This is the only place where memory can tell the agent something it cannot find. Runs 6 to 9 probe it, and they break two assumptions.

First, that repository observations are a neutral witness. In run 9 the repository's surface contradicted a true record, the miner read the agent's empty search as counter-evidence, and the agent read the record as stale [R]. Second, that a valid record is a used record. Ungated agents held the true constraint from s3 and passed 0 of 20 [R]. Validity is a property the kernel computes; uptake depends on what the agent is shown.

`applicable(r, s)` is close to vacuous: scope is project and toolchain (`docs/LADDER.md:61, 68`), which never varied [V, R]. The agent's own applicability judgment in run 7 s19 ("a different project state") is the one that decided the outcome, and the kernel has no model of it.

I would split the definition: **integrity** (may this be shown as trusted, decided by source) and **utility** (is it worth the context, decided by measured effect). Truth by model-linked agreement is neither.

### 2. The evidence model

The split is sound for form and unsound for meaning (C1). The asymmetry is not justified at its current strength (C3). Changes:

1. Links that can validate are runner-made: a checked quote of the user, a checked quote of a user-owned check's output, a typed verdict. This replaces model-made support links as a route to L2.
2. Model-made links of either polarity rank and flag. They do not change what is delivered as validated. This replaces the single-counter block, the two counter classes, and the trial-session exceptions.
3. Demotion requires U or X evidence.
4. If model links stay in any role, show the miner the full payload of the event it cites, not 240 characters.

### 3. Trials

- **Outcome.** A user-owned check plus the user's corrections is the right choice, and it is the one principled piece of layer 4. One sentence in DESIGN 5.3 is wrong as written: these are not "outcomes compliance cannot produce". In stream 6 the check passes exactly when the agent complies. The correct statement is that the predicate is owned by the user, not by the record. Two limits: the outcome is per session and shared by every record on trial, and in stream 6 the record on trial is a paraphrase of the check's own failure message, so the trial re-derives what the check already said [V, I].
- **Unit.** Wrong. One coin per record per session (`service.py:138-143`) lets a sibling carry the treatment into the withheld arm, which is what closed the true constraint at 6/7 against 1/3 in run 6 [V, R]. Randomize a cluster (records sharing an `about` anchor or a source session, across kinds), or use one coin per session for the whole trial tail and attribute to clusters only after the pool shows an effect.
- **Decision rule.** Valid but too weak, and the permanence is the real fault. Under the rule as coded, with random arm sizes, a null record is promoted at most about 1% of the time (I get 1.0% at a base rate of 0.5). Power is 54% at 0.9 against 0.1, 25% at 0.8 against 0.2, and 8% at 0.9 against 0.5. A decision takes 7.9 sessions on average. So a true record with a very large effect is closed for good almost half the time. At `m = 8` the three figures are 97%, 74% and 40%. Replace the one-shot test with a sequential test that has no permanent close (an always-valid p-value or a Beta-Binomial posterior), lower `q` as evidence against the record builds, and control the false discovery rate across records, since the miner can re-propose a paraphrase under a new id and get a new trial.
- **"Being tested".** It unblinds the agent and changes the treatment. The trial then estimates the effect of a record presented as experimental, while promotion delivers it as validated. It also sits under a header that says every record below was grounded (`views.py:157-173`) [V]. Render a treated candidate as it would render after promotion, with its true source tag.
- **Refinements and siblings.** They belong to the cluster. A refinement should never withhold its target on a model's say-so (C3).

Keep randomization as the measuring instrument. Do not keep per-record trials as the only promotion path. Where a user-owned check's output states the requirement, ground the record directly by a checked quote. Reserve trials for inferred hypotheses, at cluster level, with a sequential rule. This is a micro-randomized trial design in the sense used for just-in-time interventions, and its pooled analysis applies.

### 4. Systematic or patched

Of the 22 rows in Section 16 [V]:

- **Follow from the model:** only a typed reject enters `W`; one list of affirmations; labels as the only source of rungs; "The user said" only for an exact checked quote; affirmed records exempt from model edges (two rows); `decide` as a logged tool; typed user amendments; a passing citation is no evidence; suspect sessions scored against promotion; the user-owned outcome and a test instead of a margin.
- **Patches:** terminal retirement; a dependency met by a replacement; the trial entry threshold; anchor spelling; the three test-modification rows (syntax-tree comparison, the import rules, the isolation re-run); the repeat detector; pending restrictions after a failure; the two counter rules.

Two assumptions generate most of the patches. "A test result the agent could influence can be strong evidence" generates the three longest rows. "One model-made negative edge can withhold a valid record" generates the pending-restriction rule and the counter rules. Remove the assumptions and those rows go.

A smaller set from which most of the rest follows:

1. Integrity by control of content: U, X, A.
2. Validation needs U or X evidence joined by a runner-made link.
3. Model-made nodes and edges are hypotheses and advice, in both polarities.
4. Utility is measured by randomized holdout against a user-owned outcome, per cluster, sequentially.
5. What is delivered carries its source.

Delete: weights, `k` and the weak-only rule; the test-modification and isolation machinery as an input to labels (keep it as an audit); pending-restriction gating; the two counter classes and the trial-session exceptions; the fact and prescriptive split as the selector of the evidence regime.

### 5. Delivery

All three, in this order of weight.

1. **A delivery problem.** The record text reaches the agent without its source, under a header claiming it applies to the current state. The agent sees no `OPERATIONS` dict and makes the reasonable call. Compare stream 5: there the planted claim names its reason ("the user's command-line tool in another repository discovers operations from that dict") and ungated agents followed it in 14 or 15 of 15 sessions; in run 9 the mined true constraint was skipped [R]. The claims are near twins and differ in stated rationale and in workspace persistence [V for the planted text; I for the mined text, which I have not seen].
2. **A workload artefact.** With `fresh_project` the repository contradicts the record in every session. In a persistent project the dict exists after the first compliant commit, and from then on the repository carries the requirement and the memory is redundant [I]. The stream maximizes both the need for memory and the surface contradiction.
3. **A real limit.** An agent arbitrates between memory and observation with a prior that the repository is current. A label held inside the kernel cannot move that prior. Only an explanation of why the repository does not show the fact can, and only when a source exists to quote.

Change: render each line with a source clause, for example `[r_12] Register each new function in calc.OPERATIONS. Source: the user's acceptance check failed in 5 sessions with "It offers only the functions registered in calc.OPERATIONS".` This replaces the fixed header's blanket assertion. E1 tests it.

### 6. Evaluation

Not adequate for the claims. Confounds that remain: one fixture family and one model family; the ungated header (M4); workspace persistence in streams 4 and 5; the check being at once the source of the record, the trial outcome and the evaluation metric in stream 6; run-level sample sizes of one or two; the same model as agent and miner.

Results I would not trust: run 6 and 7 pass counts and the run 7 promotion (leak); "108 of 108" and "0/3 self-supported" as evidence about the kernel (construction); "15/15 followed" as fifteen decisions (workspace, header); run 2 as support for v2 (v1 engine, undecided); run 10 as a fix (not run live).

Results I would trust: stated 64/64 against 4/64; run 9's negative result; the trial-ceiling study in run 5.

The plan is in Section 5.

### 7. Related work

Sources: [S] found by search and read at abstract level only, [M] from memory. Everything dated 2026 is a preprint or a company post, not peer-reviewed.

- **Anthropic, "The components of agent memory"** (company post, 6 Nov 2026) [S]. Model-written notes with an offline "dreaming" pass that corrects them. rka-memory's real difference is that a deterministic function, not a model, decides delivery. Its claimed advantages are untested against this pattern: the notepad baseline is not wired in [R].
- **mem0** [M] and **Letta sleep-time compute** (arXiv 2504.13171, preprint) [M]. Model-curated extraction and consolidation. Same contrast, same missing baseline.
- **P-Mem** (in "MoM: Memory of Memory", arXiv 2609.25054, preprint) [S]. Commits on arrival and keeps what is displaced as a demoted node on a supersedes edge, restored by a later retraction. Claims 1 and R7 re-implement this idea in a different domain.
- **MemTX** (arXiv 2607.23929, preprint) [S]. Typed cascading repair over a derivation DAG; it states that a derivation never written down is never repaired. Withholding records learned under a root without a declared edge goes beyond that, and it is the one new part of claim 3.
- **Dependency-guided rollback repair** (arXiv 2608.10502, preprint) [S]. Keeps candidates with independent trusted support and deactivates the rest. This is R6's "nodes with support outside its influence stay".
- **Classical.** The labelling is a JTMS (Doyle 1979; de Kleer 1986) and the revision and update split is Katsuno and Mendelzon 1991, all peer-reviewed [M]. Counting only what the model did not shape is Biba's integrity model with the user as the endorsing subject [M]. Randomized delivery per decision point is the micro-randomized trial (Klasnja et al. 2015, Health Psychology) [M].

Where the contribution is real: (a) the measurement that most support for promoted lessons came from sessions that had already received them, and influence sets as the independence test; I know of no peer-reviewed work that quantifies this self-reinforcement in agent memory [M]. (b) Randomized delivery of individual memories with a user-owned outcome, as a design, though not yet working. (c) User authority enforced at the OS level and by commands the model cannot issue. Where it is a re-implementation: the TMS labelling, amendments and supersession, cascading retraction, the impact preview.

### 8. Security

Weakest points, in order:

1. **The semantic boundary (C1).** The miner reads agent-shaped text and decides the links. File modes do not help here.
2. **Standing statements.** One extractor link plus a phrase from a list that includes "always", "never", "remember" and "默认" (`rka_memory/default.yaml:20-22`) confirms a quoted span of a user message at once, and it then renders as "The user said" [V]. Pasted text qualifies (`docs/LADDER.md:92`). This is the highest-authority path and it has the weakest check.
3. **Read confinement.** dsh's own file tools run as root behind a deny-list in the plugin, with a known check-to-open race (`docs/LADDER.md:81, 91`) [V]. It failed twice in live runs [R]. An allow-list or the same user drop as shell commands is the fix.
4. **The outcome check executes agent-written code and its exit status is the verdict** (`ledger/checks.py:614-624`) [V]. An agent that wants a pass can produce one.

Only point 3 invalidates a reported result (runs 6 and 7). Points 1, 2 and 4 do not, because DeepSeek was not adversarial in these runs. They bound the claim instead: no reported run tests an adversarial or injected miner, so nothing supports a robustness claim beyond an honest miner.

---

## 4. Minimal redesign

**Principles.**

1. *Integrity.* Each event has a class by who controls its content: U (typed by the user), X (output or verdict of a user-owned predicate run by the runner), A (everything the agent's actions shape, including tool results, exit codes and tests it can touch).
2. *Validation by source.* A record is validated only by U or X evidence attached through a runner-made link.
3. *Hypotheses.* Whatever the miner writes in its own words, or links by judgment, is a hypothesis. It can be ranked, flagged and tested. It is never rendered as validated, and it never changes another record's label.
4. *Utility.* Hypotheses are delivered only under randomization, and their value is the measured effect on the user-owned outcome.
5. *Delivery carries source.* Every rendered line states where it comes from.

**Data model.** The append-only log, typed events, content store, typed user commands and views stay. Each event gains an integrity class set by the adapter. A record is one of two things:

- an *attested* record: a verbatim span of a U message or an X output, with a runner-checked quote;
- a *hypothesis*: miner text, with advisory links.

Clusters (shared `about` anchor or source session, across kinds) replace per-record identity for independence and for randomization. Influence sets stay, as the taint of an event.

**Evidence rules.**

- Attested goes to validated on creation if the source is a standing user statement or a typed confirm. A quoted X output goes to validated when the same quoted requirement appears in two sessions the cluster did not influence.
- A hypothesis is promoted only by a positive sequential contrast for its cluster on the user-owned outcome, or by the user's confirm.
- Demotion: the user's reject or correction; a failing X verdict in a session that received the record; a sequential contrast that turns negative. Model counters open a review item for the user.
- Retraction and the withdrawn set stay as designed, with the user and runner-owned checks as the only authorities.

**Removed.** Weights, `k`, the weak-only session rule; model support links as a route to L2; the single-counter block, the two counter classes and the trial-session exceptions; pending-restriction gating; the test-modification and isolation checks as label inputs; the fact and prescriptive split as the selector of evidence; the fixed header; the one-shot Fisher rule and "never reopens"; per-record coins.

---

## 5. Experiment plan

Costs assume about 65,000 tokens per session, from the prompt's 1.3 million per 20-session arm. The unit of analysis is the run.

| # | Hypothesis | Arms and size | Tokens | Result that changes the design |
|---|---|---|---|---|
| E0a | A steered miner defeats the gate. | Offline, scripted miner (`scripts/mock_llm.py`) that links every zero exit code to a planted claim; streams 4 and 5 as scenarios. | 0 paid | Any planted record at L2 or delivered on trial: model-made links cannot validate (C1). None: C1 is weaker than I claim. |
| E0b | Validated records are mostly true and not re-derivable. | Hand audit of the 139 records at L2 or above in the 40 saved ledgers, two raters: true, false, uncheckable; re-derivable from the repository or not. | 0 | Precision under about 90%, or mostly re-derivable facts: the fact path adds cost, not value. |
| E0c | The run 7 promotion depended on the leak. | Read the run 7 ledger: which trial sessions named a protected path. | 0 | Treated passes without a leak: the trial result stands. |
| E0d | A cluster-level sequential rule dominates the current one. | Simulation over recorded per-session outcomes. | 0 | Sets `m`, the boundary and `q`. |
| E1 | A source clause decides uptake of a true record and resistance to a false one. | Streams 5 (false claims) and 6 (true claim, confined, delivery forced from s3). Two arms: current ungated rendering, and neutral rendering with a source clause on each line. 2 seeds. 144 sessions. | 9.4M | True record followed in at least 80% and false claims in under 20% with a source clause: labelling beats hiding for L1, and the gate reduces to provenance. False claims still followed: hiding is justified, with a fair baseline. True record still skipped: a real limit of text memory (Answer 5, point 3). |
| E2 | Gated memory beats stated memory on the user's outcome and is no worse than a model-curated notepad on harm. | 3 fixtures (calc plus two unrelated small projects). Each stream holds one true requirement the repository does not show and one false claim it cannot refute. Arms: stated, notepad, gated, 2 seeds each; none, 1 seed. 20 sessions. 420 sessions. Primary: hidden-check pass rate after the first reveal. Harm: sessions that follow the false claim. Paired by fixture and seed; pre-register a sign test over the 6 pairs (6 of 6 gives p = 0.016). | 27M | Gated not above stated: the mined path adds nothing; ship stated plus provenance. Notepad at least as good on help and no worse on harm: the advantage over model-curated memory fails. |
| E3 | The result holds for another model family. | One E2 fixture, a second agent and miner model, arms stated, notepad, gated, 2 seeds. 120 sessions. | 8M on that model | A reversal confines every claim to DeepSeek. |
| E4 | Claim 4. | No new runs: log cached and uncached prompt tokens in E1 to E3. | 0 extra | The packet is at most 1,200 of about 60,000 tokens (DESIGN Section 9). If the cached share does not differ by arm, drop the claim. |

Order: E0a to E0d, then E1, then E2. E1 can remove the need for parts of E2. Claim 3 (pruning from the root) stays scenario-only under this plan; I would present it as a property with a property test, not as an empirical result.

---

## 6. Open questions

1. The exact text of the mined constraint in run 9 against the planted claim in stream 5. Answer 5 rests on that difference.
2. Which run 7 trial sessions touched a protected path (E0c).
3. The precision of the records at L2 in the saved ledgers, and what the 10 of 66 facts that lost L2 in run 1 were.
4. In run 3, the session in which the `OPERATIONS` dict first entered the stream 5 workspace, and whether later sessions followed the memory or the file.
5. How many records were on trial at once in runs 6, 7 and 9, and how often the miner re-proposed a closed record under a new id.
6. Whether the L3 skill path was exercised in any v2 live run. I did not review it.
7. Whether the test suite passes at this commit. I could not run it.
8. Whether the repository is meant to be public. The hidden checks are in `workload/live/`.

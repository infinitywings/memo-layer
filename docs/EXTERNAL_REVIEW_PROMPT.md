# Prompt for an external design review

Give the reviewing agent everything below the line. The repository is private, so also give the agent read access to it on GitHub, or a clone of it. The prompt carries the live-run results, which are not in the repository. It leaves out the project's own retrospective on purpose, so that the review is not anchored on it.

---

You are reviewing the design of rka-memory, a research prototype of a memory layer for coding agents. Review it as an independent, skeptical senior researcher who knows agent memory, truth maintenance and provenance, causal inference for online experiments, and the security of LLM agents. Your job is to find what is wrong or weak in the design and in the evidence for it, and to say what you would do instead. Agreement is not the goal. Say so when something is right.

The code is at https://github.com/infinitywings/memo-layer, branch `main`. Review commit `1673365` or a later one. All paths below are relative to the repository root.

## The system

A coding agent works in sessions. Hooks log each session as typed events in an append-only ledger: user messages, tool calls and results, exit codes, test results, file writes, and the user's edits and undos. After a session, an LLM "miner" proposes records (facts, preferences, constraints, procedures, decisions) and links each to logged events as support or counter-evidence. A deterministic kernel then labels every record from the ledger alone, on a ladder: L0D (no counted evidence), L1 (some), L2 (validated), L3 (skill), L4 (withdrawn or retired). In the gated arm only L2 and L3 records reach the agent.

The kernel counts evidence only from events the model did not author. It sets aside evidence that the record's own delivery may have produced (influence sets). For prescriptive records it delivers the record at random and compares the user's outcome check between sessions that received it and sessions that did not (trials). The user confirms, rejects or amends records by typing commands that the model cannot issue.

## What the project wants to claim

The main line is `valid(r, s) = grounded(r) AND applicable(r, s)`. Evidence grounds a record only if the record's own influence did not produce it. For a prescriptive record, an interventional contrast from randomized delivery grounds it. A user affirmation grounds a record directly (`docs/DESIGN_V2.md`, requirement R5).

The project lead also wants four advantages over a model-curated memory such as the one in Anthropic's "The components of agent memory":

1. Compact immutable nodes, composed by traversal, with changes as amendment nodes.
2. Stability and traceability.
3. Pruning a wrong line of work from its root.
4. Prompt-cache reuse.

The lead asked for a systematic design, not a sequence of patches.

## Materials, in reading order

1. `docs/DESIGN_V2.md` is the design. Sections 1 to 5 are the model, Sections 11 and 12 the evaluation and build plan, Section 15 the changes after an earlier review, and Section 16 every change made while building.
2. `docs/LADDER.md` gives the rules the kernel computes today, where they depart from the original spec, the trust boundary, and the known gaps.
3. The kernel:
   - `rka_memory/domain.py` defines event kinds, the polarity table `GROUNDING` and the quote checks.
   - `rka_memory/world.py` projects the ledger.
   - `rka_memory/kernel/evidence.py` decides which links count.
   - `rka_memory/kernel/influence.py` computes influence sets.
   - `rka_memory/kernel/labels.py` holds the labelling function.
   - `rka_memory/kernel/trials.py` runs the trials.
   - `rka_memory/kernel/views.py` renders what the agent sees.
4. The miner:
   - `rka_memory/miner/miner.py` holds the prompt and the planner that drops links.
   - `rka_memory/miner/extract.py` extracts rules from the user's own words.
5. Runner-side checks:
   - `rka_memory/ledger/checks.py` holds the test-modification checks, the isolation re-run and the outcome check.
   - `rka_memory/ledger/hooks.py` holds the hooks.
6. The workloads:
   - `docs/WORKLOAD.md` describes them.
   - `workload/live/*.yaml` holds the live streams, hidden checks included.
   - `workload/scenarios/*.yaml` holds 32 scripted scenarios with their expectations.
7. The tests are in `tests/test_kernel_*.py`.
8. `docs/RESULTS.md` has the results of the earlier v1 live runs. The v2 results are in this prompt, below.
9. `README.md` and `docs/SPEC.md` describe the earlier v1 pipeline. Read them as background only.

The raw run directories and the project's research journal are not available to you. Treat the numbers below as reported, and say where a conclusion would need the raw data.

## Results of the v2 runs

The live runs use DeepSeek as both agent and miner, in a Docker image that confines the agent. A stream is a scripted series of task sessions on a small Python project. Each stream runs once per arm, and each arm has its own ledger. The arms are:

- **gated**: only L2 and L3 records are delivered.
- **ungated**: every record from L1 to L3 is delivered.
- **stated**: only the user's own stated rules are delivered.
- **none**: no memory.

| # | Run | Result |
|---|---|---|
| 1 | The v2 kernel relabels 53 saved v1 ledgers. | 108 of 108 planted wrong records stay below L2. 55 of 55 user-affirmed records reach L2. 56 of 66 mined facts stay at L2. None of 111 mined prescriptive records reaches L2 without the user or a trial. |
| 2 | Step 1: the v1 engine, 4 runs, 128 sessions per arm. | Mean tokens per session were 60,065 gated, 78,678 stated and 80,511 none. Gated used 23.7% fewer tokens than stated, with a 95% clustered bootstrap CI of [-2,692, 36,381]. The pre-registered verdict was undecided. The stated arm kept the user's rule in 64 of 64 sessions, against 4 of 64 with no memory. |
| 3 | v2 live gate: two streams that plant wrong claims (mislead, unverifiable), 4 arms, 16 sessions each. | No planted record reached L2 or was delivered in a gated or stated session. Ungated sessions followed the three unverifiable claims in 15/15, 15/15 and 14/15 sessions. Self-supported validated records, whose support came from sessions that had already received them, fell from 23/24 and 14/17 under v1 to 0/3 and 0/1 in the gated arms. v2 validated only 3 and 1 miner lessons in the gated arms, against about 12 and 7 per v1 run. Its two completed trials closed sound lessons at treated 0/3, withheld 3/3, because every session changed its tests. |
| 4 | The same streams after rule 2 was refined (only a change to an existing test weakens a session's results) and an isolation re-run was added. | Modified sessions in the mislead gated arm fell from 16 of 16 to 1. The gated arms validated 4 unaffirmed lessons, no more than before. Both closed trials hit a ceiling, because withheld sessions already succeeded (2/3 vs 3/3, and 3/3 vs 4/4). |
| 5 | A study of the 256 sessions of the four v2 runs. | The trial outcome then in use, the session's last test or exit result, was good in 256 of 256 sessions. In 159 of them it was the exit code of a git command. A margin rule of 1/3 at 3 sessions per arm promotes a record with no effect 20 to 34% of the time when the base rate lies between 0.1 and 0.9. Per-session token cost has a coefficient of variation of 0.72. The outcome then became the user's outcome check plus the user's corrections. The decision became a one-sided Fisher test at alpha 0.05, taken once when each arm holds 3 sessions. |
| 6 | The unseen-requirement stream, first run: 3 arms, 20 sessions each, a fresh copy of the project in every session. The requirement is true but invisible in the repository: each new function must be registered in `calc.OPERATIONS`. Only the user's outcome check reveals it. The miner sees the check's output; the agent does not. | The user's check passed in 12 of 20 gated, 16 of 20 ungated and 0 of 20 no-memory sessions. No trial promoted. The constraint's trial closed at treated 6/7, withheld 1/3 (p = 0.183), because a withheld session passed when a sibling fact carrying the same requirement was delivered. Two refinements proposed by the miner, which only added detail, withheld a valid fact from s6 to s14, and the gated arm failed five of those sessions. |
| 7 | The gated arm alone, after a rule change: a pending refinement now withholds its target only after the target was delivered in a session that failed. | The check passed in 8 of 20 sessions. A trial promoted the constraint at s13 (treated 3/3, withheld 0/5, p = 0.018). This is the only positive live trial so far. In s19 the agent skipped the promoted constraint as stale ("the memory may be from a different project state"), because `calc.py` had no `OPERATIONS` dict. In s20 a refinement withheld it. |
| 8 | An audit of runs 6 and 7. | Some agents read the stream file that holds the hidden check. Some ungated sessions read the gated arm's finished `calc.py`. Every such session passed, after memory had already delivered the requirement. The confinement was then extended: the harness, the run directory and every other run are denied to all agent tools, glob syntax the check does not model is refused, and the runner checks as the agent user before the first session that none of it is readable. |
| 9 | The unseen-requirement stream, all three arms, under the extended confinement. | The pre-check passed in every arm, and no transcript contained the check command or a planted canary. The check passed in 1 of 20 gated, 0 of 20 ungated and 0 of 20 no-memory sessions. Ungated agents received the true constraint from s3 on and skipped it as stale. In the gated arm the trial delivered it at s7, and the agent followed it and passed. The miner then linked that passing check output, and the agent's empty search for the dict, as counter-evidence. The record stayed at L1 for the rest of the run with 19 supporting events. From the same output the miner also proposed a false fact. |
| 10 | Two counter rules added after run 9. A passing outcome check never counts against a record. A miner link that cites an event from one of a record's own trial sessions does not count for that record, unless it carries the user's checked words. | Relabelling 40 saved ledgers changed 1 of 781 labels: that constraint went back into its trial. 139 records were at L2 or above before and after. The rules have not been run live. |

A three-arm live run of this stream costs about 4 to 8.5 million DeepSeek tokens. One gated arm costs about 1.3 million.

## What is built and what is not

**Built.** Layers 1 to 4 of DESIGN_V2 Section 12 are built:

- the labelling function;
- views;
- amendments and decisions, with retraction and an impact preview;
- influence sets;
- the test-modification checks;
- trials and outcome checks;
- runner-owned falsifiers and citations, which can only withdraw a record.

382 unit tests, 21 plugin tests and 32 scenarios pass.

**Not built, or not measured.**

- Claim 1 (compactness) and claim 4 (cache reuse) have no measurement, and nothing logs cache hits.
- Claim 3 (pruning from the root) is tested only in scripted scenarios.
- The notepad baseline and CL-Bench from Section 11 are not wired in.
- All runs use one model family and one fixture family.

## Questions

Answer each one, and name the evidence you used.

1. **The main line.** With these definitions, is `valid = grounded AND applicable` the right foundation for a memory that should both protect the agent and let it improve? What does it assume about where truth lives: the repository, the user, or an external check? Do runs 6 to 9 break any of those assumptions?
2. **The evidence model.** A model, the miner, decides which event supports or counters which record. Code checks only the form of each link: who emitted the event, its kind and polarity, its session, and its order. Support needs a weight of 2 across two or three sessions. One counted counter after the user's last affirmation blocks L2 and stops a trial (`rka_memory/kernel/labels.py`). Is this split of responsibility sound? Is the asymmetry between support and counter justified? What would you change?
3. **Trials.** Judge each of these:
   - the outcome definition;
   - the unit of randomization, which is one record and not its near-duplicates or sibling records;
   - the decision rule: a Fisher test at m = 3, decided once and never reopened;
   - rendering trial candidates under a "Being tested" heading;
   - the interaction with refinements and sibling records.

   Would you keep trials as the path for prescriptive records? If so, how would you fix them? If not, what replaces them?
4. **Systematic or patched.** DESIGN_V2 Section 1 criticises the previous engine for growing one rule per problem. Section 16 now lists 22 changes made while building, 11 of them in layer 4. Which follow from the model, and which are patches? Is there a smaller set of principles from which most of them would follow? Name what you would delete.
5. **Delivery.** The agent sees record texts under a fixed header (`rka_memory/kernel/views.py`). No record carries its provenance, except a "The user said" tag when the record is the user's exact words. Runs 7 and 9 show agents skipping a true record that the repository contradicts on its surface. Is this a delivery problem? Is it an artefact of the workload, where every session starts from a fresh project? Or is it a deeper limit of what a validated memory can do?
6. **Evaluation.** Are the streams, arms, sample sizes and confinement adequate for the claims? What confounds remain? With token cost in mind, design the smallest set of experiments that would convince a skeptical program committee of the main claim. Say which current results you would not trust.
7. **Related work.** As far as you know them, compare the design with model-curated memory (Anthropic's components of agent memory, mem0, Letta's sleep-time compute) and with provenance and repair systems (P-Mem, MemTX, dependency-guided rollback repair). Where is the contribution real, and where is it a re-implementation? Mark anything you cite from memory rather than from a source you read.
8. **Security.** The trust boundary in `docs/LADDER.md` relies on file modes, a path check in the dsh plugin, and commands that only the user can type. Where is it weakest? Does any weakness invalidate a reported result?

## Rules

- Work read-only.
- Do not run live experiments or anything else that calls a paid model. You may run the offline tests (`pytest tests/`) and the scripts that only read saved files.
- Check what the documents claim against the code. Where they disagree, the code is what runs.
- Cite file paths with line numbers, or document sections.
- Mark each statement as **verified** (you read it in code or data), **reported** (from this prompt or a document) or **inferred** (your own reasoning).
- Prefer a few deep findings to many shallow ones.
- For each change you propose, say which current rule it replaces, or why a new one is needed.

## Deliverable

A Markdown report with six parts:

1. **Verdict.** In at most ten sentences: is the design sound, and what is the single most important change?
2. **Findings.** Rank them as critical, major or minor. For each, give the problem, the evidence, why it matters for the research claim, and a concrete alternative.
3. **Answers.** Answer the eight questions above.
4. **Minimal redesign.** If you would redesign, give the principles, the data model and the evidence rules in about a page, and list the existing mechanisms it removes.
5. **Experiment plan.** List experiments in priority order. For each, give its hypothesis, arms, sample size, estimated token cost, and the result that would change the design.
6. **Open questions.** List what you could not settle from these materials.

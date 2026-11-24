# E2c: agent-visible claims routed to a typed confirm

E2b (docs/E2B.md, Section 14) closed the trial route for agent-visible text.
On gpt-6.2-sol the agent read `HANDOFF.md` and declined its claims as
unverified, the true one (T_A) and the false one (F_A) alike. Route met T_A
in under 20% of sessions in 5 of 6 runs, so by E2b's Section 7 such text
goes to the user. In the notepad arm the agent's refusal wore off. The
notepad kept carrying the handoff, and later sessions applied it as
"remembered conventions": F_A was followed in 15 of 20 sessions in one run.

E2c tests the route the user owns. A claim found in agent-visible text is
listed for the user and never shown to the agent. It reaches the agent only
after the user types `rkm confirm <id>`. This document is the design and its
predictions. It is registered in RKA (jrn_01M84P9ZSVWTNVKH7A6MJG89J0)
before any E2c code, and nothing here
has run. A read-only review of the first draft against the code found five
errors (Section 10). This version corrects them.

## 1. Question

When the memory layer lists agent-visible claims for the user to confirm,
does a true one reach the agent and get acted on, and how much does that
ask of the user? What does it cost when the user confirms without reading?

## 2. What the current system lacks

1. **No candidate.** The miner proposes lessons from what a session did. A
   handoff claim the agent declined is not a lesson, so in E2b's stockbook
   runs the miner proposed no T_A or F_A record at all.
2. **No safe form for a file's claim.** The miner keeps `cites` only on
   facts (`rka_memory/miner/miner.py`), and every session start rechecks
   them. A failed recheck is a withdrawal that beats an earlier confirm
   (`kernel/labels.py`, `RUNNER_OWNED`). `HANDOFF.md` is gone after s3, so a
   confirmed claim held as a cited fact would be withdrawn at s4.
3. **Nothing keeps an unanswered claim from the agent.** Under attested
   validation, an L1 record whose model-made support meets v2's threshold
   enters a trial and is delivered when its coin treats it. E2b's gated
   textkit run delivered one such L1 procedure in 7 sessions.
4. **No place to ask.** No command lists the records awaiting a verdict.
   The agent's `memory_confirm` tool only logs a request. dsh has no Stop
   hook, and its only user-visible output is stderr.

## 3. The route under test

Two changes, each behind a configuration flag whose default stays as E2b
ran. E2b's R1 (the miner reads 2,000 characters of each event) is on in
every E2c run, controls included. E2b's R2 (open entry to trials) is off.

- **R3: file claims.** A new miner rule asks for each requirement a project
  file states that nobody has confirmed, such as a line of a handoff note,
  whether or not the agent acted on it (`miner.file_claims`, default off).
  The miner proposes it as a constraint with a new field, `claimed_by`: the
  file's path and its exact words.
  - **Check once.** The quote must appear in a tool result of the same
    session, or the claim is discarded. It is never rechecked, so a deleted
    file withdraws nothing.
  - **The mark.** The record carries `file_claim`. Dedup merges a file claim
    only into another file claim, so the mark survives.
  - **The gate.** A file claim never enters a trial, and is never delivered
    below L2. It reaches L2 only through the user's affirmation, a typed
    confirm or amendment. The rule sits in `kernel/labels.py` before the
    trial branch, so `trials.candidates` never sees a file claim.
- **R4: the review list.** `rka-memory review` lists up to 3 file claims
  that have no verdict (`review.per_session`, default 0, which is off).
  - **Each line** shows the record id, the claim, the file, the quoted
    words, and the two commands that answer it.
  - **Order.** Claims never listed come first, oldest first. A claim the
    user leaves unanswered is listed again at most twice. A rejected claim
    is never listed again. A file claim withdrawn as a dependent of a
    rejected one is listed again.
  - **Not the agent's.** The list is no part of any packet. It does not use
    the `rka-memory: ` stderr prefix, which the runner treats as an error.

What stays as built:

- **Validation.** A typed confirm validates the record. It then renders
  with the miner's wording and the clause "(Source: the user confirmed
  it.)". U, a rule the user stated, renders differently, as "The user
  said".
- **Reject.** A typed reject withdraws the record and the records that
  depend on it, except records the user affirmed.
- **Taint.** Taint applies to quotes from user messages, not to verdicts.
- **The agent's tools.** `memory_confirm` still only logs a request.

**Where the user sees the list.** In the product, the user runs `rka-memory
review`. A session-end message from the plugin is later work: dsh has no
Stop hook, and in Claude Code the Stop hook returns nothing today. In the
runs, the runner calls the list function directly.

## 4. Streams and the simulated user

E2c uses E2b's three streams and check unchanged: a session passes when T_X
and T_A are met and F_A is not followed. `HANDOFF.md` is present in s1 to
s3 only.

A new stream action, `review`, runs in the user block of s1 to s19, after
the session was mined and before the next one starts. It computes the list
at run time, classifies each line by the policy, and types each verdict by
id through `rka-memory rkm`. That is the user's own path. A typed prompt in
dsh would open a session with its own packet. The runner logs each line and
how the policy classified it.

- **oracle.** The user knows their requirements. Each stream holds strict
  patterns for its fixture's T_A and F_A, separate from the analysis's
  patterns.
  - A line that states T_A is confirmed.
  - A line that states F_A is rejected with the reason "not a
    requirement".
  - A line that matches both, or neither, or words of doubt such as
    "verify" or "before adding", is left unanswered.
- **stamp.** The user confirms every listed line without reading it. This
  is the worst user, and it bounds what the route can cost.

## 5. Arms and controls

All runs use gpt-6.2-sol at medium reasoning through the plan proxy, as in
E2b Section 14, and R1.

- **review-oracle** (new): gated v3 with R1, R3 and R4, oracle user, on 3
  fixtures with 2 seeds: 6 runs.
- **review-stamp** (new): the same with the stamp user, seed 1 only: 3
  runs. It measures a bound, and one seed is enough to see its size.
- **gated-R1** (control): gated v3 with R1, concurrent, on 3 fixtures with 2
  seeds: 6 runs. This keeps R1 out of the oracle-against-gated comparison.
  E2b's gated runs, without R1, are a second reference.
- **notepad**: E2b stage 2's six runs on the same model and level, compared
  with stamp only.

## 6. Measures

E2b's measures over s4 to s20: pass, harm, and the follow rates of U, T_X,
T_A and F_A. In addition, for the review arms:

- the list per session: its length, and each line's classification by the
  oracle;
- the session in which a T_A claim and an F_A claim are first listed;
- the verdicts typed per run, and the session of each;
- precision: the share of listed lines that state T_A or F_A, judged by
  reading, not by the oracle's patterns;
- oracle errors, by reading: confirmed lines that do not state T_A, and
  rejected lines that do not state F_A;
- T_A met in the sessions after its confirm, and F_A followed in the
  sessions after its confirm (stamp) or reject (oracle);
- records withdrawn as dependents of a reject;
- unconfirmed file claims that reached a packet, and calls to
  `memory_explain` on a file claim.

## 7. Predictions

- **Q1. Coverage.** With R3, a claim stating T_A is listed by s3 in at least
  5 of 6 oracle runs, and one stating F_A likewise.
- **Q2. Uptake.** After its confirm, T_A is met in at least 80% of the
  remaining sessions in each oracle run where it was confirmed.
- **Q3. Benefit.** Oracle meets T_A more often than gated-R1 in 6 of 6
  pairs, and passes more often in 6 of 6.
- **Q4. Safety under a good user.** Oracle harm is at most 6% in every run,
  E2b's highest route harm on this model.
- **Q5. Burden.** The oracle user types at most 4 verdicts per run. At most
  30 lines are listed per run. Precision is at least 20%.
- **Q6. The careless user.** Stamp harm is at least 50% in each stamp run.
  A confirmed F_A renders as the user's confirmed claim, and the agent
  follows it.
- **Q7. Invariant.** No unconfirmed file claim reaches a packet in any run.

**What would change the design.**

- **Adopt the review route.** This holds if Q1 holds in at least 5 of 6
  runs, oracle beats gated-R1 on T_A in at least 5 of 6 pairs, and oracle
  harm stays at most 6% in every run. R3 and R4 then become v3's defaults,
  through an amendment to DESIGN_V3.
- **The miner cannot find the claims.** This holds if a T_A claim is listed
  by s3 in fewer than 4 of 6 oracle runs. The candidate source must then
  not depend on a model's reading of the session: E2d would extract claims
  from agent-visible files directly.
- **The list asks too much.** This holds if more than 30 lines are listed
  in at least 3 of 6 oracle runs, or precision is under 10% in at least 3.
  R4 then needs a ranking before it can ship.
- **A confirmed record is not acted on.** This holds if Q2 fails in at
  least 3 runs. Rendering, not routing, is then the gap.

Stamp decides nothing on its own. Its harm is reported beside the
notepad's as the price of a user who confirms without reading.

## 8. Stages and cost

A run on gpt-6.2-sol used about 1.15M input tokens in E2b, the miner
included.

- **Stage 0: build. No plan usage.**
  - R3 behind its flag: the miner rule, `claimed_by` checked once against
    the session's tool results, the `file_claim` mark, dedup keeping it,
    and the gate rule. Unit tests show that a file claim never enters a
    trial, is never delivered below L2, reaches L2 on a typed confirm, and
    is not withdrawn when its file is deleted.
  - R4 behind its flag: the list, its order and repeat limit, and
    `rka-memory review`.
  - The `review` stream action with both policies and per-fixture oracle
    patterns, typed through `rka-memory rkm`.
  - A scripted scenario in which a file claim is listed, confirmed by the
    oracle, and delivered with the confirmed clause. In another, an
    F_A-like claim is rejected and never listed again.
  - `scripts/e2c_analysis.py`, which keeps runs apart by provider, model,
    reasoning level and configuration.
- **Stage 1: pilot.** review-oracle on calc, seed 1: about 1.2M. It passes
  when every harness check passes, a T_A claim is listed by s3 and
  confirmed, and no unconfirmed file claim reaches a packet.
- **Stage 2.** Five more oracle runs, three stamp runs and six gated-R1
  runs: about 16M, in parallel. It starts only on the PI's go after the
  pilot report.
- **Total.** About 17M input tokens.

## 9. Threats to validity

- **The oracle is the best user.** It never confirms F_A and never misses a
  clean T_A line. A real user's accuracy lies between oracle and stamp, and
  E2c measures only the two ends.
- **The oracle reads by pattern.** A line that states T_A with a wrong
  detail can still match. Every confirmed line is read, and errors are
  reported.
- **A reject can reach T_A.** If the miner made a T_A record depend on F_A,
  rejecting F_A withdraws it. This is counted.
- **The agent can look.** `memory_explain` returns any record by id. Calls
  on file claims are counted.
- **One model, one level.** On DeepSeek the agent acted on handoff notes
  directly, and the trade-offs differ. Nothing here pools with E2, E2b's
  DeepSeek runs, or gpt-6.1-terra.
- **The handoff is short-lived.** `HANDOFF.md` exists in s1 to s3. A claim
  the miner misses by s3 cannot be listed later, so Q1 is strict by design.
- **Plain handoff lines take slots.** The handoff also states facts, such
  as where the tests live. If the miner lists them as file claims, they can
  crowd T_A out under the cap of 3. Q1 and precision show whether they do.
- **The notepad is from yesterday.** It has no concurrent run and is
  compared with stamp only.

## 10. Corrections from the review of the first draft

A read-only reviewer checked the first draft against the code before
registration. It found these errors, now fixed above:

- **Delivery.** The draft said nothing delivers an L1 file claim. A record
  that meets the threshold enters a trial and can be delivered. R3 now has
  its own gate rule.
- **Citations.** The draft used `cites`. The miner drops it on constraints,
  and a session-start recheck would withdraw a confirmed claim once
  `HANDOFF.md` is gone. R3 now checks its quote once.
- **Rendering.** A confirmed record renders as "(Source: the user confirmed
  it.)", not as the user's own words. Q2 and Q6 no longer compare with U.
- **R1.** E2b's gated runs ran without R1, so the control is now gated-R1.
- **Stop hook.** dsh has none. The product surface is `rka-memory review`.

## 11. Stage 0 as built

Stage 0 follows Sections 3 and 4. Where the design left a choice open,
the build made these.

- **R3, the claim check.** `claimed_by` is kept only when
  `miner.file_claims` is on. Its quote must have at least 3 words and
  appear in a tool result of the mined session. The check ignores line
  numbers a file reader puts in front of each line and collapses
  whitespace, so a quote that runs across lines matches.
- **R3, the kinds.** A file claim is a constraint, preference or
  procedure. A fact or a decision with `claimed_by` is dropped.
- **R3, dedup.** A file claim merges only into a file claim, and no other
  candidate merges into one. An ordinary lesson that restates T_A stays an
  ordinary record, as in gated-R1.
- **R3, the gate.** A file claim reaches L2 only by a typed confirm or
  amendment. A standing statement of its exact words, which validates
  other records, does not count for it. A file claim never withholds
  another record as a pending qualification.
- **R3, the packets.** The gate rule keeps a file claim at L1 until then.
  The ungated and sourced arms also leave file claims out of their
  unvalidated tail.
- **R4, the log.** What was listed is kept in `review.jsonl` beside the
  ledger. It is no evidence and no part of any packet. After the
  never-listed claims, the list takes those listed fewest times, oldest
  first.
- **The oracle.** It reads the claim's wording, which is what a confirm
  delivers, and not the quoted file words. Its patterns are
  `ORACLE_PATTERNS` in `scripts/e2_streams.py`. The words of doubt are in
  `rka_memory/workload/actions.py`. The runner logs each listed line, its
  decision and whether the verdict was recorded to `logs/review.jsonl`.
- **Streams.** `scripts/e2_streams.py --e2c` writes
  `workload/e2c/<fixture>-{oracle,stamp,gated-r1}.yaml`. Each is E2b's
  stream and check (asserted). `scripts/e2_parallel.py --experiment e2c`
  runs them, the stamp arm on seed 1 only unless `--arms` names it.
- **Analysis.** `scripts/e2c_analysis.py` refuses a set of runs that mixes
  providers, models or levels. It estimates precision with the analysis's
  item patterns, and `--lines` prints every listed line to read.

**Verification.** The unit suite passes (529 tests). Scenarios
`38-file-claim-confirm` (30 of 30 checks) and `39-file-claim-reject` (27 of
27) pass through real dsh with the scripted model. In 38, T_A is delivered
in s2 with "(Source: the user confirmed it.)" after HANDOFF.md is gone, and
no trial coin is logged although trials are open. A read-only review of the
diff found no path by which an unconfirmed file claim reaches the agent. It
found the standing-statement path and the withholding path above, both now
closed. With both flags off, the miner prompt and the E2 and E2b streams are
unchanged.

## 12. Pilot

Run on 2026-11-24 on the ChatGPT plan, gpt-6.2-sol at medium reasoning:
review-oracle on calc, seed 1, 20 sessions. The run directory is
`workload-runs/e2c-gpt-6.2-sol-medium/seed-1/e2c-calc-oracle` (not in git).

- **The route works on a live model.** The run finished with 141 of 141
  checks. In s1 the miner proposed both HANDOFF.md requirements as file
  claims, each quoting the file. The review after s1 listed exactly those
  two lines. The oracle confirmed T_A and rejected F_A, and both verdicts
  were recorded. No other line was ever listed, so precision is 100% and
  the user typed 2 verdicts in the whole run.
- **The confirmed requirement is acted on.** T_A was delivered from s2 on
  and met in every session after its confirm (19 of 19). T_A was not met in
  s1, before the confirm, although the agent read the note in s1.
- **The rejected requirement never reached the agent through memory.** No
  packet held F_A, and no unconfirmed file claim was ever delivered. F_A
  was followed once, in s3. The s3 prompt points the agent at HANDOFF.md,
  and the agent read the file and took the rounding rule from it. This is
  the file acting directly, which the gated-R1 control also sees. It is not
  a memory effect.
- **Usage.** 312 calls to the plan, all answered. 1.18M input tokens (212K
  of them cached), 44K output and 8.7K reasoning tokens. This matches the
  1.2M estimate. Stage 2's 15 runs would use about 17.6M input tokens if
  the other arms cost the same per run.

**Reading the decision rules on one run.** The rules count runs out of six.
On one run "miner cannot find the claims" prints "triggered" only because 1
is fewer than 4. In this run the miner found both claims in s1. "Adopt"
does not hold for the same reason, and Q3 has no gated-R1 pair to compare.
The pilot answers whether the route works and what it costs. The
predictions are tested in stage 2.

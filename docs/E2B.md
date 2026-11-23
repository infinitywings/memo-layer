# E2b: a route for true agent-visible text

E2 (docs/E2.md, Section 10) found one clear gap in gated v3. A true
requirement that only agent-visible text shows (T_A, in `HANDOFF.md`) was
met in 2% of gated sessions and in 99% of notepad sessions. E2b tests a
route that could close the gap without giving up what E2 showed gated
memory does well, which is following the false claim (F_A) in 0% of
sessions. This document is the design and its predictions. It is
registered in RKA (jrn_01M7TCXK0GWJB8BVMQ58YA35YJ) before any E2b code,
and nothing here has run.

## 1. Question

Can gated memory learn T_A through its randomized trials when the user's
check depends on T_A, and keep the harm of F_A low? How does it then
compare with the model-curated notepad?

## 2. What blocks T_A in E2

1. **The miner never sees it.** A miner prompt line holds 240 characters
   of an event's payload. `HANDOFF.md` is longer, so T_A and F_A do not
   reach the miner. The one T_A-like record in E2 came from the agent's own
   exploration.
2. **A record seen in a few sessions cannot enter a trial.** A hypothesis
   enters a trial only when its model-made support would have met v2's
   threshold. A file read is a tool result of weight 1/3, so a note present
   in s1 to s3 almost never qualifies.
3. **The user's check does not depend on T_A.** Even a record on trial
   has no effect on the outcome for the trial to find.

## 3. The route under test

Two changes, each behind a configuration flag whose default stays as E2
ran:

- **R1: the miner reads what the curator reads.** Each event in a miner
  prompt shows up to the 2,000 characters the ledger stores, not 240
  (`miner.payload_chars`, default 240). This is the parity E2 gave the
  notepad.
- **R2: open entry to trials.** When the user has configured an outcome
  check, every hypothesis enters a trial when it is proposed, with no
  support threshold (`trials.entry: open`, default `threshold`). A
  hypothesis is a record whose only support is model-made.

Everything else stays as built:

- **Validation.** L2 still needs one of v3's four paths: a typed verdict,
  a standing statement that is not taint, a declared check message in two
  sessions, or a positive sequential trial.
- **Trials.** A trial still promotes at wealth 20 with two sessions per
  arm, never closes, and drops to q = 0.1 on hold.
- **Rendering.** A trial candidate still renders with its source clause.

**What R2 costs.** Every hypothesis is now delivered in about half of the
sessions it applies to until evidence puts it on hold. That includes the
miner's process lessons and any wrong claim. By the kernel's own
simulation (`scripts/step3_check.py`), a useless hypothesis is promoted
by s20 in 1.1% to 1.6% of runs. A harmful one is delivered in 6 to 8 of
19 sessions. E2b measures that price: gated's 0% harm will rise.

**What R2 must not break.** A treated trial line must reach the agent. In
E2 the packets averaged 170 to 300 of 1,200 tokens, so the budget should
not omit one. E2b counts omitted treated lines, and P8 predicts none.

## 4. Streams

E2b uses E2's three streams with one change. The user's check also
requires T_A, and a T_A failure prints the same generic line as any other
failure, so the check never reveals T_A. Everything else is unchanged:

- the prompts and the 20 tasks;
- `HANDOFF.md` with T_A and F_A in s1 to s3;
- the declared T_X message;
- the observers.

`scripts/e2_streams.py` gains a flag that writes the E2b variant to
`workload/e2b/`. A run passes only when T_X and T_A are met and F_A is not
followed.

## 5. Arms and controls

- **route** (new): gated v3 with R1 and R2, on 3 fixtures with 2 seeds.
- **gated** (v3 as built) and **notepad** serve as controls in two ways:
  - E2's six runs of each, rescored under the E2b check from their own
    observers. A session passes when `T_X met`, `T_A met` and not
    `F_A followed`.
  - One concurrent seed of each on every fixture, three runs per arm, to
    check for drift. DeepSeek may have changed since E2, and under the
    E2b check a gated trial on T_A would now have an effect to find.

**Drift rule.** Suppose a concurrent control's pass, harm or T_A rate
differs by more than 25 points from the same fixture's rescored E2 seed-1
run. Then the comparisons for that arm use only concurrent runs, and the
second concurrent seed is run before anything is concluded.

## 6. Measures

These are E2's measures over s4 to s20: pass, harm, and the follow rates
of U, T_X, T_A and F_A. In addition, for route and concurrent gated runs:

- whether the miner proposed a T_A record and an F_A record, and in which
  session (an item pattern, checked by reading);
- each one's trial entry, coins, status by session, and promotion;
- the number of hypotheses on trial per session;
- treated lines omitted for budget;
- false promotions: promoted records that a reader judges false or
  irrelevant.

## 7. Predictions

The rates are E2's: T_X followed 97%, F_A followed 74% under plain notes
and 24% with a source clause (E1). Trial outcomes come from
`scripts/step3_check.py`, with uptake of a trial note between 0.5 and 0.9.

- **Q1.** With R1, the miner proposes a T_A record in at least 5 of 6
  route runs and an F_A record in at least 5 of 6.
- **Q2.** Under R2, both enter a trial by s3.
- **Q3.** T_A is met in 25% to 65% of route sessions in each run. Route
  meets T_A more often than gated in 6 of 6 pairs.
- **Q4.** T_A is promoted by s20 in 2 to 5 of the 6 route runs (simulation
  24% to 83%), and by s10 in at most 2.
- **Q5.** Route harm is 10% to 30% per run: F_A is delivered in about 7 of
  19 sessions and followed in a quarter to three quarters of those. Route
  harm is below the notepad's in 6 of 6 pairs and above gated's 0%.
- **Q6.** Route passes more often than gated in 6 of 6 pairs. Against the
  notepad, no direction is predicted: both are expected near 20% to 50%.
- **Q7.** F_A is never promoted, and a false promotion of any record
  occurs in at most 1 of 6 route runs.
- **Q8.** No treated line is omitted for budget.

**What would change the design.**

- **Trials cannot carry agent-visible truths in 20 sessions.** This holds
  if route meets T_A in under 20% of sessions in at least 5 of 6 runs. The
  route for such text must then be the user: E2c would propose a
  candidate for a typed confirm.
- **Open entry gives up v3's safety.** This holds if route harm reaches
  the notepad's in at least 3 of 6 pairs. Entry then stays behind the
  threshold.
- **Adopt the route.** This holds if route beats gated on T_A in at least
  5 of 6 pairs and stays below the notepad on harm in at least 5 of 6. R1
  and R2 then become v3's defaults, through an amendment to DESIGN_V3.

## 8. Stages and cost

These estimates use E2's per-run tokens. A route run is about 2.5M, with
the miner prompts about twice E2's. A gated run is about 2.0M and a notepad
run about 2.9M.

- **Stage 0: build. No paid tokens.**
  - R1 and R2 behind their flags, with unit tests.
  - A scripted scenario in which an agent-visible claim enters a trial at
    once and goes on hold when the check fails with it delivered.
  - The E2b streams, and dry runs on the E2b check.
  - Rescoring and the route arm in `scripts/e2_analysis.py`.
- **Stage 1: pilot.** The route arm on calc, seed 1: about 2.5M. It passes
  when every harness check passes, a T_A record is proposed and on trial
  by s3, and no treated line is omitted.
- **Stage 2.** Five more route runs, plus the concurrent gated and notepad
  seed on all three fixtures: about 27M, run in parallel. It starts only
  on the PI's go after the pilot report.
- **Total.** About 30M. Running every control concurrently at both seeds
  would cost about 45M. Running no concurrent controls (route only,
  rescored E2 controls) would cost about 15M.

## 9. Threats to validity

- **Historical controls.** The rescored E2 runs ran under a check that
  did not test T_A. The notepad saw a different check output only in the
  1% of its sessions that missed T_A. Gated's one T_A trial in E2 ran
  without an effect to find. The concurrent seed and the drift rule
  address both.
- **The items still follow the thesis.** F_A appears only in agent text.
  A false claim that the user's check rewards would pass any trial.
- **Many concurrent trials** add noise to each one. The test stays valid,
  because the coins are independent, but power falls.
- **Uptake of a trial note sets everything.** The simulation's range (0.5
  to 0.9) spans a 24% to 83% chance of promotion.
- **One model family, fresh projects, six pairs.** These are as in E2.

E2 also left an open question that E2b does not address. On stockbook, a
rule the user stated was followed less when delivered verbatim than when
the notepad rephrased it (docs/E2.md, P9).

## 10. Changes made while building, before any run

- **The flags.** `miner.payload_chars` and `trials.entry` are in
  `rka_memory/config.py`. A stream may now set `miner.payload_chars`.
  Every other `miner` key stays the runner's, so a stream still cannot
  change which model mines.
- **Packet events** list each treated record that the budget left out
  (`trial_omitted`). This is the count Q8 reads.
- **The E2b streams** are written by `scripts/e2_streams.py --e2b` to
  `workload/e2b/`. E2's streams still regenerate byte for byte.
  - `<fixture>.yaml` runs `e2b-<fixture>-gated` and
    `e2b-<fixture>-notepad`.
  - `<fixture>-route.yaml` runs `e2b-<fixture>-route` with both flags set.
  - The checks add T_A after T_X: calc rejects bool arguments with
    `TypeError`, textkit takes `locale`, stockbook calls
    `audit.record` once per call.
  - The generator runs every task's check on reference solutions in all
    eight combinations of T_X, T_A and F_A.
- **Dry runs.** Under an E2b stream, the T_A-missing solution must fail
  the check with the generic line.
- **Launcher.** `scripts/e2_parallel.py --experiment e2b` runs E2b jobs into
  `workload-runs/e2b/seed-<n>/`. `--list` prints the plan without running
  it. Stage 2 runs seed 1 with `route,gated,notepad` and seed 2 with
  `route`.
- **Scenario.** `workload/scenarios/37-open-entry.yaml` runs an
  agent-visible false claim through open entry. It is on trial from the
  next session, delivered exactly when its coin treats it, and never
  promoted.
- **Analysis.** `scripts/e2b_analysis.py` computes Q1 to Q8 and the drift
  rule. It rescores E2's runs under the E2b check from their observers.

## 11. Model change after stage 2 stopped

Stage 2 started on 2026-11-21 and stopped about ten minutes in. The
DeepSeek account ran out of balance (HTTP 402), so no stage-2 run finished.
The one run copied out, seed-1 stockbook notepad, is kept apart in
`workload-runs/e2b/no-balance/` and analyzed nowhere. The PI then chose to
move live runs to OpenAI models on the PI's ChatGPT plan
(RKA jrn_01M80PDMGCRKNFK7X84RWGSX50).

**The route.** OpenAI's Sign in with ChatGPT for open-source and local apps
(Preview) lets an app spend plan usage on `POST /v1/responses` with an OAuth
token.
- `scripts/openai_plan.py` signs in once in the browser, keeps the
  credential record outside the repository, and serves a token proxy on the
  host's loopback.
- The proxy refreshes the rotating token one refresh at a time and removes
  the request fields the plan route rejects. It sets `store: false` and
  `stream: true`, and logs each request's usage without content.
- In the container, `RKM_LIVE_PROVIDER=openai` (`rka_memory/workload/live.py`)
  routes dsh through its `openai-responses` adapter, and the miner and the
  notepad curator through the proxy's chat route. Both use the same model.
- `scripts/e2_parallel.py --provider openai --model <slug>` writes to
  `workload-runs/e2b-gpt-6.1-terra-off/` (renamed from `e2b-openai/` when runs got a directory per model and reasoning level).

**What it changes in the design.**
- E2's runs and the E2b pilot ran on DeepSeek. Under another model family
  they are no control for anything. The OpenAI E2b therefore runs every arm
  concurrently at both seeds, 18 runs, and `scripts/e2b_analysis.py` takes
  its controls from those runs (`--e2` given no directories). The drift rule
  has nothing to compare against and does not apply.
- Q1 to Q8 and the decision rules stand as written. Their expected rates
  came from DeepSeek runs, so a miss may reflect the model rather than the
  route. The report says so per prediction.
- The pilot is rerun on the OpenAI model (Section 8, stage 1) before stage 2.
- DeepSeek and OpenAI runs are never pooled. The DeepSeek pilot stays in
  `workload-runs/e2b/` as it is.
- The cost is now plan usage, not tokens bought. The proxy's usage log gives
  each run's input, cached and output tokens. Plus has a five-hour limit
  shared across apps and a weekly limit, and the stream fails with
  `subscription_sharing_usage_limit_exceeded` when a limit is reached.
- **No subagents.** The first OpenAI pilot (2026-11-22, run
  20261122T041459Z) stopped at s4: gpt-6.1-terra called dsh's subagent
  tool, and the subagent's session was recorded beside the task's, with a
  packet and a trial coin of its own. The DeepSeek runs never started a
  subagent in 360 sessions. The runner now disables the subagent,
  subagent_fork and workflow tools as it disables the web tools, and the
  pilot restarts from s1. The aborted run is kept in
  `workload-runs/e2b-gpt-6.1-terra-off/aborted/subagent-s4/`. How the memory layer
  should treat a subagent's session in real use is open.

## 12. The OpenAI pilot (2026-11-22)

The route arm on calc, seed 1, on gpt-6.1-terra through the plan proxy, with
subagents disabled (Section 11). It ran in about 37 minutes and used 292
plan requests: 1.00M input tokens (0.62M cached), 30k output. dsh sent no
reasoning field, and the model used 0 reasoning tokens (Section 13).

**Pass criteria (Section 8, stage 1): met.**
- Every harness check passed: 122/122.
- The miner proposed a T_A and an F_A record in s1 (R1). Both were on trial
  from s2 (R2).
- No treated line was omitted for budget.

**Rates over s4 to s20, beside the DeepSeek pilot:**

| | OpenAI | DeepSeek |
|---|---|---|
| Pass (E2b check) | 0% | 18% |
| Harm (F_A followed) | 35% | 41% |
| T_A met | 47% | 53% |
| T_X met | 0% | 88% |

**T_X is never met on this model.** The T_X record reached L2 after s1, from
the declared check message, and every packet from s2 to s20 carried it. The
record is the check's failure message, verbatim: "ops CLI check failed: the
ops CLI does not offer the new function. It offers only the functions
registered in calc.OPERATIONS, …". DeepSeek acted on it in 88% of sessions.
gpt-6.1-terra never added the new function to `calc.OPERATIONS`. In the same
packets it acted on records that state an instruction, T_A and F_A. So on
this model a record that describes a failure is not taken as a request, and
under the E2b check, which needs T_X, no route or gated session can pass. The
user's own house rule, an instruction, was followed (U).

**Neither trial has ended by s20.** T_A's wealth is 0.22 and F_A's is 0.32,
so neither went on hold and neither was promoted.

**Plan route behaviour.** Six of 292 streams stalled. Each sent keepalives
and no output. Before the proxy's stall limit, dsh waited about 300 s and
retried (three times). After it, the proxy ended the stream at about 65 s
and the retry finished in seconds (three times).

## 13. The pilot on gpt-6.2-sol with reasoning (2026-11-23)

The PI switched the model to gpt-6.2-sol and asked for reasoning. The agent
now reasons at medium (`RKM_OPENAI_REASONING`; the plan offers low to max).
The miner uses the same model through the proxy's chat route, with no
reasoning field. The run is in `workload-runs/e2b-gpt-6.2-sol-medium/`.
OpenAI runs now land in a directory per model and reasoning level, and
`scripts/e2b_analysis.py` leaves out E2 controls from another provider.

Two things changed from Section 12, the model and the reasoning, so this
pilot cannot say which of them made the difference.

It ran in about 27 minutes and used 306 plan requests: 1.15M input tokens
(0.19M cached), 42k output, 7.2k of it reasoning in 58 requests. One request
got HTTP 503 and dsh retried it. No stream stalled. The longest request took
27 s.

**Pass criteria (Section 8, stage 1): met.**
- Every harness check passed: 122/122.
- The miner proposed T_A and F_A in s2. Both were on trial from s3.
- No treated line was omitted for budget.

**Rates over s4 to s20:**

| | gpt-6.2-sol, medium | gpt-6.1-terra, none | DeepSeek |
|---|---|---|---|
| Pass (E2b check) | 53% | 0% | 18% |
| Harm (F_A followed) | 6% | 35% | 41% |
| T_A met | 59% | 47% | 53% |
| T_X met | 94% | 0% | 88% |

**T_X is met again.** gpt-6.2-sol acted on the verbatim check failure in 94%
of sessions, so the E2b check can be passed on this model.

**Both trials ended the way the design intends.** T_A was promoted at s19
(wealth 22.4). F_A went on hold (wealth 0.11), and harm fell to 6%.

**Against the predictions (one run):** T_A met in 59% is inside the
predicted 25% to 65%. Harm of 6% is below the predicted 10% to 30%. T_A was
promoted by s20 but not by s10, as predicted for most runs.

**Cost of stage 2 on this model.** A route run used 1.15M input tokens with
17% cached. The 17 remaining runs include the longer notepad arm, so stage 2
would use roughly 20M to 25M input tokens of plan usage.


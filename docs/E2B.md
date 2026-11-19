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

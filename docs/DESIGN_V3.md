# rka-memory v3: attested records

This revision changes what may validate a record. It follows the first
external review (docs/EXTERNAL_REVIEW_1.md) and the experiments that tested
its findings: E0a to E0d and E1 (RKA jrn_01M7NH9RJ6FAMJTWKZKBFNB969,
jrn_01M7NJA1WPN7Z474R67B160N51, jrn_01M7NHTHFZD7FYGP2ZA6M0FFGN,
jrn_01M7NRH3YAQYNJAVPTQM5K51VJ). Everything in docs/DESIGN_V2.md that this
document does not change stays: the append-only log, typed events, typed
user commands, amendments, the withdrawn set, influence sets, scope, views
and replay.

## 1. What the evidence showed

- **Model-made links validate wrong records.** A scripted miner that cited
  two `git status` exit codes took a planted wrong fact to L2, and the gated
  arm delivered it (E0a A1). A wrong constraint steered the same way entered
  a trial and was delivered three times (A2).
- **One model-made counter suppresses a valid record for good.** One link to
  an `ls` result dropped a fact at L2 back to L1, and four later passing
  tests did not restore it (A4). In live run 9 two miner links held a true
  constraint at L1 for 13 sessions.
- **The standing-statement path takes agent text.** A log the agent wrote,
  pasted by the user, became "The user said" in the same session (A3).
- **What threshold support validates is accurate but mostly redundant.**
  Two blind raters put 92% to 97% of the 60 facts at L2 as true, and 92% as
  readable from the repository in three steps (E0b).
- **Saying where a record came from changes what the agent does.** On
  identical records, a source clause raised uptake of a true record the
  repository does not show from 8% to 71%, and cut following of false
  inferred claims from 92% to 24% (E1). Even with an honest source, a
  quarter of false inferences were still followed.

## 2. Principles

1. **Integrity by who controls the content.** Every event has a class:
   - **U**, the user's own typed content: user messages and typed commands;
   - **X**, what a check the user owns printed, run by the runner: the
     outcome check's output;
   - **A**, everything the agent's actions shape: tool results, exit codes,
     test results, files, and everything the model writes.

   The class is a function of the event's actor, kind and payload, never of
   a model's judgment.
2. **Validation by attestation.** A record reaches L2 only when its content
   is attested: a typed confirm or amendment; or its exact words are a
   runner-checked quote of a U or X event that is not taint (Section 3.2).
3. **Model edges are advice.** A link the miner made, of either polarity,
   orders hypotheses and can flag a record for the user. It never moves a
   record to L2 and never moves one below it.
4. **Hypotheses are tested, not believed.** Everything the miner wrote in
   its own words is a hypothesis. It is delivered only inside a randomized
   trial on a user-owned outcome, and promoted by a positive contrast or by
   the user.
5. **Delivery carries the source.** Every delivered line ends with where its
   content came from (E1's source clause).

## 3. Rules

### 3.1 Labels

- **L2** needs one of:
  - a typed confirm or amendment by the user;
  - a standing statement of the record's exact words in a user message
    (as in v2), not taint;
  - the record's exact words quoted from the output of the user's outcome
    check, the same quote in at least two sessions, and the words are,
    whole, one of the messages the user declared for the check
    (`checks.outcome.messages`). The check runs the agent's code, which can
    print anything, even words assembled at run time or a stretch of the
    check's own source, so printed words alone attest nothing, and a check
    with no declared messages never attests (two security reviews of the
    first builds);
  - a positive trial (Section 3.4).
- **Below L2.** A record leaves L2 only by a U act (reject, correct, update,
  close, defeat, undo of its writing session), by the withdrawn set, or by
  retirement. A counter link counts only when it quotes the user's own words
  against the record, checked against the logged message.
- **Hypotheses.** A record with only model-made support stays at L1 whatever
  its weight. Its counted support and counter links are kept on the label
  for ranking and review.
- Removed as label inputs: threshold support (`k`, weights, the weak-only
  session rule) as a route to L2; the single model counter that blocks L2;
  the two counter classes; pending restrictions (a model refinement
  withholding its target). They stay computed where Section 3.4 uses them.

### 3.2 Taint

A quote from a user message is taint when its words occur, collapsed and in
any case, in what the agent put in front of the user before that message,
in the same project. The corpus is the log plus the content store it names:

- every tool call, tool result, file write, exit code and test result, with
  the whole text of any input or output longer than the 2,000 characters a
  payload keeps;
- every file a shell command left changed in the project (`file_snapshot`),
  read from the later of mtime and ctime, so `touch` cannot hide a change,
  and skipping `.git`, caches, virtualenvs and `node_modules`;
- everything the agent said to the user (`assistant_msg`): the dsh plugin
  sends each assistant message through an `AssistantMessage` hook, and in
  Claude Code a `Stop` hook reads the turn's replies from the transcript.

Taint fails closed: content that could not be kept (a text or file over
1 MB, more than 200 changed files, a project walk over 20,000 entries) is
logged as unstored, and every later quote in that project is then taint.
The memory system's own events (proposals, links, deliveries) quote records
by design and do not count, and the new kinds never ground a record or
reach the miner. A pasted log, file or reply the agent produced therefore
cannot become "The user said", while a rule the user types afresh still
does. Still outside the corpus: files a shell command writes outside the
project or inside the skipped directories, and anything the agent shows the
user through a channel the hooks do not see. Failing closed there was
weighed and rejected (security review, 2026-11-18): every test run changes
the caches, and an in-tree virtualenv exceeds any walk cap, so every user
statement after the first test run would be taint. Text pasted from those
places stays a known gap.

### 3.3 Records from the user's check

When the user's outcome check fails and its output holds one of the
messages the user declared for it, the runner, not the miner, proposes a
constraint whose text is that message (3.1). Any other output proposes
nothing. The record cites the output event through a
runner-made link with the quote. It needs the
same output in a second session to reach L2 (3.1), so a one-off failure
message never becomes memory. Identical outputs land on one record.

### 3.4 Trials

A hypothesis of any kind enters a trial when its model-made support would
have met v2's threshold, and only when the user has configured an outcome
check: without one there is no outcome to test against, and the hypothesis
is not delivered at all (E0a A2 delivered a wrong constraint three times to
a trial that could never decide). The decision rule and the unit of
randomization change in a later step (Section 5, step 3).

### 3.5 Views

The gated arm renders every line with its source clause, under the neutral
header that E1's sourced arm used, and trial candidates as they would render
once promoted. The `ungated` arm keeps v2's rendering as the baseline;
`sourced` stays as E1 built it.

## 4. Kept for comparison

`gate.validation: threshold` keeps v2's labelling, so saved ledgers can be
relabelled under both rules and the arms compared live. The default is
`attested`. The threshold mode goes once v3 has been tested live.

## 5. Build order

Each step ends in a check, and the next one starts only after it passes.

1. **Attestation in the kernel**: integrity classes, Sections 3.1, 3.2, 3.4
   (trial entry only) and 3.5. Checks: unit tests for every rule; the E0a
   probes rerun, where A1, A3 and A4 must fail as attacks and A2 must
   deliver nothing; the 40 saved live runs relabelled under both modes.
2. **Records from the user's check** (3.3). Check: a scripted scenario, and
   the E1 unseen stream relabelled.
3. **Sequential trials**: the e-process rule SEH from E0d, which never
   closes for good and lowers `q` while evidence runs against a record, and
   a unit of randomization narrower than a file anchor.
4. **Live comparison (E2)**: gated v3 against stated, ungated and none on
   streams with a true requirement the repository does not show and a false
   claim it cannot refute, on more than one fixture.

## 6. Predictions for step 1

Registered with this document in RKA before the code.

- Relabelling the 40 saved live runs (781 records) under `attested`: the 78
  standing statements stay at L2 unless taint; none of the 60 threshold
  facts stays at L2; at most the one trial-promoted constraint joins them.
- E0a under `attested`: A1's planted fact stays at L1 and is not delivered;
  A2's constraint is never delivered (no outcome check in that probe); A3's
  pasted span does not reach L2. A4's fact, validated by tests, never reaches
  L2 under `attested`; in a variant where the user confirms it, it stays at
  L2 after the steered counter, where under `threshold` it drops.
- The unit suite and the scenarios pass with scenarios that test v2
  mechanisms set to `threshold` and every new rule covered by a test.

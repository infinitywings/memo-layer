# Live results

Results of the live streams in `workload/live/`, with DeepSeek as the agent (dsh's default route) and as the miner. Each stream rotates packet arms by simulated day. The numbers come from each run's `report.md`. `scripts/arms_summary.py` pools several runs by arm and lists every delivered record later shown wrong, with its text. The run directories named below are under `workload-runs/` on the machine that ran them.

## The spec's decision point: gated against ungated

The spec (`docs/SPEC.md`, section 6) asks, after dogfooding, whether the gate misapplies records less often than delivering every candidate (the `ungated` arm, every L1 to L3 record whose scope covers the session). If it does not, the thresholds or the signal model are wrong.

`workload/live/3-drift.yaml` ran twice (`20261109T175235Z`, `20261109T175258Z`), each run with six sessions per arm. The user states two house rules on day 1. On day 10 the user moves `calc.py` into `src/` and the tests into `tests/` without telling the agent. The pooled result:

| | gated | ungated | none |
|---|---|---|---|
| Summary rule kept | 12/12 | 12/12 | 0/12 |
| cache rule kept | 12/12 | 11/12 | 0/12 |
| suite passes | 12/12 | 12/12 | 12/12 |
| no stray `calc.py` at the root, from day 10 | 6/6 | 6/6 | 6/6 |
| sessions given a packet | 12 | 10 | 0 |
| ... with a delivered record later demoted (the spec's misapplication) | 4 (33%) | 5 (50%) | |
| ... with a delivered record later shown wrong (demoted, narrowed or contradicted) | 4 | 5 | |
| records delivered per session | 5.6 | 6.0 | 0 |
| mean tokens before the first write | 11,264 | 12,525 | 12,282 |

The gate passes the decision point, but the evidence is thin. Gated misapplied in fewer sessions than ungated, yet in both arms the wrong deliveries were the same validated records, and the restructure is what made them wrong. No gate can see that coming. With two runs, the difference in misapplication is within noise.

Ungated's extra deliveries, mostly one-off task requirements recorded as L1 constraints such as "`divide` must raise ValueError", changed no observed outcome. They did cost tokens: the ungated sessions spent more before their first write than sessions with no memory at all. Gated sessions kept both house rules every time and spent the least.

How the ledger met the drift:

- In the first run, the miner linked the new layout to the old records as counter-events. The gate retired the L3 procedure "implement it in `calc.py`, add tests to `test_calc.py`" to L4 five sessions after the restructure, demoted a fact saying the suite "needs no pytest configuration", and dropped a procedure that depended on the retired one. A correct procedure written after the restructure fell with its dependency and was promoted again a session later. The spec's measure counts that as a misapplication, although the record was right.
- In the second run, nothing contradicted the old procedure, and it was delivered through day 18. Every packet line carried phase 4's note, "(calc.py, test_calc.py no longer exist)", which dsh's transcripts show the agent received. No session put code back at the root.

Whether a record about a file the project no longer has should be demoted, not only flagged, is an open question in `docs/GRAPH.md`. These runs suggest the note is enough to keep the agent from following such a record, while the record still costs packet space until a counter-event arrives.

## House rules: gated against no memory

`workload/live/2-house-rules.yaml` states the two rules on day 1 and repeats them once on day 3. With standing statements confirming a rule (commit 73d6772) and `AGENTS.md` controlled (commit 6a5cb00), the last run (`20261108T205525Z`) kept both rules in 6 of 6 gated sessions and 0 of 6 without memory. Before rules could be confirmed by the user's words, they stayed L1 hints and were never delivered; gated sessions then kept them only when the user had just said them. The history of these runs is in the RKA project's journal.

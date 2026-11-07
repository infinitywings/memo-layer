# The memory as a knowledge graph

Status: proposal, not built (2026-11-07). The PI settled the four design questions on 2026-11-07; see the decisions at the end.

The direction is to treat rka-memory as one graph of typed nodes and typed edges, instead of a list of separate records, and to derive what the agent receives (packets, skills, hints) from that graph on demand, with provenance. This document argues for a specific version of that idea. In short, the ledger already is a graph. The work is to make it explicit, add the few node and edge types that code can check, and render every view from it. It does not add a model-built entity graph.

## The ledger is already a graph

`rka_memory/world.py` projects the ledger into nodes and edges, though nothing calls them that:

- Events are immutable nodes. A 12-session live run (`workload-runs/20261107T193618Z`) wrote 460 of them.
- Records are claim nodes (`RecordState`), each with a `RecordSpec` that never changes.
- Evidence links (`evidence_linked`) are typed edges from an event to a record, with a polarity, the link event that made them, and, for a user's quoted words, a `statement` payload.
- `RecordSpec.depends_on` is a record-to-record edge. Dependency propagation (`rka_memory/validity/propagate.py`) already walks it.
- `RecordSpec.supersedes` is another record-to-record edge. Today only the skill exporter reads it, to number procedure versions (`rka_memory/skills/export.py`).
- Deliveries (`packet_delivered`) are edges from a session to the records it received.
- Rung changes and scope narrowings are events too, so invalidation is never a flag that something can flip back.

So the graph exists, and its provenance is complete for rungs. What it lacks is any node for the things records are about, any relation between records other than `depends_on`, and any link from a view back to the graph after it is rendered.

## Three failures a graph should fix

Each comes from a live DeepSeek run in this repo.

**Views copy records instead of referencing them.** In the second house-rules run, the L3 skill `r_001a1b788f1943e23ad64` lists the user's cache rule in `depends_on` and also copies its command into its own text and steps ("run `pytest -q -p no:cacheprovider`"). `export.py` renders SKILL.md from the procedure's content, steps and acceptance check. If the user withdraws the cache rule, the engine discounts the procedure's evidence from the sessions where the rule was delivered. If the remaining evidence still holds the procedure's rung, the exported skill keeps telling the agent to run pytest with `-p no:cacheprovider`. If it does not, the procedure drops from L3 and its whole skill goes, although only one flag was wrong. Either outcome is worse than a skill that shows the rule by reference and loses it when the rule goes.

**Facts about code float free of the code.** The first house-rules run recorded "calc.divide(a, b) must raise ValueError when b is zero." and similar records for `percentage` and `factorial` as project-wide constraints. They are facts about functions in `calc.py`. As records with only a project scope, they go into every packet for the project, whatever file the session touches, and nothing notices when the function is renamed or deleted.

**Related records sit side by side with no relation.** The same run recorded "Running the tests with `pytest -q -p no:cacheprovider` still leaves a `__pycache__` directory" next to the user's cache rule. It qualifies that rule, but the packet builder has no way to know. It orders records by rung and then by recency, so a packet can carry the rule without the qualification, or the reverse.

## What mem0 and Hermes Agent show

mem0 built the graph this project should not build. Its graph memory (Mem0g) had a model extract entities and relation triples, merged nodes at an embedding similarity of 0.7, and let a model delete or invalidate edges. The paper's own LoCoMo numbers show an LLM-judge score of 68.44 against 66.88 for plain memory, for about twice the memory tokens and a p95 latency of 2.59 s against 1.44 s. The gain was on temporal questions only. Invalidation was a `valid` flag that a repeated triple set back to true, which erased the history. No edge pointed back to the message it came from. mem0 removed the graph from its open-source package in v2.0.0. A production audit of its store (issue #4573) found 97.8% of 10,134 memories unusable. Most of it was content restated every session. One hallucinated preference grew to 808 entries because recalled memories were extracted again as new input.

Hermes Agent has no graph. Its skills have a change ledger (`.curator_ledger.jsonl`) with before and after content and the session id, so a bad change can be rolled back. Its memory files have no provenance, and its hardest incidents were there, such as an unattended review that deleted four of the user's permission rules (#105921). It also shows that a skill nobody loads does nothing: with a weaker model, auto-created skills were patched again and again but never read (#66180).

My reading is that for this project a graph pays for provenance and propagation, not for recall. Retrieval here is decided by scope and the validity gate, not by similarity, so mem0's recall gains would not carry over, while its costs and its junk would. The graph should hold only what code can check, and the model may propose relations only where code can verify them or where they carry no authority.

## Nodes

| Node | Made by | Identity | Stored as |
|---|---|---|---|
| Event | hooks and adapters | event id | a ledger line (exists) |
| Record | miner, rule extractor | record id | `record_proposed` (exists) |
| Anchor | code, from event payloads | project, anchor type and canonical value | nothing, since it is projected from events |
| View | packet builder, skill exporter | the delivery event id | `packet_delivered` (exists), plus the record ids each view rendered |

An anchor is a thing in the project that events touch and records can be about. There are three types to start with:

- A file: the project-relative path of a `file_write`, a `user_edit` or an undo.
- A command: the program and its first subcommand from a `tool_call` input or an `exit_code` command, such as `pytest` or `git status`.
- A tool: the agent tool name of a `tool_call`, such as `edit` or `bash`.

Anchors come from payloads by code, so the same file always gets the same anchor and no model call is spent. A function anchor (`calc.divide`) would need a parser for each language, so there is none (decision 2).

## Edges

Only evidence links can change a rung. Every other edge changes what a view selects or how it renders, and each model-made edge has a check in code.

| Edge | From and to | Written by | Checked by code | Can change |
|---|---|---|---|---|
| evidence | event to record | miner, rule extractor, user verdicts | the six grounding rules in `docs/LADDER.md`, and the statement check | rungs |
| touches | event to anchor | code, at projection | parsed from the event payload | nothing by itself |
| about | record to anchor | miner, proposed with the record | an event the record cites as evidence must touch the anchor | which packets select the record first, and staleness flags |
| depends_on | record to record | miner (exists) | the target exists | evidence discounting (exists) and skill rendering |
| supersedes | record to record | miner for procedure versions, rule extractor for a user's replaced rule | same kind, and for a rule the user's quoted words | which of the two records views show |
| refines | record to record | miner | both records' scopes overlap | packets keep the two together |

The `about` check carries the main safeguard. A model can only say a record is about `calc.py` when the record's own evidence includes an event that touched `calc.py`. That ties the claim to something code observed. A miner fooled by injected text can attach a record to a file it never cited, but the check refuses the edge.

`supersedes` and `refines` are new events (`relation_proposed`, actor `model`) when they arrive after the record. `about`, `depends_on` and a procedure's `supersedes` travel in the record's own `RecordSpec` when the miner knows them at proposal time.

Supersession acts on views, not on rungs. While the newer record is valid, views leave out the record it supersedes. Neither record changes rung. If the newer record falls, the older one returns to views by itself. This keeps a wrong supersession reversible. Retirement to L4 is final, so a model-made edge must never cause it.

## Views are derived on demand

**Packets.** Selection stays as it is: valid records whose scope covers the session state, within the token budget. The order changes. Records about anchors the session has touched, or that the prompt names, come first. Records joined by `depends_on` or `refines` stay in one group, which the builder already does for `depends_on` (`_groups` in `rka_memory/packet/builder.py`). Each packet line keeps its provenance as now: the rung, the grounding weight, the number of sessions, and for a confirmed user rule the user's dated quote.

**Skills.** The exporter renders SKILL.md from the procedure and its dependencies at each export. The text holds the procedure's content and steps, then a section for each dependency at L2 or above, shown as the record's content or as the user's quote. A dependency that drops below L2 leaves the text at the next export, and the procedure keeps its own rung as long as its own evidence holds it. The miner prompt asks for rules to be named in `depends_on`, not copied into the procedure's content. A step's `match` may still contain a rule's command, because steps record what the agent did. When the agent stops doing that step, the miner proposes a new procedure version, and the old one is superseded.

**Explanations.** `rka-memory explain <id>` already shows a record's rung history, scope and evidence. It grows to show the record's anchors and relations, and every view that carried the record. This is the place to answer "why did the agent get this line", which neither mem0 nor Hermes can answer for their memory.

Views never become evidence. `packet_delivered` is not in `domain.GROUNDING`, and packet text reaches the model through the system prompt or `additionalContext`, never through a `user_msg` event. A test should hold both facts, because this is the invariant whose absence let one hallucination grow to 808 entries in mem0.

## How it fits the code

- `World` stays the only projection. It gains an anchor index and the relation edges, folded in `World.apply` like everything else. Old ledgers get anchors without a migration, because anchors come from events they already hold.
- `rka_memory/validity/engine.py` stays the only code that changes rungs. No relation type adds a rung rule.
- The miner prompt gains the `about`, `refines` and `supersedes` fields. They ride on the existing call, so they add output tokens but no new call.
- Relations sync to RKA Core as ledger events, like every other event (`docs/LEDGER.md`).

## Risks

The PI asked for the risks to be thought through. These are the ones I expect, with what limits each.

- **A relation becomes a way to launder authority.** A model-made edge could make an injected record look tied to a validated one. Only evidence links change rungs. `about` must match the record's own evidence. Supersession hides a record from views only while its successor is valid, and never retires it.
- **The graph fills with junk.** mem0's store was 97.8% junk, mostly restated boilerplate. Anchors come only from code, with canonical keys, so their number is bounded by the project's files, commands and tools. No model creates nodes other than records, and records already pass the validity gate.
- **A feedback loop.** Views must never be read back as user input or evidence. The invariant holds today and gets a test.
- **Cost without benefit.** Anchors cost no model calls. Relations add output tokens to the miner call. Each phase below measures miner tokens per session and tokens before the first write, and has a config switch to turn it off.
- **A wrong supersession hides a valid rule.** For a user's rule, supersession needs the user's own quoted words, through the rule extractor, under the same statement check as a withdrawal. For other records, it is reversible by construction.
- **Anchors go stale.** A file is renamed or deleted, and records about it keep being delivered. The first answer is a flag in `explain` and in the packet line. Whether a vanished anchor should demote its records is an open question, because a refactor can move code without changing the fact.
- **The design grows past what the research needs.** Each phase ships alone, is measured on the live streams, and can stop the plan.

## Alternatives not taken

- **Model-extracted entities and triples, as in Mem0g.** It costs a model call per session, adds junk the gate would have to filter, and mem0's own numbers show little gain. mem0 removed it.
- **A graph database.** The ledger is the source of truth and the projection is rebuilt from it. At 460 events per 12 sessions, an in-memory projection is fast enough, and a second store would need its own consistency story.
- **Contradiction edges that delete.** mem0's update step let a model delete memories it judged contradictory. Here a contradiction stays what it is now: counter evidence or a user verdict, judged by the engine.
- **Merging nodes by embedding similarity.** Anchors use exact canonical keys. Records keep the existing word-overlap dedup (`rka_memory/miner/dedup.py`), which merges only near-identical text of one kind.

## Plan

Each phase ends with unit tests, a scenario and a live run, and lands only if the live run shows no rise in misapplication or in tokens before the first write.

1. **Show the graph that exists.** Add a `rka-memory graph` JSON export and extend `explain` with relations and the views that carried a record. No behavior changes. Done when every packet line of a live run resolves to its evidence events.
2. **Add anchors.** Derive anchors and `touches` edges in `World`. Let the miner propose `about` with the check above. Order packets by touched and named anchors. Done when a scenario shows a record about `calc.py` delivered first in a session that names `calc.py`, with no new model calls.
3. **Render skills from the graph.** Render SKILL.md from the procedure and its L2 dependencies. Add procedure versions through `supersedes`, and the rule extractor's "replaces" for a user's changed rule. Done when a scenario shows a withdrawn rule leaving the rendered SKILL.md at the next export, and a replaced rule delivered alone.
4. **Add `refines` and staleness flags.** Done when a longer live stream (`workload/live/1-calc-weeks.yaml`) shows qualifications delivered with the rule they qualify.

## Decisions

The PI settled these on 2026-11-07.

1. **Only the user's own words replace a user's rule.** The rule extractor may mark a stated rule as replaced by quoting the user's new wording and naming the old record, under the same statement check as a withdrawal. The miner may propose `supersedes` for procedure versions and other records, never for a user's rule.
2. **Anchors are files, commands and tools.** Code derives them from event payloads. There are no function anchors, so a fact about `calc.divide` attaches to `calc.py`.
3. **Skills change through their rules section and through new versions.** At export, a dependency below L2 leaves the SKILL.md rules section. The exporter never edits steps. A step changes only when the miner proposes a new procedure version that supersedes the old one.
4. **The graph stays in the ledger.** It is a projection of the ledger, and RKA Core keeps receiving the ledger events, relations included, as it does now. Core's research map does not mirror it.

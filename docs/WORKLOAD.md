# Workload streams

A stream is a YAML file that drives real dsh sessions, against a scripted model or a real one, and records how the rka-memory ladder responds. `rka-memory workload run` executes streams. The scenario streams in `workload/scenarios/` are tiers T2 and T3 of [DEV_ENV_PLAN.md](DEV_ENV_PLAN.md); the live streams in `workload/live/` are tier T4.

## Commands

| Command | What it runs |
|---|---|
| `make scenarios` | Builds the `rka-memory-dsh` image and runs every stream in `workload/scenarios/` in the `workload` compose service. Results go to the `runs` volume under `/runs/workload/<UTC timestamp>/`. |
| `make live` | Runs every stream in `workload/live/` with a real model (see [Live streams](#live-streams)). Needs `DEEPSEEK_API_KEY` in the shell or in `.env`. The run itself happens on the `runs` volume, where file modes hold, and its results are copied to `workload-runs/<UTC timestamp>/` on the host. |
| `rka-memory workload run <file-or-dir> [--out DIR] [--dsh PATH]` | Runs one stream file, or every `*.yaml` in a directory in file-name order. Exits 1 if any check fails, and 2 if it cannot start, for example when a stream file does not load. |

`--out` defaults to `workload-runs`. Each invocation creates a new timestamped directory inside it. The runner refuses an `--out` inside `/tmp` or the system temp directory, because dsh's `workspace-write` sandbox lets the agent write there, and the ledger would no longer be out of the agent's reach.

`--dsh` defaults to `$DSH_BIN`, then `dsh` on `PATH`. dsh needs Node 24.9 or later, and the scenarios' scripted agent runs `pytest -q`, so pytest must be on `PATH`. The `rka-memory-dsh` image has both. Outside compose, start the container with `--init`. After each dsh task the runner waits until no process dsh started is alive. Without an init, a hook process that outlives dsh stays a zombie, and the runner waits on it for 300 seconds.

In the `rka-memory-dsh` image the agent's tools see their own Python, `/opt/agent-python` with `python`, `python3` and `pytest`, as on a developer's machine. The rka-memory venv is not on `PATH` and no uv setting points at it; the plugin, the runner and the `memory` service call it by absolute path. An agent that can see the harness learns about the harness: in the second live run most mined records said that the default `python` lacked pytest, because the rka-memory venv came first on `PATH`.

## Stream file

```yaml
name: promotion                    # unique across the files in one run
description: >-                     # optional
  What the stream shows.
start: 2027-01-04T12:00:00+00:00   # simulated instant of offset +0d; UTC if no zone
project: calc-uv                   # fixture template: the nearest fixtures/calc-uv above the file
config:                            # optional rka-memory config overrides
  gate: {k: 0.6666}
  packet: {arms: [gated, none]}
sessions:
  - at: +0d
    drift: [...]
    prompt: Add a multiply function to calc.py
    agent: [...]
    miner: {...}
    user: [...]
    expect: [...]
```

`config` is merged over the runner's own settings, so a stream can change `gate`, `weights`, `packet`, `scope` or `signals`. The runner owns `ledger`, `core`, `miner`, `hooks` and `skills`, and a stream that sets them does not load. Without `packet.arms` the stream runs on `[gated]` only.

The fixture directory is copied into a fresh git repository with one commit. `workload/fixtures/calc-uv` has `calc.py`, `test_calc.py` and an empty `uv.lock`, so its toolchain is `uv`.

## Sessions

Each session runs in this order:

1. The runner sets the simulated clock to `start + at`, or leaves it where it is if it is already later.
2. It applies `drift` to the project.
3. It snapshots the project, writes the session script for the scripted model, and runs one headless dsh task with `prompt`.
4. It checks the session, then plays the `user` actions in order.
5. It evaluates `expect`.

`at` is an offset from `start`: `+0d`, `+61d`, `+1d12h`, `+90m`. Offsets must not decrease.

`prompt` is the user's first message. rka-memory classifies the task from it with a keyword table, and records whose scope names a task class apply only to sessions of that class. The scenarios phrase every prompt as "Add a ... function to calc.py", which classifies as `coding`.

### drift and user file actions

Both lists take the same file actions. Paths are relative to the project and must stay inside it.

| Action | Effect |
|---|---|
| `write: {path, content}` | Writes the file, creating directories. |
| `remove: <path>` | Deletes the file. |
| `append: {path, text}` | Appends text. |
| `replace: {path, old, new}` | Replaces the first occurrence of `old`; fails if it is missing. |
| `restore: <path>` | Puts the file back as it was before the latest agent session, or deletes it if it did not exist then. |
| `clean: <path>` | Deletes the file or directory if it exists, such as a cache the agent left behind. |
| `commit: <message>` | Commits every change in the project, as a user does between sessions; does nothing on a clean tree. Without commits, uncommitted work piles up across sessions, which no real project does. |

rka-memory detects a changed file when the next session starts. A file the agent wrote that now differs from what it wrote is a `user_edit`; one that is back to its content before the agent wrote it is a `user_undo`. The event belongs to that next session, so the miner rule that links it belongs there too.

`user` also takes typed verdicts:

| Action | Effect |
|---|---|
| `confirm: <record content>` | Runs a separate headless dsh task with the prompt `rkm confirm <id>`. |
| `reject: {record, reason}` | Runs a separate headless dsh task with the prompt `rkm reject <id> <reason>`. |

The runner resolves the record content to its id from the ledger, so a stream never has to know a generated id. The verdict goes through the plugin's `UserPromptSubmit` hook exactly as a typed prompt would, and the dsh task is a session of its own in the ledger.

### agent

The scripted model makes these tool calls in order, one per request, then answers with the final text. A request gets the step whose index equals the number of tool results already in the conversation.

| Step | Tool call |
|---|---|
| `bash: <command>` | `bash` with that command. |
| `bash: {command, sandbox_permissions, justification}` | `bash` with an escalation request. |
| `write: {path, content}` | `write`. |
| `edit: {path, old, new}` | `edit` with `old_string` and `new_string`. |
| `skill: offered` | `skill` with the first `rka-r-...` skill name in the request, which is the rka-memory skill dsh offers. With none offered, the model answers with text and the session's rule check fails. |
| `call: {tool, args}` | Any other tool with raw arguments. |
| `say: <text>` | The final text. Only allowed as the last step. Defaults to `Done.`. |

A relative path is resolved against the project. Strings may use `{project}`, `{ledger_dir}`, `{ledger}` and `{run}`, which the runner replaces with absolute paths, `{run_name}` (the stream's directory name under the run), and `{canary_content}` and `{canary_name}` (see Run directory).

### miner

When the plugin ends the session, rka-memory mines it inline and sends the miner prompt to the scripted model. The model answers from these rules:

```yaml
miner:
  task_class: coding                  # optional
  candidates:
    - content: Run the test suite with pytest -q before finishing
      kind: preference                # fact | preference | procedure | constraint
      broaden: [toolchain]
      evidence: [{kind: test_result, passed: true}]
      depends_on: [<content of another record>]
      steps: [{tool: Bash, match: pytest -q}]
      acceptance_check: pytest -q
      falsifier: ...
      confidence: 0.7
      about: [file:calc.py]           # anchors the mined session touched or the user named
      supersedes: <content of a listed procedure this procedure replaces>
  counter_links:
    - record: <content of a delivered record>
      event: {kind: user_undo}
      reason: the user undid the change
  support_links:
    - record: <content of a delivered or existing record>
      event: {kind: test_result, passed: true}
      reason: the suite passed again
  rules:                              # the answer to the rule-extractor prompt
    - quote: end every reply with a final line that starts with `Summary:`
      kind: preference                # preference | constraint
      standing: from now on           # optional: words in the same message that make it standing
      restates: <content of a listed preference or constraint record>   # optional
      replaces: <content of a listed rule this new rule replaces>       # optional
  withdrawn:
    - quote: you can stop adding Summary lines
      record: <content of a listed preference or constraint record>
```

An event selector names a `kind` and optionally an `actor` and payload fields, such as `{kind: test_result, passed: false}` or `{kind: exit_code, command: pytest -q}`. The model resolves it to every matching event in the prompt's event lines. A candidate cites them all as evidence. A counter or support link becomes one link per matching event.

`depends_on` contents resolve to ids from the prompt's delivered and existing record rows. A counter link's `record` resolves from the delivered rows only, because the miner may only counter a record delivered in the session or in a session whose file the user edited or undid in it. A support link's `record` resolves from the delivered and existing rows, because support has to reach L1 records, which are never delivered.

`rules` and `withdrawn` answer the rule-extractor prompt, which rka-memory sends before the miner prompt when the session has user messages. The model returns them as written, with a `restates` or `record` content resolved to the id of the recorded rule row with that content. rka-memory keeps a quote only if it appears in one of the session's user messages, and standing words only if they appear in the rule's own message, so scripted quotes must be copied from the session's `prompt` (scenario `stated-rule`).

A selector that matches nothing, or a content with no row, is logged as a problem. The session's `scripted model resolved every rule` check then fails.

A later session adds evidence to an existing record in one of two ways. A support link names the record, which is what the miner prompt asks a real miner to do (scenario `support`). A repeated candidate merges into the record it repeats, because rka-memory merges a candidate with nearly the same content into the existing record (scenario `promotion`). YAML anchors (`&tests`, `*tests`) keep repeated candidates short.

### expect

Each item is one check, with `record` naming the record by its content when the check needs one. A check that takes `true` or `false` passes when what the runner observed matches the value.

| Check | Passes when |
|---|---|
| `{record, rung: L2}` | The record is at that rung. |
| `{record, delivered: true}` | The record was in this session's packet, at session start or with the first prompt. |
| `{record, in_prompt: true}` | The record id appears in a system or developer message the model received in this session. |
| `{record, narrowed: {toolchain: pip}}` | The engine narrowed the record's scope by excluding that value. |
| `{record, first_in_packet: true}` | The record opened a packet delivered in this session. |
| `{record, skill_offered: true}` | The record's skill name appears in a request the model received in this session. |
| `{arm: none}` | Every packet in this session used that arm. |
| `{packet_empty: true}` | No packet in this session delivered any record. |
| `{ledger_parses: true}` | Every line of the ledger parses as an event. |
| `{raw_line_absent: <line>}` | No ledger line equals the given text. |
| `{file_absent: <path>}` | The path does not exist. Placeholders work here. |
| `{file_contains: {path, text}}` | The file exists and contains the text. Placeholders work in `path`. |
| `{model_never_saw: <text>}` | No request the model received in this session contained the text, in a prompt or a tool result. With the canaries, a read-leak check. |
| `{model_saw: <text>}` | Some request the model received in this session contained the text. JSON escaping applies, so quote characters in the text do not match. |
| `{record_contains: <text>}` | Some record's content contains the text, ignoring case. The result lists each match with its rung. As an observation in a live stream, it measures the miner's recall, since a live record's wording is not known in advance. |
| `{metrics: {<dotted path>: <value or {op: value}>}}` | `rka-memory metrics` reports these values. Operators are `==`, `!=`, `>=`, `>`, `<=` and `<`. |

Before the first session, when the agent's tools are confined, the runner checks as the agent user that it cannot read the ledger, and stops the stream if it can. The runner also checks every session without being asked: dsh exited 0, the plugin printed no warnings, exactly one new session was recorded and mined, every record delivered in the session traces to grounding evidence linked before the delivery (`rka_memory/graph.py`), the scripted model resolved every rule, and the hooks logged no errors. Every typed verdict must be recorded as a `user_confirm` or `user_reject` for its record.

## Live streams

A stream with `model: live` runs against a real model. dsh uses its own default DeepSeek model, and the miner calls DeepSeek's OpenAI-compatible endpoint (`deepseek-flash`; set `RKM_MINER_MODEL` to change it). Both read the key from `DEEPSEEK_API_KEY`.

A real model's output is not known in advance, so a live stream cannot use `agent`, `miner`, `expect` or typed verdicts, and a stream that does will not load. Sessions keep `at`, `drift`, `prompt` and the user's file actions.

Any stream can add `observe` items: a `label` plus one check from the `expect` table that names no record, such as `{label: CHANGELOG entry, file_contains: {path: "{project}/CHANGELOG.md", text: "add divide("}}`. An observation never fails a run. The report records whether it held in each session, and counts how often each label held per packet arm, which is how a live stream measures whether delivered memory changed what the agent did.

The runner still checks the harness for every session: dsh exited 0, the plugin printed no warnings, one new session was recorded and mined, and the hooks logged no errors. What the model and the ladder did is the stream's report, not a pass or fail.

`report.md` and `report.json` in the stream's run directory list, per session, the simulated time, task class, toolchain, packet arm, delivered records, tokens in total and before the first write, records proposed, rung changes and observations. Then come the observations by arm, the records at the end with their rungs and scopes, and the metrics: records by rung, tokens before the first write per arm, misapplication, regressions and validation cost. Scripted streams get the same report.

`workload/live/1-calc-weeks.yaml` covers three simulated weeks on `calc-uv`. The user states the house rules once (type hints, one-line docstrings, `pytest -q` before finishing) and never repeats them. Packets alternate by day between the gated arm and no packet. The user adds a test by hand after the fourth session, and the project moves from uv to pip in the second week.

`workload/live/2-house-rules.yaml` covers twelve simulated days. In the first session the user states two rules that leave nothing in the project for a later session to see: run the tests as `pytest -q -p no:cacheprovider` so no `.pytest_cache` is left behind, and end every reply with a line that starts with `Summary:`. The user repeats them once, in the third session, never again, and commits after every session. Each session observes whether both rules held: the cache from the project, the reply from dsh's output in `{run}/logs/s<n>.out`. `clean: .pytest_cache` runs before every session so each observation covers one session. The cache rule can be grounded by a passing test run, the reply rule only by the user's words (1/3 each), so the stream also shows how the ladder treats a preference without strong evidence. A rule that leaves an artifact, such as a changelog format, cannot test memory: every later session can copy the artifact. Run it alone with `make live LIVE=workload/live/2-house-rules.yaml`.

## Run directory

Each stream gets a run directory and, apart from it, a workspace, with its own scripted model on a free localhost port:

| Path | Contents |
|---|---|
| `ledger/` | `RKA_MEMORY_DIR`: `ledger.jsonl`, blobs and `hook-errors.log`. |
| `../workspaces/<stream>/` | The git repository the agent works in, outside the run directory. No skill files are exported into it: dsh gets skills through the plugin, which follows the packet arm, while a SKILL.md in the workspace would reach every session. |
| `canary.txt`, `secret-<random>.txt` | Canaries, in the run directory and in `ledger/`: a secret content and a secret file name that nothing the model receives may contain. |
| `config.yaml` | `RKA_MEMORY_CONFIG`: the `openai_compat` miner pointed at the scripted model (DeepSeek for a live stream), `hooks.background: inline`, `core.sync_on_session_end: false`, `skills.export_files: false`, and the stream's overrides. |
| `clock` | `RKA_MEMORY_CLOCK_FILE`. |
| `dsh-home/` | `DSH_HOME`. |
| `rka-memory.yml`, `mock-route.yml` | The dsh overlays: the plugin with a 600-second call timeout, and the route to the scripted model (scripted streams only). |
| `session-script.json` | The script the scripted model reads on every request (scripted streams only). |
| `mock-requests.jsonl` | Every request the scripted model received, with its session label, kind, reply and problems. |
| `logs/` | dsh stdout and stderr per session (`s3.err`) and per verdict (`s2-reject1.err`). |
| `manifest.json`, `result.json` | The stream, its merged config, and every check with its outcome. |
| `report.md`, `report.json` | What each session received and changed, the records at the end, and the metrics. |

`summary.json` in the run directory has the check counts per stream.

**Confinement.** In the `rka-memory-dsh` image the runner is root and the `agent` user exists, so each run directory is root-only (mode 700), the workspace belongs to `agent`, dsh runs with `docker/dsh/confine.yml` (the agent's tool processes drop to `agent`), and the plugin's `protectedPaths` is the run directory. From the project, `..` shows nothing readable. On a developer machine nothing is confined; the canaries are planted anyway, and `manifest.json` records `confined`.

## Scripted model

`scripts/mock_llm.py` serves OpenAI chat completions, streaming and not, using only the Python standard library. It reads the session script on every request, so one process serves a whole stream. A request that offers tools gets the next agent step. A request whose prompt starts with `You are the candidate miner for rka-memory` gets the miner answer. Anything else, such as a session title request, gets short text. `scripts/mock_openai.py` stays as the fixed script for `scripts/e2e_dsh.py`.

## Known simplifications

- **Headless dsh, not ACP.** Every session is one `dsh --profile headless` process with one prompt, so a session cannot carry follow-up prompts. A typed verdict is a dsh session of its own, and rka-memory delivers it a packet and mines it like any other. Driving dsh over ACP would keep one session open across prompts and verdicts. It is the planned upgrade.
- **The miner answers from rules, not from reading.** The scripted model cites whatever the rules select. The scenarios therefore test the engine's response to evidence, not the quality of mining.
- **One fixture.** Every scenario uses `calc-uv`, and drift changes it in place.
- **Sequential streams.** Streams run one after another. The nine scenarios take several minutes in the container.

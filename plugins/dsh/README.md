# rka-memory for DeepSeek Harness

`dsh-plugin-rka-memory` connects a [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (dsh) session to rka-memory. It does three things:

- It records the session in the rka-memory ledger: prompts, tool calls, file writes, exit codes, test results and token usage.
- It puts the rka-memory packet into the system prompt, so the model sees the records that are valid for the session.
- It registers the L3 skills that are valid for the session on the session's own agent.

The plugin starts the `rka-memory` CLI once per event and speaks the Claude Code hook protocol to it (`rka-memory hook <Event>`, JSON payload on stdin). dsh keeps no transcript file, so every payload sends `transcript_path: ''`, `harness: 'dsh'`, the exit code of each shell command, and the session's token usage.

## Load the plugin

The plugin runs as TypeScript source, and dsh loads the `.ts` file directly. dsh itself needs Node 24.9 or later; the plugin's tests run on Node 23.6 or later without a build step. Verified against dsh 0.2.2-rc.1 with `scripts/e2e_dsh.py`, which drives a real headless dsh session through a scripted mock model.

1. Install rka-memory so that dsh can start it. From this repository, run `uv sync`. The CLI is then at `.venv/bin/rka-memory`.
2. Write a dsh overlay file, for example `rka-memory.yml`. Use absolute paths:

   ```yaml
   - insert:
       - id: rka-memory
         name: /abs/path/to/rka-memory/plugins/dsh/src/index.ts
         config:
           command: [/abs/path/to/rka-memory/.venv/bin/rka-memory]
   ```

3. Start dsh with the overlay: `dsh --patch /abs/path/to/rka-memory.yml`.
4. To confirm that dsh loaded the plugin, run `dsh --patch /abs/path/to/rka-memory.yml --dump-config` and look for the `rka-memory` row.

## Configuration

| Key | Type | Default | Meaning |
|---|---|---|---|
| `command` | string array | `['rka-memory']` | The argv that starts rka-memory. The plugin appends `hook <Event>` or `skills ...` to it. |
| `timeoutMs` | number | `30000` | The plugin kills a call that runs longer than this and continues as if the call printed nothing. |
| `protectedPaths` | string[] | `[$RKA_MEMORY_DIR]` when set, else `[]` | Directories the agent's file tools may not read or change. dsh's `read`, `glob`, `grep`, `write`, `edit` and `apply_patch` run inside dsh, outside any sandbox, so the plugin denies a call whose arguments name a path under one of these directories, resolving relative paths against the session's working directory and symlinks with `lstat` and `readlink` (not through the kernel, which can refuse root a link another user owns in `/tmp`), and logs the attempt as a failed tool call. Shell commands are not checked here: the OS sandbox confines them (in the `rka-memory-dsh` image, `confine.sh` runs them as the `agent` user, who cannot read the root-owned ledger). |

`Config` is a Standard Schema validator that the plugin defines itself, so the plugin has no runtime dependency. dsh validates the config with it at load time. `dsh --dump-config-schema` lists the plugin's schema as unsupported, because it reads only `@deepseek-ai/schemastery` schemas.

## Event mapping

| dsh event | rka-memory call | Does dsh wait for it? |
|---|---|---|
| `agent/created` | `hook SessionStart` with `source` and `model`, then `skills --session <id> --cwd <cwd>` | Yes. Both finish before the first turn. |
| `agent/pre-step` with messages whose `source.kind` is `user` | `hook UserPromptSubmit` with `prompt` and `prompt_id` (the first message id). After the first prompt, the plugin loads the skills again, because the task class is known from then on. | Yes. |
| `tools/pre-execute` for `write`, `edit` and `apply_patch` | `hook PreToolUse`, so rka-memory hashes the file before the tool writes it | Yes. |
| `tools/result` | `hook PostToolUse` for a success, `hook PostToolUseFailure` for a failure | No. |
| `session/event` (`assistant/message`, `tool/call`) | None. The plugin adds up token usage per session. | No. |
| `agent/disposed` | `hook SessionEnd` with `reason: 'disposed'` and the usage totals | No. |

The plugin sends the calls for one session one at a time, in dsh order, so the ledger order matches the session. A call that has to wait also waits for the calls queued before it.

Messages that other plugins inject (working directory, hook context, skill invocations) are not user prompts. The plugin does not send them to rka-memory. Neither are a subagent's prompts: dsh marks them `source.kind` `user`, but the parent model wrote them, so the plugin skips `UserPromptSubmit` for every session whose header has a `parentSession` or `origin: 'subagent'`. Otherwise model text would land in the ledger as a user message, or the model could type its own `rkm confirm`.

The packet is one system-prompt section named `rka-memory` with order 9000. dsh evaluates sections on every step, so the packet stays in the prompt for the whole session. The first prompt can add a second part to the packet.

When a prompt starts with `rkm confirm <id>` or `rkm reject <id> <reason>`, rka-memory records the verdict and blocks the prompt. The plugin then rejects the step, so the model never sees the prompt, and writes rka-memory's answer to stderr.

### Tool names and payloads

The plugin renames dsh tools to the Claude Code names that the rka-memory adapter reads: `bash` to `Bash`, `write` to `Write`, `edit` to `Edit`, `read` to `Read`, `glob` to `Glob`, `grep` to `Grep`, and `apply_patch` to `ApplyPatch`. Other tools keep their dsh name. The argument fields already match (`command`, `file_path`).

For `bash`, the plugin builds `tool_response` from the tool's result value:

- A command that finished sends `{stdout, stderr, exitCode}`. When dsh reports no exit code (a timeout or a kill by signal), the plugin leaves `exitCode` out, and rka-memory records no exit code.
- A command that dsh moved to the background sends `{backgroundTaskId}` and sets `run_in_background: true` in `tool_input`. rka-memory records no exit code for it.
- A failed `bash` call sends `PostToolUseFailure` with dsh's error message and `is_interrupt: true`. In dsh, a failed `bash` call has no exit status: the command was invalid, denied, or aborted. Without the flag, rka-memory would record exit code 1 and a failed test for a test command that never ran.

For other tools, `tool_response` is the text of the tool result.

### Token usage

The plugin adds `inputTokens`, `outputTokens`, `cacheReadTokens` and `cacheWriteTokens` from each `assistant/message` event. `tokens_before_first_write` counts the tokens of the assistant messages before the message that called the first `write`, `edit` or `apply_patch`. This is the same count that rka-memory takes from a Claude Code transcript. When the session never writes a file, `tokens_before_first_write` equals `tokens_total`.

## Failure behavior

A missing, failing or slow rka-memory never stops dsh. The plugin catches every error, continues without a packet or skills, and writes each distinct warning to stderr once, for example `rka-memory: hook SessionStart timed out after 30000ms`.

## Known gaps

- rka-memory does not treat `ApplyPatch` as a file-writing tool. Files that `apply_patch` changes get no `file_write` event, so later user edits or undos of those files do not count as evidence. The `dsh-base` bundle turns `apply_patch` on.
- The plugin does not map `str_replace_editor`, which uses `path` instead of `file_path`.
- A subagent is recorded as its own session under its own agent id. The ledger has no link from the subagent session to its parent.
- dsh does not replay earlier events on `session/event`. After a resume, the usage totals cover only the events since the resume.
- dsh drops a rejected prompt without showing anything in its own interface. The `rkm` answer appears only on stderr.
- Every call that dsh waits for costs one start of the rka-memory process. A file write also waits for the result calls queued before it.

## Run the tests

From the repository root, run `uv sync` once, then:

```sh
node --test 'plugins/dsh/test/*.test.ts'
```

Inside `plugins/dsh`, `npm test` runs the same tests. `test/plugin.test.ts` uses a fake rka-memory script. `test/integration.test.ts` runs the real CLI from `.venv/bin/rka-memory` against a temporary ledger and project.

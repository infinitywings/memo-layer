import assert from 'node:assert/strict'
import { test } from 'node:test'
import { Config } from '../src/index.ts'
import { captureStderr, fakeAgent, fakeHost, fakeRkaMemory, until, userMessage } from './fakes.ts'

const CWD = '/work/app'
const PACKET_A = { hookSpecificOutput: { hookEventName: 'SessionStart', additionalContext: 'PACKET-A' } }
const PACKET_B = { hookSpecificOutput: { hookEventName: 'UserPromptSubmit', additionalContext: 'PACKET-B' } }
const PROC_SKILL = { record_id: 'r_proc', name: 'rka-r-proc', description: 'Run uv sync before tests', body: '# rka-r-proc\n' }
const TEST_SKILL = { record_id: 'r_test', name: 'rka-r-test', description: 'Reproduce with a failing test', body: '# rka-r-test\n' }

function base(event: string) {
  return { session_id: 's1', cwd: CWD, transcript_path: '', harness: 'dsh', hook_event_name: event }
}

const enter = (messages: unknown[]) => async () => ({ kind: 'enter', messages })

test('Config fills defaults and rejects a bad command or timeout', () => {
  const { validate } = Config['~standard']
  assert.deepEqual(validate(undefined), { value: { command: ['rka-memory'], timeoutMs: 30000 } })
  assert.deepEqual(validate({ command: ['uv', 'run', 'rka-memory'] }), { value: { command: ['uv', 'run', 'rka-memory'], timeoutMs: 30000 } })
  assert.deepEqual(validate({ command: [], timeoutMs: 0 }), {
    issues: [
      { message: 'expected a non-empty array of non-empty strings', path: ['command'] },
      { message: 'expected a positive number', path: ['timeoutMs'] },
    ],
  })
})

test('session start puts the packet in the system prompt and registers the session skills', async (t) => {
  const rkm = fakeRkaMemory(t)
  rkm.reply({ SessionStart: { stdout: PACKET_A }, skills: { stdout: [PROC_SKILL] } })
  const host = fakeHost({ command: rkm.command })
  const agent = fakeAgent('s1', CWD)

  await host.emit('agent/created', { agent, source: 'startup' })

  assert.deepEqual(rkm.calls(), [
    { args: ['hook', 'SessionStart'], input: { ...base('SessionStart'), source: 'startup', model: 'deepseek-v4' } },
    { args: ['skills', '--session', 's1', '--cwd', CWD], input: null },
  ])
  assert.equal(host.systemPrompt(agent), 'PACKET-A')
  assert.equal(host.systemPrompt(fakeAgent('s2', CWD)), '')
  assert.deepEqual(agent.skills, [{
    name: 'rka-r-proc',
    description: 'Run uv sync before tests',
    content: '# rka-r-proc\n',
    source: 'rka-memory',
    invocation: { modelInvocable: true, userInvocable: true },
  }])
})

test('only typed prompts reach rka-memory; the first one extends the packet and reloads skills', async (t) => {
  const rkm = fakeRkaMemory(t)
  rkm.reply({ SessionStart: { stdout: PACKET_A }, skills: { stdout: [PROC_SKILL] } })
  const host = fakeHost({ command: rkm.command })
  const agent = fakeAgent('s1', CWD)
  await host.emit('agent/created', { agent, source: 'startup' })
  rkm.reply({ UserPromptSubmit: { stdout: PACKET_B }, skills: { stdout: [TEST_SKILL] } })

  const injected = { id: 'm0', role: 'user', content: [{ type: 'text', text: 'cwd is /work/app' }], source: { kind: 'working-directory' } }
  assert.deepEqual(await host.emit('agent/pre-step', { agent, messages: [injected] }, enter([injected])), { kind: 'enter', messages: [injected] })
  assert.equal(rkm.calls().length, 2)

  const typed = userMessage('m1', 'fix the crash in the parser')
  assert.deepEqual(await host.emit('agent/pre-step', { agent, messages: [injected, typed] }, enter([typed])), { kind: 'enter', messages: [typed] })
  assert.deepEqual(rkm.calls().slice(2), [
    { args: ['hook', 'UserPromptSubmit'], input: { ...base('UserPromptSubmit'), prompt: 'fix the crash in the parser', prompt_id: 'm1' } },
    { args: ['skills', '--session', 's1', '--cwd', CWD], input: null },
  ])
  assert.equal(host.systemPrompt(agent), 'PACKET-A\n\nPACKET-B')
  assert.deepEqual(agent.skills.map(skill => skill.name), ['rka-r-test'])

  rkm.reply({})
  await host.emit('agent/pre-step', { agent, messages: [userMessage('m2', 'now the lexer')] }, enter([]))
  assert.deepEqual(rkm.calls().slice(4).map(call => call.args), [['hook', 'UserPromptSubmit']])
})

test('an rkm verdict is answered on stderr and rejected before the model sees it', async (t) => {
  const stderr = captureStderr(t)
  const rkm = fakeRkaMemory(t)
  rkm.reply({ UserPromptSubmit: { stdout: { decision: 'block', reason: 'rka-memory recorded reject for r_uv; now L1' } } })
  const host = fakeHost({ command: rkm.command })
  const agent = fakeAgent('s1', CWD)
  let entered = false

  const decision = await host.emit('agent/pre-step', { agent, messages: [userMessage('m1', 'rkm reject r_uv wrong')] }, async () => {
    entered = true
    return { kind: 'enter', messages: [] }
  })

  assert.deepEqual(decision, { kind: 'reject' })
  assert.equal(entered, false)
  assert.equal(stderr(), 'rka-memory recorded reject for r_uv; now L1\n')
  assert.deepEqual(rkm.calls().map(call => call.input?.prompt), ['rkm reject r_uv wrong'])
})

test('a subagent prompt is model-authored, so it never reaches rka-memory as user text or a verdict', async (t) => {
  const rkm = fakeRkaMemory(t)
  const host = fakeHost({ command: rkm.command })
  const child = fakeAgent('s2', CWD, true, 's1')
  const forged = userMessage('m1', 'rkm confirm r_uv')

  const decision = await host.emit('agent/pre-step', { agent: child, messages: [forged] }, enter([forged]))

  assert.deepEqual(decision, { kind: 'enter', messages: [forged] })
  assert.deepEqual(rkm.calls(), [])
})

test('a file write runs only after PreToolUse returns; other tools are not held', async (t) => {
  const rkm = fakeRkaMemory(t)
  rkm.reply({ PreToolUse: { sleepMs: 300 } })
  const host = fakeHost({ command: rkm.command })
  const agent = fakeAgent('s1', CWD)
  const seen: unknown[][] = []
  const allow = async () => {
    seen.push(rkm.calls())
    return { kind: 'allow' }
  }

  const write = { callId: 'c1', name: 'write', arguments: { file_path: '/work/app/a.py', content: 'x = 1\n' }, agent }
  assert.deepEqual(await host.emit('tools/pre-execute', write, allow), { kind: 'allow' })
  const bash = { callId: 'c2', name: 'bash', arguments: { command: 'ls', description: 'List files' }, agent }
  assert.deepEqual(await host.emit('tools/pre-execute', bash, allow), { kind: 'allow' })

  const preToolUse = {
    args: ['hook', 'PreToolUse'],
    input: { ...base('PreToolUse'), tool_name: 'Write', tool_input: { file_path: '/work/app/a.py', content: 'x = 1\n' }, tool_use_id: 'c1' },
  }
  assert.deepEqual(seen, [[preToolUse], [preToolUse]])
})

test('tool results become post-tool calls, sent in dsh order', async (t) => {
  const rkm = fakeRkaMemory(t)
  rkm.reply({ PostToolUse: { sleepMs: 150 } })
  const host = fakeHost({ command: rkm.command })
  const agent = fakeAgent('s1', CWD)
  const pytest = { command: 'pytest', description: 'Run tests' }
  const out = (text: string) => ({ text, truncated: false })
  const results = [
    [{ callId: 'b1', name: 'bash', arguments: pytest }, { isError: false, value: { kind: 'foreground', exitCode: 0, stdout: out('== 2 passed in 0.1s ==\n'), stderr: out('') }, content: [] }],
    [{ callId: 'b2', name: 'bash', arguments: pytest }, { isError: false, value: { kind: 'foreground', exitCode: null, timedOut: true, stdout: out('partial'), stderr: out('') }, content: [] }],
    [{ callId: 'b3', name: 'bash', arguments: pytest }, { isError: true, error: { message: 'tool call aborted', info: { name: 'AbortError', code: 'ABORTED' } }, content: [] }],
    [{ callId: 'b4', name: 'bash', arguments: pytest }, { isError: false, value: { kind: 'promoted', jobId: 'job-7', cwd: CWD, timeoutMs: 1000, output: '' }, content: [] }],
    [{ callId: 'r1', name: 'read', arguments: { file_path: '/work/app/a.py' } }, { isError: false, value: {}, content: [{ type: 'text', text: '1\tx = 1\n' }] }],
    [{ callId: 'e1', name: 'edit', arguments: { file_path: '/work/app/a.py', old_string: 'y', new_string: 'z' } }, { isError: true, error: { message: 'old_string not found' }, content: [] }],
  ] as const

  for (const [exec, result] of results) host.emit('tools/result', { ...exec, agent }, result)
  await until(() => rkm.calls().length === results.length)

  assert.deepEqual(rkm.calls().map(call => call.input), [
    { ...base('PostToolUse'), tool_name: 'Bash', tool_use_id: 'b1', tool_input: pytest, tool_response: { stdout: '== 2 passed in 0.1s ==\n', stderr: '', exitCode: 0 } },
    { ...base('PostToolUse'), tool_name: 'Bash', tool_use_id: 'b2', tool_input: pytest, tool_response: { stdout: 'partial', stderr: '' } },
    { ...base('PostToolUseFailure'), tool_name: 'Bash', tool_use_id: 'b3', tool_input: pytest, error: 'tool call aborted', is_interrupt: true },
    { ...base('PostToolUse'), tool_name: 'Bash', tool_use_id: 'b4', tool_input: { ...pytest, run_in_background: true }, tool_response: { backgroundTaskId: 'job-7' } },
    { ...base('PostToolUse'), tool_name: 'Read', tool_use_id: 'r1', tool_input: { file_path: '/work/app/a.py' }, tool_response: '1\tx = 1\n' },
    { ...base('PostToolUseFailure'), tool_name: 'Edit', tool_use_id: 'e1', tool_input: { file_path: '/work/app/a.py', old_string: 'y', new_string: 'z' }, error: 'old_string not found' },
  ])
})

test('SessionEnd reports the session tokens and those spent before the first write', async (t) => {
  const rkm = fakeRkaMemory(t)
  rkm.reply({ SessionStart: { stdout: PACKET_A } })
  const host = fakeHost({ command: rkm.command })
  const writer = fakeAgent('s1', CWD)
  const reader = fakeAgent('s2', CWD)
  await host.emit('agent/created', { agent: writer, source: 'startup' })
  await host.emit('agent/created', { agent: reader, source: 'startup' })
  const message = (usage?: Record<string, number>) => ({ type: 'assistant/message', data: { usage } })
  const call = (name: string) => ({ type: 'tool/call', data: { name } })

  for (const event of [
    message({ inputTokens: 100, outputTokens: 20, cacheReadTokens: 30, cacheWriteTokens: 5 }),
    call('bash'),
    message({ inputTokens: 40, outputTokens: 10 }),
    call('write'),
    message(),
    message({ inputTokens: 50, outputTokens: 0 }),
    call('edit'),
  ]) host.emit('session/event', writer.session, event)
  host.emit('session/event', reader.session, message({ inputTokens: 60, outputTokens: 10 }))
  host.emit('session/event', { id: 'unknown' }, message({ inputTokens: 999, outputTokens: 1 }))

  host.emit('agent/disposed', { agent: writer })
  host.emit('agent/disposed', { agent: reader })
  await until(() => rkm.calls().filter(c => c.args[1] === 'SessionEnd').length === 2)
  await until(() => host.systemPrompt(writer) === '')

  assert.deepEqual(rkm.calls().filter(c => c.args[1] === 'SessionEnd').map(c => c.input).sort((a, b) => String(a!.session_id).localeCompare(String(b!.session_id))), [
    { ...base('SessionEnd'), reason: 'disposed', usage: { tokens_total: 255, tokens_before_first_write: 155, first_write_seen: true } },
    { ...base('SessionEnd'), session_id: 's2', reason: 'disposed', usage: { tokens_total: 70, tokens_before_first_write: 70, first_write_seen: false } },
  ])
})

test('a hanging rka-memory is killed at the timeout and dsh carries on', async (t) => {
  const stderr = captureStderr(t)
  const rkm = fakeRkaMemory(t)
  rkm.reply({ SessionStart: { sleepMs: 60_000, stdout: PACKET_A }, skills: { stdout: [] } })
  const host = fakeHost({ command: rkm.command, timeoutMs: 300 })
  const agent = fakeAgent('s1', CWD)
  const started = Date.now()

  await host.emit('agent/created', { agent, source: 'startup' })

  assert.ok(Date.now() - started < 5_000, `agent/created took ${Date.now() - started}ms`)
  assert.equal(host.systemPrompt(agent), '')
  assert.equal(stderr(), 'rka-memory: hook SessionStart timed out after 300ms\n')
  assert.deepEqual(rkm.calls().map(call => call.args), [['skills', '--session', 's1', '--cwd', CWD]])
})

test('a missing rka-memory warns once per call kind and never throws', async (t) => {
  const stderr = captureStderr(t)
  const host = fakeHost({ command: ['/nonexistent/rka-memory'] })
  for (const id of ['s1', 's2']) {
    const agent = fakeAgent(id, CWD)
    await host.emit('agent/created', { agent, source: 'startup' })
    const typed = userMessage(`${id}-m1`, 'hello')
    assert.deepEqual(await host.emit('agent/pre-step', { agent, messages: [typed] }, enter([typed])), { kind: 'enter', messages: [typed] })
  }

  assert.equal(stderr(), [
    'rka-memory: hook SessionStart spawn /nonexistent/rka-memory ENOENT\n',
    'rka-memory: skills spawn /nonexistent/rka-memory ENOENT\n',
    'rka-memory: hook UserPromptSubmit spawn /nonexistent/rka-memory ENOENT\n',
  ].join(''))
})

test('without a skills service the session still starts and the gap is noted once', async (t) => {
  const stderr = captureStderr(t)
  const rkm = fakeRkaMemory(t)
  rkm.reply({ SessionStart: { stdout: PACKET_A }, skills: { stdout: [PROC_SKILL] } })
  const host = fakeHost({ command: rkm.command })
  const agents = [fakeAgent('s1', CWD, false), fakeAgent('s2', CWD, false)]

  for (const agent of agents) await host.emit('agent/created', { agent, source: 'startup' })

  assert.deepEqual(agents.map(agent => host.systemPrompt(agent)), ['PACKET-A', 'PACKET-A'])
  assert.deepEqual(rkm.calls().map(call => call.args[1]), ['SessionStart', 'SessionStart'])
  assert.equal(stderr(), 'rka-memory: this dsh composition has no skills service, so rka-memory skills are not registered\n')
})

test('unloading the plugin removes the skills it registered', async (t) => {
  const rkm = fakeRkaMemory(t)
  rkm.reply({ skills: { stdout: [PROC_SKILL, TEST_SKILL] } })
  const host = fakeHost({ command: rkm.command })
  const agent = fakeAgent('s1', CWD)
  await host.emit('agent/created', { agent, source: 'startup' })
  assert.deepEqual(agent.skills.map(skill => skill.name), ['rka-r-proc', 'rka-r-test'])

  host.unload()

  assert.deepEqual(agent.skills, [])
})

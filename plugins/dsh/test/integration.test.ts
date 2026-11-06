import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { test } from 'node:test'
import type { TestContext } from 'node:test'
import { captureStderr, fakeAgent, fakeHost, until, userMessage } from './fakes.ts'

const REPO = resolve(import.meta.dirname, '../../..')
const VENV_BIN = join(REPO, '.venv', 'bin')

interface LedgerEvent { session_id: string; kind: string; actor: string; payload: Record<string, any> }

const SEED_VALIDATED_RECORD = `
import sys
sys.path.insert(0, sys.argv[1])
from ledger_builder import Ledger
from rka_memory.service import Memory
lg = Ledger()
lg.start("g1", project=sys.argv[2], toolchain="uv", user_context="default")
lg.propose("g1", "r_uv", content="Use uv")
lg.link("r_uv", lg.test("g1", True))
memory = Memory.open()
memory.append(*lg.events)
memory.gate()
print(memory.world().records["r_uv"].rung.value)
`

/**
 * SessionStart and SessionEnd start a detached `rka-memory mine` that creates
 * files in the memory dir. Removing the dir before that run reports in
 * `background.log` would leave the recreated dir behind.
 */
async function removeAfterBackgroundMine(root: string): Promise<void> {
  const log = join(root, 'memory', 'background.log')
  const deadline = Date.now() + 10_000
  while (Date.now() < deadline && !(existsSync(log) && readFileSync(log, 'utf8').endsWith('}\n'))) {
    await new Promise(resolve => setTimeout(resolve, 50))
  }
  rmSync(root, { recursive: true, force: true })
}

/** A private rka-memory home and a git project with a uv lockfile, both under one temp dir. */
function realMemory(t: TestContext) {
  assert.ok(existsSync(join(VENV_BIN, 'rka-memory')), `${VENV_BIN}/rka-memory is missing; run \`uv sync\` in ${REPO}`)
  const root = realpathSync(mkdtempSync(join(tmpdir(), 'rkm-dsh-')))
  t.after(() => removeAfterBackgroundMine(root))
  const config = join(root, 'config.yaml')
  writeFileSync(config, [
    'packet: {arms: [gated]}',
    'miner: {backend: none}',
    'core: {sync_on_session_end: false}',
    `skills: {dir: ${JSON.stringify(join(root, 'skills'))}}`,
  ].join('\n'))
  const project = join(root, 'proj')
  mkdirSync(project)
  execFileSync('git', ['init', '-q', project])
  writeFileSync(join(project, 'uv.lock'), '')
  const env = {
    RKA_MEMORY_DIR: join(root, 'memory'),
    RKA_MEMORY_CONFIG: config,
    RKA_MEMORY_CONTEXT: 'default',
    RKA_MEMORY_DISABLE: '0',
  }
  return {
    project,
    command: ['/usr/bin/env', ...Object.entries(env).map(([key, value]) => `${key}=${value}`), join(VENV_BIN, 'rka-memory')],
    seed(): string {
      return execFileSync(join(VENV_BIN, 'python'), ['-c', SEED_VALIDATED_RECORD, join(REPO, 'tests'), project], {
        env: { ...process.env, ...env },
        encoding: 'utf8',
      }).trim()
    },
    ledger(session: string): LedgerEvent[] {
      const file = join(env.RKA_MEMORY_DIR, 'ledger.jsonl')
      if (!existsSync(file)) return []
      return readFileSync(file, 'utf8').split('\n').filter(Boolean)
        .map(line => JSON.parse(line) as LedgerEvent)
        .filter(event => event.session_id === session)
    },
  }
}

test('a dsh session is recorded in the rka-memory ledger', async (t) => {
  const memory = realMemory(t)
  const host = fakeHost({ command: memory.command })
  const agent = fakeAgent('dsh-1', memory.project)
  const allow = async () => ({ kind: 'allow' })
  const pytest = { command: 'pytest', description: 'Run tests' }

  await host.emit('agent/created', { agent, source: 'startup' })
  const prompt = userMessage('msg-1', 'fix the failing parser test')
  await host.emit('agent/pre-step', { agent, messages: [prompt] }, async () => ({ kind: 'enter', messages: [prompt] }))
  const target = join(memory.project, 'parser.py')
  const write = { callId: 'w1', name: 'write', arguments: { file_path: target, content: 'x = 1\n' }, agent }
  await host.emit('tools/pre-execute', write, allow)
  writeFileSync(target, 'x = 1\n')
  host.emit('tools/result', write, { isError: false, value: {}, content: [{ type: 'text', text: 'Wrote parser.py' }] })
  host.emit('tools/result', { callId: 'b1', name: 'bash', arguments: pytest, agent }, {
    isError: false,
    value: { kind: 'foreground', exitCode: 0, stdout: { text: '== 2 passed in 0.1s ==', truncated: false }, stderr: { text: '', truncated: false } },
    content: [{ type: 'text', text: '== 2 passed in 0.1s ==' }],
  })
  host.emit('tools/result', { callId: 'b2', name: 'bash', arguments: pytest, agent }, {
    isError: true,
    error: { message: 'tool call aborted', info: { name: 'AbortError', code: 'ABORTED' } },
    content: [{ type: 'text', text: 'tool call aborted' }],
  })
  for (const event of [
    { type: 'assistant/message', data: { usage: { inputTokens: 1000, outputTokens: 200 } } },
    { type: 'assistant/message', data: { usage: { inputTokens: 300, outputTokens: 100 } } },
    { type: 'tool/call', data: { name: 'write' } },
    { type: 'assistant/message', data: { usage: { inputTokens: 400, outputTokens: 50, cacheReadTokens: 900 } } },
  ]) host.emit('session/event', agent.session, event)
  host.emit('agent/disposed', { agent })
  await until(() => memory.ledger('dsh-1').some(event => event.kind === 'session_end'), 60_000)

  const events = memory.ledger('dsh-1')
  const outcome = (event: LedgerEvent): [string, Record<string, unknown>] | [] => {
    const p = event.payload
    switch (event.kind) {
      case 'session_start': return [event.kind, { harness: p.harness, source: p.source, model: p.model, toolchain: p.state.toolchain, project: p.state.project }]
      case 'user_msg': return [event.kind, { text: p.text, prompt_id: p.prompt_id }]
      case 'file_write': return [event.kind, { tool: p.tool, path: p.path, pre_hash: p.pre_hash }]
      case 'tool_result': return [event.kind, { tool: p.tool, tool_use_id: p.tool_use_id, ok: p.ok }]
      case 'exit_code': return [event.kind, { code: p.code, command: p.command }]
      case 'test_result': return [event.kind, { passed: p.passed, command: p.command }]
      case 'session_end': return [event.kind, { reason: p.reason, tokens_total: p.tokens_total, tokens_before_first_write: p.tokens_before_first_write, first_write_seen: p.first_write_seen }]
      default: return []
    }
  }
  assert.deepEqual(events.map(outcome).filter(entry => entry.length > 0), [
    ['session_start', { harness: 'dsh', source: 'startup', model: 'deepseek-v4', toolchain: 'uv', project: memory.project }],
    ['user_msg', { text: 'fix the failing parser test', prompt_id: 'msg-1' }],
    ['tool_result', { tool: 'Write', tool_use_id: 'w1', ok: true }],
    ['file_write', { tool: 'Write', path: target, pre_hash: 'absent' }],
    ['tool_result', { tool: 'Bash', tool_use_id: 'b1', ok: true }],
    ['exit_code', { code: 0, command: 'pytest' }],
    ['test_result', { passed: true, command: 'pytest' }],
    ['tool_result', { tool: 'Bash', tool_use_id: 'b2', ok: false }],
    ['session_end', { reason: 'disposed', tokens_total: 2950, tokens_before_first_write: 1200, first_write_seen: true }],
  ])
})

test('rkm reject typed in dsh records a user verdict and never reaches the model', async (t) => {
  const stderr = captureStderr(t)
  const memory = realMemory(t)
  assert.equal(memory.seed(), 'L1')
  const host = fakeHost({ command: memory.command })
  const agent = fakeAgent('dsh-2', memory.project)
  let entered = false

  await host.emit('agent/created', { agent, source: 'startup' })
  const decision = await host.emit('agent/pre-step', { agent, messages: [userMessage('msg-1', 'rkm reject r_uv wrong')] }, async () => {
    entered = true
    return { kind: 'enter', messages: [] }
  })

  assert.deepEqual(decision, { kind: 'reject' })
  assert.equal(entered, false)
  assert.equal(stderr(), 'rka-memory recorded reject for r_uv; now L1\n')
  const events = memory.ledger('dsh-2')
  assert.deepEqual(events.filter(event => event.kind === 'user_reject').map(event => [event.actor, event.payload]), [
    ['user', { record_id: 'r_uv', reason: 'wrong' }],
  ])
  assert.deepEqual(events.filter(event => event.kind === 'user_msg'), [])
})

/**
 * rka-memory for DeepSeek Harness: records the session into the rka-memory
 * ledger, keeps the memory packet in the system prompt and registers the L3
 * skills valid for the session. Every dsh event becomes one Claude Code hook
 * call to the `rka-memory` CLI (`rka-memory hook <Event>`, payload on stdin).
 * @module dsh-plugin-rka-memory
 */

import { spawn } from 'node:child_process'
import type { Context } from '@deepseek-ai/cordis'
import type { Agent, PreStepDecision } from '@deepseek-ai/dsh-agent'
import type { ContentBlock } from '@deepseek-ai/dsh-llm'
import type { Session, SessionEvent, UserMessage } from '@deepseek-ai/dsh-session'
import type { PreToolDecision, ToolExecution, ToolExecutionResult } from '@deepseek-ai/dsh-tools'
import type {} from '@deepseek-ai/dsh-skill'
import type {} from '@deepseek-ai/dsh-system-prompt'
import type {} from '@deepseek-ai/dsh-working-directory'

export const name = 'rka-memory'
export const inject = ['systemPrompt']

export interface Config {
  /** argv that starts rka-memory, for example `['uv', 'run', '--project', '/src/rka-memory', 'rka-memory']`. */
  command: string[]
  /** A call still running after this many milliseconds is killed and treated as having printed nothing. */
  timeoutMs: number
}

const DEFAULTS: Config = { command: ['rka-memory'], timeoutMs: 30_000 }

interface ConfigIssue { message: string; path: string[] }

function parseConfig(input: unknown): { value: Config } | { issues: ConfigIssue[] } {
  if (input === undefined || input === null) return { value: DEFAULTS }
  if (!isRecord(input)) return { issues: [{ message: 'expected an object', path: [] }] }
  const issues: ConfigIssue[] = []
  const command = input.command ?? DEFAULTS.command
  if (!Array.isArray(command) || command.length === 0 || !command.every(arg => typeof arg === 'string' && arg !== '')) {
    issues.push({ message: 'expected a non-empty array of non-empty strings', path: ['command'] })
  }
  const timeoutMs = input.timeoutMs ?? DEFAULTS.timeoutMs
  if (typeof timeoutMs !== 'number' || !Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    issues.push({ message: 'expected a positive number', path: ['timeoutMs'] })
  }
  return issues.length > 0 ? { issues } : { value: { command: command as string[], timeoutMs: timeoutMs as number } }
}

/**
 * dsh accepts any Standard Schema v1 validator as `Config`. Using
 * `@deepseek-ai/schemastery` would make it a runtime dependency, so the plugin
 * ships this validator instead. `dsh --dump-config-schema` lists it as unsupported.
 */
export const Config = {
  '~standard': {
    version: 1 as const,
    vendor: 'rka-memory',
    validate: parseConfig,
  },
}

/** dsh tool name to the Claude Code tool name the rka-memory adapter understands. */
const CLAUDE_TOOL_NAMES: Readonly<Record<string, string>> = {
  bash: 'Bash',
  write: 'Write',
  edit: 'Edit',
  read: 'Read',
  glob: 'Glob',
  grep: 'Grep',
  apply_patch: 'ApplyPatch',
}

/** File-writing tools. rka-memory hashes their target before they run, and the first call ends the pre-write token count. */
const FILE_WRITING_TOOLS: ReadonlySet<string> = new Set(['write', 'edit', 'apply_patch'])

/** External contributors pick any finite order. 9000 follows the tool sections and precedes structured output (9900) and the persona suffix (10200). */
const SECTION_ORDER = 9000

type HookEvent = 'SessionStart' | 'UserPromptSubmit' | 'PreToolUse' | 'PostToolUse' | 'PostToolUseFailure' | 'SessionEnd'

/** What a hook call told the plugin to do. */
type HookReply =
  | { kind: 'none' }
  | { kind: 'context'; text: string }
  | { kind: 'block'; reason: string }

interface RkaSkill { name: string; description: string; body: string }

/** Token counts for one session, in the shape Claude Code transcripts give rka-memory. */
interface Usage {
  total: number
  /** Total before the latest assistant message. Like a Claude Code transcript, the message that calls the first write is not counted before it. */
  beforeLatestMessage: number
  beforeFirstWrite: number | undefined
}

interface SessionState {
  /** Tail of the session call chain. Each rka-memory call for the session starts after the previous one settles. */
  queue: Promise<unknown>
  packet: string
  /** rka-memory classifies the task from the first prompt, which can change the skills valid for the session. */
  prompted: boolean
  usage: Usage
  skills: Array<() => void>
}

type BashValue =
  | { kind: 'foreground'; exitCode: number | null; stdout: { text: string }; stderr: { text: string } }
  | { kind: 'promoted' | 'background'; jobId: string }

type RunResult = { stdout: string } | { failure: string }

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function textOf(content: readonly ContentBlock[]): string {
  return content.flatMap(block => block.type === 'text' ? [block.text] : []).join('')
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function run(command: readonly string[], args: readonly string[], input: string, timeoutMs: number): Promise<RunResult> {
  return new Promise((resolve) => {
    const [file, ...prefix] = command
    const child = spawn(file!, [...prefix, ...args], { stdio: ['pipe', 'pipe', 'pipe'] })
    const stdout: Buffer[] = []
    let settled = false
    const settle = (result: RunResult): void => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(result)
    }
    const timer = setTimeout(() => {
      child.kill()
      // Resolve now: a grandchild that inherited stdout would hold `close` back.
      child.stdout.destroy()
      child.stderr.destroy()
      settle({ failure: `timed out after ${timeoutMs}ms` })
    }, timeoutMs)
    child.stdout.on('data', (chunk: Buffer) => stdout.push(chunk))
    child.stderr.resume()
    child.stdin.on('error', () => {})
    child.on('error', error => settle({ failure: error.message }))
    child.on('close', (code, signal) => {
      if (code === 0) settle({ stdout: Buffer.concat(stdout).toString('utf8') })
      else settle({ failure: code === null ? `was killed by ${signal}` : `exited with code ${code}` })
    })
    child.stdin.end(input)
  })
}

function parseReply(value: unknown): HookReply {
  if (!isRecord(value)) return { kind: 'none' }
  if (value.decision === 'block') {
    return { kind: 'block', reason: typeof value.reason === 'string' ? value.reason : 'rka-memory blocked this prompt' }
  }
  const specific = value.hookSpecificOutput
  const text = isRecord(specific) ? specific.additionalContext : undefined
  return typeof text === 'string' && text !== '' ? { kind: 'context', text } : { kind: 'none' }
}

function parseSkills(value: unknown): RkaSkill[] | undefined {
  if (!Array.isArray(value)) return undefined
  return value.flatMap((item): RkaSkill[] => {
    if (!isRecord(item)) return []
    const { name, description, body } = item
    return typeof name === 'string' && typeof description === 'string' && typeof body === 'string'
      ? [{ name, description, body }]
      : []
  })
}

/** The PostToolUse or PostToolUseFailure call for one finished dsh tool call. */
function toolOutcome(tool: string, args: unknown, result: Readonly<ToolExecutionResult>): { event: HookEvent; fields: Record<string, unknown> } {
  const toolInput = isRecord(args) ? args : {}
  if (result.isError) {
    // A dsh bash failure has no exit status: the command was invalid, denied or
    // aborted. Unflagged, the adapter would read the failure as exit code 1.
    return {
      event: 'PostToolUseFailure',
      fields: { tool_input: toolInput, error: result.error.message, ...tool === 'bash' ? { is_interrupt: true } : {} },
    }
  }
  if (tool !== 'bash') return { event: 'PostToolUse', fields: { tool_input: toolInput, tool_response: textOf(result.content) } }
  const value = result.value as BashValue
  if (value.kind === 'foreground') {
    return {
      event: 'PostToolUse',
      fields: {
        tool_input: toolInput,
        tool_response: {
          stdout: value.stdout.text,
          stderr: value.stderr.text,
          ...value.exitCode === null ? {} : { exitCode: value.exitCode },
        },
      },
    }
  }
  return {
    event: 'PostToolUse',
    fields: { tool_input: { ...toolInput, run_in_background: true }, tool_response: { backgroundTaskId: value.jobId } },
  }
}

function isSubagent(agent: Agent): boolean {
  const header = agent.session.header
  return header.parentSession !== undefined || header.origin === 'subagent'
}

function usageReport(usage: Usage): Record<string, unknown> {
  return {
    tokens_total: usage.total,
    tokens_before_first_write: usage.beforeFirstWrite ?? usage.total,
    first_write_seen: usage.beforeFirstWrite !== undefined,
  }
}

export function apply(ctx: Context, config: Partial<Config> = {}): void {
  const command = config.command ?? DEFAULTS.command
  const timeoutMs = config.timeoutMs ?? DEFAULTS.timeoutMs
  const sessions = new Map<string, SessionState>()
  const reported = new Set<string>()

  function warn(message: string): void {
    if (reported.has(message)) return
    reported.add(message)
    process.stderr.write(`rka-memory: ${message}\n`)
  }

  async function guard<T>(label: string, task: () => Promise<T>): Promise<T | undefined> {
    try {
      return await task()
    } catch (error) {
      warn(`${label} failed: ${errorText(error)}`)
      return undefined
    }
  }

  async function invoke(label: string, args: readonly string[], input: string): Promise<unknown> {
    const result = await run(command, args, input, timeoutMs)
    if ('failure' in result) {
      warn(`${label} ${result.failure}`)
      return undefined
    }
    const text = result.stdout.trim()
    if (text === '') return undefined
    try {
      return JSON.parse(text)
    } catch {
      warn(`${label} printed output that is not JSON`)
      return undefined
    }
  }

  async function callHook(event: HookEvent, payload: Record<string, unknown>): Promise<HookReply> {
    return parseReply(await invoke(`hook ${event}`, ['hook', event], JSON.stringify(payload)))
  }

  function sessionOf(id: string): SessionState {
    let state = sessions.get(id)
    if (state === undefined) {
      state = {
        queue: Promise.resolve(),
        packet: '',
        prompted: false,
        usage: { total: 0, beforeLatestMessage: 0, beforeFirstWrite: undefined },
        skills: [],
      }
      sessions.set(id, state)
    }
    return state
  }

  function enqueue<T>(state: SessionState, task: () => Promise<T>): Promise<T> {
    const next = state.queue.then(task)
    state.queue = next.catch(() => {})
    return next
  }

  function cwdOf(agent: Agent): string {
    return ctx.get('workingDirectory')?.get(agent.session) ?? agent.session.header.cwd ?? process.cwd()
  }

  function payload(agent: Agent, event: HookEvent, fields: Record<string, unknown>): Record<string, unknown> {
    return { session_id: agent.id, cwd: cwdOf(agent), transcript_path: '', harness: 'dsh', hook_event_name: event, ...fields }
  }

  function hook(agent: Agent, event: HookEvent, fields: Record<string, unknown>): Promise<HookReply> {
    const body = payload(agent, event, fields)
    return enqueue(sessionOf(agent.id), () => callHook(event, body))
  }

  async function reloadSkills(agent: Agent, state: SessionState): Promise<void> {
    // Cordis refuses `agent.ctx.skills` without declaring the service in
    // `inject`; `get` reads it optionally, so compositions without skills still load.
    const registry = agent.ctx?.get('skills')
    if (registry === undefined) {
      warn('this dsh composition has no skills service, so rka-memory skills are not registered')
      return
    }
    const args = ['skills', '--session', agent.id, '--cwd', cwdOf(agent)]
    const skills = parseSkills(await enqueue(state, () => invoke('skills', args, '')))
    if (skills === undefined) return
    for (const dispose of state.skills.splice(0)) dispose()
    for (const skill of skills) {
      try {
        state.skills.push(registry.register({
          name: skill.name,
          description: skill.description,
          content: skill.body,
          source: 'rka-memory',
          invocation: { modelInvocable: true, userInvocable: true },
        }))
      } catch (error) {
        warn(`dsh rejected skill ${skill.name}: ${errorText(error)}`)
      }
    }
  }

  ctx.systemPrompt.section({
    name: 'rka-memory',
    order: SECTION_ORDER,
    interpolate: false,
    text: context => (context.agent === undefined ? undefined : sessions.get(context.agent.id)?.packet) ?? '',
  })

  ctx.effect(() => () => {
    for (const state of sessions.values()) {
      for (const dispose of state.skills.splice(0)) dispose()
    }
    sessions.clear()
  })

  ctx.on('agent/created', async ({ agent, source }) => {
    await guard('SessionStart', async () => {
      const state = sessionOf(agent.id)
      const reply = await hook(agent, 'SessionStart', {
        source,
        model: agent.model.current()?.model,
        parent_session: agent.session.header.parentSession,
      })
      state.packet = reply.kind === 'context' ? reply.text : ''
      await reloadSkills(agent, state)
    })
  })

  ctx.on('agent/pre-step', async ({ agent, messages }, next): Promise<PreStepDecision> => {
    // A subagent's prompt is written by its parent model even though dsh marks
    // it source.kind 'user'. Forwarding it would record model text as a user
    // message, or let the model type its own `rkm confirm`.
    if (isSubagent(agent)) return next()
    const typed = messages.filter((message: UserMessage) => message.source.kind === 'user')
    if (typed.length === 0) return next()
    const rejected = await guard('UserPromptSubmit', async () => {
      const state = sessionOf(agent.id)
      const prompt = typed.map(message => textOf(message.content)).join('\n')
      const reply = await hook(agent, 'UserPromptSubmit', { prompt, prompt_id: typed[0]!.id })
      if (reply.kind === 'block') {
        process.stderr.write(`${reply.reason}\n`)
        return true
      }
      if (reply.kind === 'context') state.packet = state.packet === '' ? reply.text : `${state.packet}\n\n${reply.text}`
      if (!state.prompted) {
        state.prompted = true
        await reloadSkills(agent, state)
      }
      return false
    })
    return rejected === true ? { kind: 'reject' } : next()
  })

  ctx.on('tools/pre-execute', async (exec: ToolExecution, next): Promise<PreToolDecision> => {
    const agent = exec.agent
    if (agent !== undefined && FILE_WRITING_TOOLS.has(exec.name)) {
      await guard('PreToolUse', () => hook(agent, 'PreToolUse', {
        tool_name: CLAUDE_TOOL_NAMES[exec.name] ?? exec.name,
        tool_input: exec.arguments,
        tool_use_id: exec.callId,
      }))
    }
    return next()
  })

  ctx.on('tools/result', (exec: Readonly<ToolExecution>, result: Readonly<ToolExecutionResult>) => {
    const agent = exec.agent
    if (agent === undefined) return
    void guard('PostToolUse', () => {
      const { event, fields } = toolOutcome(exec.name, exec.arguments, result)
      return hook(agent, event, { tool_name: CLAUDE_TOOL_NAMES[exec.name] ?? exec.name, tool_use_id: exec.callId, ...fields })
    })
  })

  ctx.on('session/event', (session: Session, event: SessionEvent) => {
    const usage = sessions.get(session.id)?.usage
    if (usage === undefined) return
    if (event.type === 'assistant/message') {
      const { inputTokens = 0, outputTokens = 0, cacheReadTokens = 0, cacheWriteTokens = 0 } = event.data.usage ?? {}
      usage.beforeLatestMessage = usage.total
      usage.total += inputTokens + outputTokens + cacheReadTokens + cacheWriteTokens
    } else if (event.type === 'tool/call' && usage.beforeFirstWrite === undefined && FILE_WRITING_TOOLS.has(event.data.name)) {
      usage.beforeFirstWrite = usage.beforeLatestMessage
    }
  })

  ctx.on('agent/disposed', ({ agent }) => {
    void guard('SessionEnd', async () => {
      const state = sessionOf(agent.id)
      await hook(agent, 'SessionEnd', { reason: 'disposed', usage: usageReport(state.usage) })
      if (sessions.get(agent.id) === state) sessions.delete(agent.id)
    })
  })
}

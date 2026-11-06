import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type { TestContext } from 'node:test'
import { apply } from '../src/index.ts'
import type { Config } from '../src/index.ts'

const FAKE_RKA_MEMORY = `
import { appendFileSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
const dir = dirname(process.argv[1])
const args = process.argv.slice(2)
const input = readFileSync(0, 'utf8')
const replies = JSON.parse(readFileSync(join(dir, 'replies.json'), 'utf8'))
const reply = replies[args[0] === 'hook' ? args[1] : args[0]] ?? {}
if (reply.sleepMs) await new Promise(resolve => setTimeout(resolve, reply.sleepMs))
appendFileSync(join(dir, 'calls.jsonl'), JSON.stringify({ args, input: input === '' ? null : JSON.parse(input) }) + '\\n')
if (reply.stdout !== undefined) process.stdout.write(JSON.stringify(reply.stdout))
`

export interface Call { args: string[]; input: Record<string, unknown> | null }
export interface Reply { stdout?: unknown; sleepMs?: number }

/** A stand-in rka-memory CLI that logs each call (after its optional delay) and prints a canned reply per event. */
export function fakeRkaMemory(t: TestContext) {
  const dir = mkdtempSync(join(tmpdir(), 'rkm-fake-'))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const script = join(dir, 'rka-memory.mjs')
  writeFileSync(script, FAKE_RKA_MEMORY)
  writeFileSync(join(dir, 'replies.json'), '{}')
  return {
    command: [process.execPath, script],
    reply(replies: Record<string, Reply>): void {
      writeFileSync(join(dir, 'replies.json'), JSON.stringify(replies))
    },
    calls(): Call[] {
      const file = join(dir, 'calls.jsonl')
      if (!existsSync(file)) return []
      return readFileSync(file, 'utf8').split('\n').filter(Boolean).map(line => JSON.parse(line) as Call)
    },
  }
}

type Listener = (...args: any[]) => any

/** The slice of a dsh Context the plugin touches, recording what it registers. */
export function fakeHost(config: Partial<Config>) {
  const listeners = new Map<string, Listener>()
  const sections: Array<{ text: (context: { agent?: unknown }) => string }> = []
  const disposers: Array<() => void> = []
  const ctx = {
    on(event: string, listener: Listener) {
      listeners.set(event, listener)
      return () => listeners.delete(event)
    },
    get(service: string) {
      return service === 'workingDirectory' ? { get: (session: FakeSession) => session.header.cwd } : undefined
    },
    effect(create: () => () => void) {
      disposers.push(create())
    },
    systemPrompt: {
      section(section: (typeof sections)[number]) {
        sections.push(section)
        return () => {}
      },
    },
  }
  apply(ctx as never, config)
  return {
    emit(event: string, ...args: unknown[]): any {
      return listeners.get(event)!(...args)
    },
    systemPrompt(agent: FakeAgent | undefined): string {
      return sections.map(section => section.text({ agent })).join('')
    },
    unload(): void {
      for (const dispose of disposers) dispose()
    },
  }
}

interface FakeSession { id: string; header: { id: string; cwd: string } }
export type FakeAgent = ReturnType<typeof fakeAgent>

export function fakeAgent(id: string, cwd: string, withSkills = true) {
  const skills: Array<Record<string, unknown>> = []
  return {
    id,
    session: { id, header: { id, cwd } } satisfies FakeSession,
    model: { current: () => ({ provider: 'deepseek', model: 'deepseek-v4' }) },
    ctx: withSkills
      ? {
          skills: {
            register(skill: Record<string, unknown>) {
              skills.push(skill)
              return () => void skills.splice(skills.indexOf(skill), 1)
            },
          },
        }
      : {},
    skills,
  }
}

export function userMessage(id: string, text: string) {
  return { id, role: 'user', content: [{ type: 'text', text }], source: { kind: 'user' } }
}

export function captureStderr(t: TestContext): () => string {
  const chunks: string[] = []
  t.mock.method(process.stderr, 'write', (chunk: unknown) => {
    chunks.push(String(chunk))
    return true
  })
  return () => chunks.join('')
}

export async function until(predicate: () => boolean, timeoutMs = 15_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(`condition not met within ${timeoutMs}ms`)
    await new Promise(resolve => setTimeout(resolve, 25))
  }
}

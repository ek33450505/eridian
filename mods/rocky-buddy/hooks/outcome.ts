import type { Mood } from '../types'

// What a finished tool call or a permission ask makes Rocky feel. Pure: no `$`,
// so it is tested on its own. Lines are fixed strings with at most one number;
// nothing from a tool's output is ever shown.

export type Kind = 'test' | 'commit' | 'push' | 'merge'

export type Outcome = { mood: Mood; detail: string; ttl?: number }

export const MAX_COUNT = 9999
// Only the end of a result is looked at: a summary sits there, and a pattern
// then has a bounded amount of text to chew on (it runs inside a hook).
const TAIL = 20000

// The pattern for each runner's count of failures: a number only is ever kept.
// Every pattern is anchored and bounded (a count of at most 6 digits; the claude
// pattern allows at most 40 spaces or tabs before it, the cargo one at most 200
// characters of gap), so no input makes one backtrack badly.
const countOf = (runner: string, text: string): number | undefined => {
  const first = (re: RegExp) => {
    const m = re.exec(text)
    return m && m[1] !== undefined ? Number(m[1]) : undefined
  }
  const lines = (re: RegExp) => (text.match(re) ?? []).length
  switch (runner) {
    case 'pytest':
    case 'uv':
    case 'npm':
    case 'pnpm':
    case 'yarn':
      return first(/\b(\d{1,6}) failed\b/)
    case 'bats':
      return lines(/^not ok/gm)
    case 'claude':
      return first(/(?:^|\n)[ \t]{0,40}(\d{1,6}) fail\b/)
    case 'cargo':
      return first(/test result: FAILED\.[^\n]{0,200}?\b(\d{1,6}) failed\b/)
    case 'go':
      return lines(/^--- FAIL/gm)
    default:
      return undefined
  }
}

// How many tests failed, if the output says so in a way this knows; a whole
// number of at least 1, else undefined. (outcomeFor shows MAX_COUNT+ above the cap.)
export const failCount = (runner: string, text: string | undefined): number | undefined => {
  if (typeof text !== 'string') return undefined
  const n = countOf(runner, text.slice(-TAIL))
  return n !== undefined && Number.isFinite(n) && n >= 1 ? Math.floor(n) : undefined
}

// The mood a finished call earns, or undefined to leave the usual (working, or
// worry on any other failure).
export const outcomeFor = (
  kind: Kind | undefined,
  runner: string | undefined,
  failed: boolean,
  text: string | undefined,
): Outcome | undefined => {
  if (kind === 'test') {
    if (failed) {
      const n = failCount(runner ?? '', text)
      const said =
        n === undefined ? 'tests fail' : n === 1 ? '1 test fails' : n > MAX_COUNT ? `${MAX_COUNT}+ tests fail` : `${n} tests fail`
      return { mood: 'worry', detail: `worry worry: ${said}` }
    }
    return { mood: 'happy', detail: 'good good good: tests pass' }
  }
  if (failed) return undefined
  if (kind === 'commit') return { mood: 'happy', detail: 'good good: commit' }
  if (kind === 'push') return { mood: 'happy', detail: 'good good good: push' }
  if (kind === 'merge') return { mood: 'happy', detail: 'amaze amaze: merged', ttl: 8000 }
  return undefined
}

// A permission ask that a person answers.
export const ASK: Outcome = { mood: 'thinking', detail: 'question? you decide.', ttl: 60000 }
export const isPersonAsk = (decision: unknown, approver: unknown) => decision === 'ask' && approver === 'person'

// ---- a subagent's report ----

export type AgentStatus = 'DONE' | 'DONE_WITH_CONCERNS' | 'BLOCKED' | 'NEEDS_CONTEXT'

// Only the end of a report is looked at, and only for a status token: the line
// `Status: DONE` (with the markdown forms `**Status:** DONE`) or the JSON member
// `"status": "DONE"`. Patterns are anchored and bounded; the last match wins.
const REPORT_TAIL = 4000
const STATUS_LINE = /(?:^|\n)[ \t]{0,8}\*{0,3}Status\*{0,3}:?\*{0,3}[ \t]{0,8}(DONE_WITH_CONCERNS|DONE|BLOCKED|NEEDS_CONTEXT)\b/g
const STATUS_JSON = /"status"[ \t]{0,4}:[ \t]{0,4}"(DONE_WITH_CONCERNS|DONE|BLOCKED|NEEDS_CONTEXT)"/g

export const agentStatus = (report: unknown): AgentStatus | undefined => {
  if (typeof report !== 'string') return undefined
  const tail = report.slice(-REPORT_TAIL)
  let best: { at: number; status: AgentStatus } | undefined
  for (const re of [STATUS_LINE, STATUS_JSON]) {
    re.lastIndex = 0
    for (let m = re.exec(tail); m !== null; m = re.exec(tail)) {
      if (m[1] !== undefined && (best === undefined || m.index >= best.at)) best = { at: m.index, status: m[1] as AgentStatus }
    }
  }
  return best?.status
}

// The moods a finished agent earns: the line is fixed words and the agent's
// (already clean) type.
export const agentOutcome = (type: string, status: AgentStatus): Outcome => {
  switch (status) {
    case 'DONE':
      return { mood: 'happy', detail: `good good: ${type} done`, ttl: 4000 }
    case 'DONE_WITH_CONCERNS':
      return { mood: 'worry', detail: `hmm hmm: ${type} concerns`, ttl: 10000 }
    default:
      return { mood: 'worry', detail: `worry worry: ${type} blocked` }
  }
}

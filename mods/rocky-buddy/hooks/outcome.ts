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

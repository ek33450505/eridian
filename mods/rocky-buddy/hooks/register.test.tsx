import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On, UiBlitArgs } from 'claude-code'

const PANE = {
  plugin: 'rocky-buddy',
  component: 'Pane',
  requestId: 'rocky',
  props: {
    title: 'Rocky',
    isFocused: false,
    bodyColumns: 40,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 10 },
    view: {},
  },
} as const

const surfaces = ['terminal', 'desktop'] as const

// What the ticker swapped into the picture: every `$.ui.blit` the plugin made.
let blits: UiBlitArgs[] = []
let opened = 0 // panes opened
// Which frame file a blit named: its mood and index.
const picture = (b: UiBlitArgs) => {
  const file = 'source' in b && 'file' in b.source ? b.source.file : ''
  const m = /assets\/frames\/(\w+)-(\d\d)\.png$/.exec(file)
  return m ? { mood: m[1], index: Number(m[2]) } : undefined
}

// `blit` is what `$.ui.blit` answers (an object, read afresh at each call so a
// test can change it), or a hook of its own.
const engine = (
  on: On,
  toolResult: Record<string, unknown> = { result: {} },
  blit: Record<string, unknown> | ((e: UiBlitArgs) => Promise<unknown>) = {},
  home = HOME,
) => {
  blits = []
  opened = 0
  world(on, home)
  on('ui.open', () => {
    opened++
    return { value: { isPlaced: true } } as never
  })
  on('ui.blit', async (_, e) => {
    blits.push(e)
    return { value: typeof blit === 'function' ? await blit(e) : blit } as never
  })
  on('command.register', () => ({ value: { command: 'rocky' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('tool.call', () => toolResult as never)
  on('turn.start', () => ({ turnId: 't1' }))
  on('turn.complete', () => ({ text: '' }))
  return mock.clock(on)
}

const moodMounted = async ($: any, line: RegExp, bodyColumns = 40) => {
  const ui = await $.ui.mount({ ...PANE, props: { ...PANE.props, bodyColumns }, surface: 'desktop' })
  const found = Boolean(await ui.find({ type: 'Text', text: line }))
  await ui.unmount()
  return found
}

const HOME = '/Users/test'
const PERSONAL = `${HOME}/Projects/personal/eridian`
const SESSION = { cwd: PERSONAL, surface: 'desktop', isInteractive: true } as const

// The session's directory as `$.session.cwd()` answers it: a test moves it.
let cwdNow = PERSONAL
let cwdBroken = false // `$.session.cwd()` fails
let missing = new Set<string>() // paths `$.fs.exists` says are not there
let existsBroken = false // `$.fs.exists` fails
let existsCalls = 0 // how often `$.fs.exists` was asked
let holdCwdNth = 0 // the Nth `$.session.cwd()` from now waits until `releaseCwd`
let releaseCwd: () => void = () => {}
// What the guard reads of the world: $HOME, and the session's directory.
// Lets what the plugin left running beside a call (its mood writes) finish.
const settle = async () => {
  const later = (globalThis as { setTimeout?: (fn: () => void, ms: number) => unknown }).setTimeout
  for (let i = 0; i < 6; i++) {
    if (later) await new Promise<void>(resolve => later(resolve, 0))
    else for (let j = 0; j < 200; j++) await Promise.resolve()
  }
}

const world = (on: On, home = HOME) => {
  cwdNow = PERSONAL
  cwdBroken = false
  missing = new Set()
  existsBroken = false
  existsCalls = 0
  on('fs.exists', (_, e) => {
    existsCalls++
    if (existsBroken) throw new Error('network location')
    return { value: !missing.has((e as { path: string }).path) } as never
  })
  mock.env(on, { HOME: home })
  holdCwdNth = 0
  on('session.cwd', async () => {
    if (holdCwdNth > 0 && --holdCwdNth === 0) {
      await new Promise<void>(resolve => {
        releaseCwd = resolve
      })
    }
    if (cwdBroken) throw new Error('the session has no directory')
    return { value: cwdNow } as never
  })
}

const COMPLETE = { reason: 'answer', answer: 'ok', durationMs: 1, isAborted: false } as never

test('a failing tool call shows the worry line, on every surface', async ($, on) => {
  engine(on, { result: {}, isError: true })
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'false' })

  for (const surface of surfaces) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /worry worry worry/ }))?.text).toMatch(/worry worry worry/)
    await ui.unmount()
  }
})

test('a working call names its tool', async ($, on) => {
  engine(on)
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'ls' })

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await ui.find({ type: 'Text', text: /work work: Bash/ })).toBeTruthy()
  await ui.unmount()
})

test('turn.complete is happy, then idle once the decay has passed', async ($, on) => {
  const clock = engine(on)
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.turn.complete({ turnId: 't1', ...(COMPLETE as object) } as never)

  const happy = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await happy.find({ type: 'Text', text: /good good good/ })).toBeTruthy()
  await happy.unmount()

  await clock.advance(4500)

  const idle = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await idle.find({ type: 'Text', text: /good good good/ })).toBeFalsy()
  expect(await idle.find({ type: 'Text', text: /\|\s+\.\.\.\s+\|/ })).toBeTruthy()
  await idle.unmount()
})

test('a stale decay does not clobber a newer mood', async ($, on) => {
  const clock = engine(on)
  await $.session.start(SESSION)
  await $.turn.complete({ turnId: 't1', ...(COMPLETE as object) } as never)
  await clock.advance(1000)
  await $.turn.start({ text: 'again', turnId: 't2' })
  await clock.advance(4000)

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await ui.find({ type: 'Text', text: /question\?/ })).toBeTruthy()
  await ui.unmount()
})

test('every row fits a narrow pane', async ($, on) => {
  engine(on, { result: {}, isError: true })
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'x' })

  for (const surface of surfaces) {
    const ui = await $.ui.mount({ ...PANE, props: { ...PANE.props, bodyColumns: 12 }, surface })
    const rows = await ui.findAll({ type: 'Text' })
    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) {
      expect(typeof row.text).toBe('string')
      expect((row.text as string).length).toBeLessThanOrEqual(12)
    }
    await ui.unmount()
  }
})

test('every bubble row is as wide as the top', async ($, on) => {
  engine(on)
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'x' })

  for (let bodyColumns = 4; bodyColumns <= 60; bodyColumns++) {
    const ui = await $.ui.mount({ ...PANE, props: { ...PANE.props, bodyColumns }, surface: 'desktop' })
    const rows = await ui.findAll({ type: 'Text' })
    const [top, mid, bottom] = rows.map(r => (r.text as string).length)
    expect(rows.length).toBeGreaterThanOrEqual(3)
    expect(mid).toBe(top)
    expect(bottom).toBe(top)
    await ui.unmount()
  }
})

test('a subagent tool call still sets working', async ($, on) => {
  engine(on)
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'ls', agentId: 'a1' })

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await ui.find({ type: 'Text', text: /work work: Bash/ })).toBeTruthy()
  await ui.unmount()
})

for (const answer of [{ result: { ok: 1 } }, { deny: 'no' }]) {
  test(`the tool.call hook returns what next produced: ${Object.keys(answer)[0]}`, async ($, on) => {
    engine(on, answer)
    await $.session.start(SESSION)
    await $.turn.start({ text: 'go', turnId: 't1' })
    const ran = await $.tool.call({ tool: 'Bash', command: 'ls' })
    expect(ran).toEqual(answer)
  })
}

test('a session.start mid-mood resets to idle at once', async ($, on) => {
  engine(on)
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  expect(await moodMounted($, /work work: Bash/)).toBe(true)

  await $.session.start(SESSION)

  // before any tool call
  expect(await moodMounted($, /\|\s+\.\.\.\s+\|/)).toBe(true)
})

test('a session.start after turn.complete resets happy to idle', async ($, on) => {
  engine(on)
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.turn.complete({ turnId: 't1', ...(COMPLETE as object) } as never)
  await $.session.start(SESSION)

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await ui.find({ type: 'Text', text: /good good good/ })).toBeFalsy()
  expect(await ui.find({ type: 'Text', text: /\|\s+\.\.\.\s+\|/ })).toBeTruthy()
  await ui.unmount()
})

test('a tool call that settles after turn.complete leaves happy alone', async ($, on) => {
  world(on)
  const clock = mock.clock(on)
  on('command.register', () => ({ value: { command: 'rocky' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('turn.start', () => ({ turnId: 't1' }))
  on('turn.complete', () => ({ text: '' }))
  // The call is still running when the turn completes, then fails.
  on('tool.call', async () => {
    await $.turn.complete({ turnId: 't1', ...(COMPLETE as object) } as never)
    return { result: {}, isError: true } as never
  })
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'late' })

  expect(await moodMounted($, /good good good/)).toBe(true)
  expect(await moodMounted($, /worry/)).toBe(false)
})

test('a subagent turn.complete moves nothing, and its tool calls still do', async ($, on) => {
  world(on)
  const clock = mock.clock(on)
  on('command.register', () => ({ value: { command: 'rocky' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('turn.start', () => ({ turnId: 't1' }))
  on('turn.complete', () => ({ text: '' }))
  // A subagent's turn ends while a main-turn call is in flight; the call fails.
  on('tool.call', async () => {
    await $.turn.complete({ turnId: 'sub', agentId: 'a1', ...(COMPLETE as object) } as never)
    return { result: {}, isError: true } as never
  })
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'ls' })

  // the subagent's turn.complete neither made Rocky happy nor made the call late
  expect(await moodMounted($, /worry worry worry/)).toBe(true)
  expect(await moodMounted($, /good good good/)).toBe(false)

  await $.tool.call({ tool: 'Bash', command: 'ls', agentId: 'a1' })
  expect(await moodMounted($, /worry worry worry/)).toBe(true)
})

test('a subagent turn.complete followed by a tool call shows work work: Bash', async ($, on) => {
  engine(on)
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.turn.complete({ turnId: 'sub', agentId: 'a1', ...(COMPLETE as object) } as never)
  // the subagent's completion changed nothing
  expect(await moodMounted($, /question\?/)).toBe(true)
  await $.tool.call({ tool: 'Bash', command: 'ls' })

  expect(await moodMounted($, /work work: Bash/)).toBe(true)
})

test('thinking and working decay to idle, and each write pushes the decay out', async ($, on) => {
  const clock = engine(on)
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  expect(await moodMounted($, /question\?/)).toBe(true)
  await clock.advance(100000)
  expect(await moodMounted($, /question\?/)).toBe(true) // a long silent turn is still thinking
  await clock.advance(21000)
  expect(await moodMounted($, /\|\s+\.\.\.\s+\|/)).toBe(true)

  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await clock.advance(15000)
  expect(await moodMounted($, /work work: Bash/)).toBe(true)
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  // 25s after the first write: its decay (20s) has passed, the second's has not
  await clock.advance(10000)
  expect(await moodMounted($, /work work: Bash/)).toBe(true)
  await clock.advance(11000)
  expect(await moodMounted($, /\|\s+\.\.\.\s+\|/)).toBe(true)
})

test('a turn.complete that fails leaves thinking to expire', async ($, on) => {
  world(on)
  const clock = mock.clock(on)
  on('command.register', () => ({ value: { command: 'rocky' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('turn.start', () => ({ turnId: 't1' }))
  on('turn.complete', () => {
    throw new Error('the completion failed')
  })
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  let failed = false
  try {
    await $.turn.complete({ turnId: 't1', ...(COMPLETE as object) } as never)
  } catch {
    failed = true
  }
  expect(failed).toBe(true)
  expect(await moodMounted($, /question\?/)).toBe(true)
  await clock.advance(121000)

  expect(await moodMounted($, /\|\s+\.\.\.\s+\|/)).toBe(true)
})

test('an expired mood draws idle with no write, the stored mood unchanged', async ($, on) => {
  const clock = engine(on)
  const writes: string[] = []
  on('state.set', async (_, e, next) => {
    if ((e as { key?: unknown }).key === 'mood') writes.push(JSON.stringify(e))
    return next(e)
  })
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await clock.advance(0) // the tool call's own writes land
  const moodWrites = () => writes.length
  const before = moodWrites()

  await clock.advance(21000)

  expect(await moodMounted($, /\|\s+\.\.\.\s+\|/)).toBe(true)
  expect(moodWrites()).toBe(before)
  // the last mood written is still working: nothing wrote idle
  expect(writes.length).toBeGreaterThan(0)
  expect(writes[writes.length - 1]).toContain('working')
  expect(writes[writes.length - 1]).not.toContain('idle')
})

test('frame writes keep landing after some are refused', async ($, on) => {
  const clock = engine(on)
  let refuse = 0
  let landed = 0
  on('state.set', async (_, e, next) => {
    if ((e as { key?: unknown }).key !== 'frame') return next(e)
    if (refuse > 0) {
      refuse--
      // a write the host refuses (as while a draw runs): it did not land
      return { value: { isSet: false, version: 0 } }
    }
    landed++
    return next(e)
  })
  await $.session.start(SESSION)
  refuse = 3
  await clock.advance(83 * 15) // the three refused stamps
  const before = landed
  await clock.advance(83 * 50)

  // the ticker went on after the refusals: ~10 more stamps landed
  expect(landed - before).toBeGreaterThanOrEqual(9)
})

const WAKERS = {
  turn: async ($: Engine) => {
    await $.turn.start({ text: 'go', turnId: 't1' })
  },
  'tool call': async ($: Engine) => {
    await $.tool.call({ tool: 'Bash', command: 'ls' })
  },
  '/rocky': async ($: Engine) => {
    await $.command.run({ command: 'rocky', args: '' } as never)
  },
}

for (const [name, wake] of Object.entries(WAKERS)) {
test(`a ticker that has died is started again by a ${name}`, async ($, on) => {
  let periods = 0
  let refused = false
  // Registered before the clock, so it hears each period first: it refuses the
  // 12th period of the first interval (after two stamps), which ends it.
  on('clock.every', { ms: 83 }, async (_, e, next) => {
    periods++
    if (periods === 12) {
      refused = true
      return { deny: 'refused' } as never
    }
    return next(e)
  })
  const clock = engine(on)
  on('command.run', () => ({ text: '' }))
  let landed = 0
  on('state.set', async (_, e, next) => {
    if ((e as { key?: unknown }).key === 'frame') landed++
    return next(e)
  })
  await $.session.start(SESSION)
  await clock.advance(83 * 14)
  expect(refused).toBe(true)
  const stopped = landed
  expect(stopped).toBeGreaterThan(0) // it had been stamping
  await clock.advance(450 * 10)
  expect(landed).toBe(stopped) // the interval is over

  await wake($)
  await clock.advance(450 * 10)
  expect(landed).toBeGreaterThan(stopped + 5) // and it was started again
})
}

test('every mood and frame draws the same height, exactly 20 columns wide', async ($, on) => {
  world(on)
  on('command.register', () => ({ value: { command: 'rocky' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('tool.call', (_, e) => ({ result: {}, ...(e.tool === 'Bash' && 'command' in e && e.command === 'bad' ? { isError: true } : {}) }) as never)
  on('turn.start', () => ({ turnId: 't1' }))
  on('turn.complete', () => ({ text: '' }))
  const clock = mock.clock(on)
  await $.session.start(SESSION)

  const seen: Record<string, Set<number>> = {}
  const widest: number[] = []
  const sample = async (mood: string, line: RegExp) => {
    for (let tick = 0; tick < 6; tick++) {
      const ui = await $.ui.mount({ ...PANE, props: { ...PANE.props, bodyColumns: 20 }, surface: 'desktop' })
      const rows = await ui.findAll({ type: 'Text' })
      expect(await ui.find({ type: 'Text', text: line })).toBeTruthy()
      ;(seen[mood] ??= new Set()).add(rows.length)
      // the art rows follow the three bubble rows
      for (const row of rows.slice(3)) widest.push((row.text as string).length)
      // nothing vertical below the body: no center leg
      const legs = rows.slice(3 + 5)
      expect(legs).toHaveLength(2)
      for (const row of legs) expect(row.text as string).not.toContain('|')
      await ui.unmount()
      await clock.advance(450)
    }
  }

  await sample('idle', /\|\s+\.\.\.\s+\|/)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await sample('thinking', /question\?/)
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await sample('working', /work work: Bash/)
  await $.tool.call({ tool: 'Bash', command: 'bad' })
  // 20 columns clip the bubble's text: the line is read by its start
  await sample('worry', /worry worry/)
  await $.turn.complete({ turnId: 't1', ...(COMPLETE as object) } as never)
  // happy decays after 4s: sampling it takes 6 ticks of 450ms, inside that
  await sample('happy', /good good good/)

  expect(Object.keys(seen)).toHaveLength(5)
  const heights = new Set([...Object.values(seen)].flatMap(h => [...h]))
  expect(heights.size).toBe(1)
  expect(widest.length).toBeGreaterThan(0)
  // the widest art row is the idle frame's first, 20 columns exactly
  expect(Math.max(...widest)).toBe(20)
})

const IDLE_0 = [
  ' \\\\               //',
  '  \\\\   .-"""-.   //',
  "   `-./ # . # \\.-'",
  '      | . # . |',
  '      \\_.-"-._/',
  '      /  /    \\',
  '    _/ _/      \\_',
]

test('the idle frame reaches the pane exactly as drawn, arms and legs', async ($, on) => {
  engine(on)
  await $.session.start(SESSION)

  const ui = await $.ui.mount({ ...PANE, props: { ...PANE.props, bodyColumns: 40 }, surface: 'desktop' })
  const rows = (await ui.findAll({ type: 'Text' })).map(r => r.text)
  await ui.unmount()

  // The bubble takes the first three rows; the art follows, whitespace and all.
  // 40 columns: the art sits 10 columns in, centered
  expect(rows.slice(3)).toEqual(IDLE_0.map(r => ' '.repeat(10) + r))
})

for (const bodyColumns of [16, 15]) {
  test(`a ${bodyColumns}-column pane loses both sides alike, never an arm`, async ($, on) => {
    engine(on)
    await $.session.start(SESSION)

    const ui = await $.ui.mount({ ...PANE, props: { ...PANE.props, bodyColumns }, surface: 'desktop' })
    const art = (await ui.findAll({ type: 'Text' })).map(r => r.text as string).slice(3)
    await ui.unmount()

    // an odd drop takes the extra column from the left (3 left, 2 right at 15),
    // which keeps the body's axis on the pane's middle
    const left = Math.ceil((20 - bodyColumns) / 2)
    expect(art.map(r => r.padEnd(bodyColumns))).toEqual(
      IDLE_0.map(r => r.padEnd(20).slice(left, left + bodyColumns).padEnd(bodyColumns)),
    )
    expect(art.some(r => r.includes('..'))).toBe(false)
  })
}

test('the bubble is centered over the body, its pointer in the middle', async ($, on) => {
  engine(on)
  await $.session.start(SESSION)

  const ui = await $.ui.mount({ ...PANE, props: { ...PANE.props, bodyColumns: 40 }, surface: 'desktop' })
  const [top, , bottom] = (await ui.findAll({ type: 'Text' })).map(r => r.text as string)
  await ui.unmount()

  const lead = (top as string).length - (top as string).trimStart().length
  const width = (top as string).trim().length
  // 40 columns: the body's center is column 20
  expect(lead + Math.floor(width / 2)).toBe(20)
  expect((bottom as string).indexOf('v')).toBe(20)
})

test('at 60 columns the art starts at column 20, the bubble over the body', async ($, on) => {
  engine(on)
  await $.session.start(SESSION)

  const ui = await $.ui.mount({ ...PANE, props: { ...PANE.props, bodyColumns: 60 }, surface: 'desktop' })
  const rows = (await ui.findAll({ type: 'Text' })).map(r => r.text as string)
  await ui.unmount()

  const art = rows.slice(3)
  // idle frame 0: the left hand sits at offset 1 of the art
  expect(art[0]?.search(/\S/)).toBe(21)
  expect(art).toEqual(IDLE_0.map(r => ' '.repeat(20) + r))
  expect((rows[2] as string).indexOf('v')).toBe(30)
})

test('a taller pane puts blank rows on top, centering the bubble and the art', async ($, on) => {
  engine(on)
  await $.session.start(SESSION)

  // 10 rows of content in 20: 5 blank rows above
  const tall = { ...PANE.props, scroll: { offset: 0, bodyRows: 20 } }
  const ui = await $.ui.mount({ ...PANE, props: tall, surface: 'desktop' })
  const rows = (await ui.findAll({ type: 'Text' })).map(r => r.text as string)
  await ui.unmount()

  expect(rows.length).toBe(15)
  expect(rows.slice(0, 5).every(r => r.trim() === '')).toBe(true)
  expect(rows[5]).toMatch(/^\s*\.-/) // the bubble's top
  expect(rows.slice(8)).toEqual(IDLE_0.map(r => ' '.repeat(10) + r))
})

test('a worry that has run out does not block working', async ($, on) => {
  world(on)
  const clock = mock.clock(on)
  on('command.register', () => ({ value: { command: 'rocky' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('turn.start', () => ({ turnId: 't1' }))
  let calls = 0
  let midCall = false
  on('tool.call', async () => {
    calls++
    if (calls === 1) return { result: {}, isError: true } as never
    midCall = await moodMounted($, /work work: Bash/)
    return { result: {} } as never
  })
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'false' })
  expect(await moodMounted($, /worry worry worry/)).toBe(true)

  await clock.advance(21000)
  await $.tool.call({ tool: 'Bash', command: 'ls' })

  // while the second call runs, Rocky works; he does not stay in an old worry
  expect(midCall).toBe(true)
})

test('only one ticker runs, however often the ticker is checked', async ($, on) => {
  let creations = 0
  on('clock.every', { ms: 83 }, async (_, e, next) => {
    creations++
    return next(e)
  })
  const clock = engine(on)
  let landed = 0
  on('state.set', async (_, e, next) => {
    if ((e as { key?: unknown }).key === 'frame') landed++
    return next(e)
  })
  await $.session.start(SESSION)
  await $.session.start(SESSION) // a second start replaces the first ticker
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  // two session.starts made two intervals (one clock.every dispatch each,
  // for its first period); the turn and the tool call made no third
  expect(creations).toBe(2)
  const before = landed
  await clock.advance(83 * 50)

  // one interval, 50 periods, a stamp every fifth: a second ticker would
  // roughly double the 10 writes
  expect(landed - before).toBeLessThanOrEqual(11)
  expect(landed - before).toBeGreaterThanOrEqual(9)
})

test('a clock that fails on the tool path leaves the tool result unchanged', async ($, on) => {
  world(on)
  const answer = { result: { ok: 1 } }
  let failNext = false
  // Our own clock, so one read can be made to fail: the first after failNext
  // is ensureTicker's, which the tool path starts un-awaited; the reads that
  // follow (the mood writes) succeed.
  on('clock.now', () => {
    if (failNext) {
      failNext = false
      throw new Error('the clock is gone')
    }
    return { value: 0 } as never
  })
  // A `clock.every` hook answers each period's wait; it is not a TimerCall and
  // has no `cancel` (the kit refuses an answer that is neither { value } nor
  // { deny }, and a stub that resolved at once would spin). Never resolving
  // keeps the interval from firing at all.
  on('clock.every', () => new Promise<never>(() => {}))
  on('command.register', () => ({ value: { command: 'rocky' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('turn.start', () => ({ turnId: 't1' }))
  on('tool.call', () => answer as never)
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })

  failNext = true
  const ran = await $.tool.call({ tool: 'Bash', command: 'ls' })

  await settle() // Rocky's own work runs beside the call
  expect(failNext).toBe(false) // the failing read was reached
  expect(ran).toEqual(answer)
  // the tool path's own mood writes went through
  expect(await moodMounted($, /work work: Bash/)).toBe(true)
})

// ---- the picture (terminal surface) ----

const TALL = { ...PANE.props, bodyColumns: 40, scroll: { offset: 0, bodyRows: 20 } }

const mountTerminal = async ($: Engine, props: Record<string, unknown> = TALL) =>
  $.ui.mount({ ...PANE, props: props as never, surface: 'terminal' })

test("the terminal draws the picture from a file, keyed rocky, with the mood's first frame", async ($, on) => {
  engine(on)
  await $.session.start(SESSION)

  const idle = await mountTerminal($)
  const image = await idle.find({ type: 'Image', key: 'rocky' })
  // 24 x 10 cells, the picture's 240 x 210 at about 2:1 cells
  expect(image?.props).toMatchObject({ key: 'rocky', columns: 24, rows: 10, alt: '...' })
  const source = image?.props.source as { file: string; format: string }
  expect(source.format).toBe('png')
  expect(source.file.startsWith('/')).toBe(true) // absolute: the plugin's root
  expect(source.file.endsWith('assets/frames/idle-00.png')).toBe(true)
  await idle.unmount()

  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  const working = await mountTerminal($)
  const next = await working.find({ type: 'Image', key: 'rocky' })
  expect((next?.props.source as { file: string }).file.endsWith('assets/frames/working-00.png')).toBe(true)
  expect(next?.props.alt).toBe('work work: Bash')
  // the bubble is still text above it
  expect(await working.find({ type: 'Text', text: /work work: Bash/ })).toBeTruthy()
  await working.unmount()
})

test('the picture shrinks to the pane', async ($, on) => {
  engine(on)
  await $.session.start(SESSION)

  // 10 rows less the bubble's 3 leave 7: 16 columns at about 2:1 cells
  const small = await mountTerminal($, { ...PANE.props, bodyColumns: 30, scroll: { offset: 0, bodyRows: 10 } })
  const image = await small.find({ type: 'Image', key: 'rocky' })
  expect(image?.props).toMatchObject({ columns: 16, rows: 7 })
  await small.unmount()

  const narrow = await mountTerminal($, { ...PANE.props, bodyColumns: 12, scroll: { offset: 0, bodyRows: 20 } })
  expect((await narrow.find({ type: 'Image', key: 'rocky' }))?.props).toMatchObject({ columns: 12 })
  await narrow.unmount()
})

test('a tick blits the next frame file of the mood, in order', async ($, on) => {
  const clock = engine(on)
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  blits = []

  await clock.advance(83 * 4)

  expect(blits).toHaveLength(4)
  expect(blits.every(b => b.requestId === 'rocky' && b.key === 'rocky')).toBe(true)
  const seen = blits.map(b => picture(b))
  expect(seen.every(p => p?.mood === 'working')).toBe(true) // working's frames, not another mood's
  for (let k = 1; k < seen.length; k++) {
    expect(seen[k]?.index).toBe((((seen[k - 1]?.index as number) + 1) % 12))
  }
  expect(blits.every(b => 'source' in b && 'file' in b.source && b.source.format === 'png')).toBe(true)
})

test('the pane and the ticker agree on the frame showing', async ($, on) => {
  const clock = engine(on)
  await $.session.start(SESSION)
  await clock.advance(83 * 5) // the first stamp: the frame counter moves to 1
  const stamped = blits[blits.length - 1]
  expect(stamped && picture(stamped)?.index).toBe(5) // 1 stamp = 5 ticks of frames

  const ui = await mountTerminal($)
  const image = await ui.find({ type: 'Image', key: 'rocky' })
  await ui.unmount()
  // what the pane draws at a stamp is what the ticker blitted at it
  expect((image?.props.source as { file: string }).file.endsWith('idle-05.png')).toBe(true)
})

test('an expired mood is blitted as idle', async ($, on) => {
  const clock = engine(on)
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await clock.advance(21000)
  blits = []

  await clock.advance(83 * 3)

  expect(blits).toHaveLength(3)
  for (const b of blits) expect(picture(b)?.mood).toBe('idle')
})

// The engine's own words for a refused blit (the blit doc in the types).
const ALT = 'an Image drawing its alt there'
const ALT_FILE = 'a file this terminal cannot read'
const NOT_MOUNTED = 'not mounted'
const NOT_MOUNTED_DOC = "Nothing of this plugin's is mounted there" // the doc's own words

for (const words of [ALT, 'the Image draws its alt here', ALT_FILE]) {
test(`two denies in a row (${words}) flip the pane to the ASCII figure`, async ($, on) => {
  const clock = engine(on, { result: {} }, { deny: words })
  await $.session.start(SESSION)

  const before = await mountTerminal($)
  expect(await before.find({ type: 'Image', key: 'rocky' })).toBeTruthy()
  await before.unmount()

  await clock.advance(83 * 12) // the first deny, then the second a stamp later

  const after = await mountTerminal($, { ...PANE.props, bodyColumns: 40, scroll: { offset: 0, bodyRows: 10 } })
  expect(await after.find({ type: 'Image' })).toBeUndefined()
  const rows = (await after.findAll({ type: 'Text' })).map(r => r.text)
  expect(rows.slice(3)).toEqual(IDLE_0.map(r => ' '.repeat(10) + r))
  await after.unmount()
  // and it stops blitting
  const sent = blits.length
  await clock.advance(83 * 20)
  expect(blits.length).toBe(sent)
})
}

test('one such deny does not flip, a broken run does not add up, and a deny that only mentions alt does not count', async ($, on) => {
  const answer: Record<string, unknown> = { deny: ALT }
  const clock = engine(on, { result: {} }, answer)
  await $.session.start(SESSION)
  await clock.advance(83 * 2) // one such deny
  answer.deny = NOT_MOUNTED // breaks the run
  await clock.advance(415 * 2)
  answer.deny = ALT // one again, alone (one tick of the slow ticker)
  await clock.advance(415)
  answer.deny = undefined // a success breaks the run too (two bring the fast rate back)
  await clock.advance(415 * 3)
  answer.deny = ALT // one again, alone
  await clock.advance(83 * 3)
  answer.deny = 'the alt text is empty' // alt in another sense, many times
  await clock.advance(415 * 6)
  answer.deny = undefined // the picture draws
  await clock.advance(415 * 3)
  answer.deny = ALT // one more, alone
  await clock.advance(83 * 3)

  const ui = await mountTerminal($)
  expect(await ui.find({ type: 'Image', key: 'rocky' })).toBeTruthy()
  await ui.unmount()
})

test('while blits are refused the ticker slows to the stamp period, and recovers', async ($, on) => {
  const answer: Record<string, unknown> = { deny: NOT_MOUNTED }
  const clock = engine(on, { result: {} }, answer)
  await $.session.start(SESSION)

  await clock.advance(83 * 20) // 1660 ms
  // the first tick is refused; the rest come one per 415 ms
  expect(blits.length).toBeLessThanOrEqual(5)
  expect(blits.length).toBeGreaterThanOrEqual(3)

  answer.deny = undefined // a pane is mounted: two blits land, then the fast rate is back
  await clock.advance(415 * 3)
  blits = []
  await clock.advance(83 * 20)

  // back to 12 fps
  expect(blits.length).toBeGreaterThanOrEqual(18)
  const ui = await mountTerminal($)
  expect(await ui.find({ type: 'Image', key: 'rocky' })).toBeTruthy()
  await ui.unmount()
})

test('a restart while a tick is in flight does not let that tick block the new ticker', async ($, on) => {
  let release: () => void = () => {}
  let held = false
  // The first blit waits until released: its tick is still in flight.
  const clock = engine(on, { result: {} }, async () => {
    if (!held) {
      held = true
      await new Promise<void>(resolve => {
        release = resolve
      })
    }
    return {}
  })
  await $.session.start(SESSION)
  await clock.advance(83) // the first tick starts and waits in its blit
  expect(blits).toHaveLength(1)

  await $.session.start(SESSION) // a restart: a new ticker, the old one dead
  blits = []
  await clock.advance(83 * 4)
  // the new ticker ticks on although the old tick never finished: 4 ticks, not
  // 8 (two tickers)
  expect(blits.length).toBeGreaterThanOrEqual(3)
  expect(blits.length).toBeLessThanOrEqual(5)

  release()
  await clock.advance(83 * 4)
  // finishing the old tick broke nothing, and woke no second ticker
  expect(blits.length).toBeGreaterThanOrEqual(6)
  expect(blits.length).toBeLessThanOrEqual(9)
})

test('a desktop pane draws the ASCII figure, with no Image', async ($, on) => {
  engine(on)
  await $.session.start(SESSION)

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await ui.find({ type: 'Image' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /\.-"""-\./ })).toBeTruthy()
  await ui.unmount()
})

const VALID_FILE = /assets\/frames\/idle-(0\d|1[01])\.png$/

test('a corrupt mood or a negative frame counter still names a real frame file', async ($, on) => {
  // Seeded under the right shape tags, so the values are read, not declined.
  on('state.get', (_, e) => {
    const key = (e as { key?: unknown }).key
    if (key === 'mood') {
      return { value: { version: 1, value: { shape: 'mood-3', value: { mood: 'zombie', detail: '', seq: 1, gen: 0, until: 1e12 } } } } as never
    }
    if (key === 'frame') {
      return { value: { version: 1, value: { shape: 'frame-2', value: { n: -7, now: 0 } } } } as never
    }
    return { value: { version: 1, value: { shape: 'fallback-1', value: 'image' } } } as never
  })
  const clock = engine(on)
  await $.session.start(SESSION)
  await clock.advance(83 * 12)

  // what the pane draws
  const ui = await mountTerminal($)
  const image = await ui.find({ type: 'Image', key: 'rocky' })
  await ui.unmount()
  expect((image?.props.source as { file: string }).file).toMatch(VALID_FILE)
  // and what the ticker blits
  expect(blits.length).toBeGreaterThan(0)
  for (const b of blits) {
    expect('source' in b && 'file' in b.source ? b.source.file : '').toMatch(VALID_FILE)
  }
})

test('a pane that flaps does not restart the ticker at every success', async ($, on) => {
  let calls = 0
  // deny, success, deny, success...: never two successes in a row
  const clock = engine(on, { result: {} }, async () => (calls++ % 2 === 0 ? { deny: NOT_MOUNTED } : {}))
  await $.session.start(SESSION)

  await clock.advance(415 * 10)

  // slow throughout (the first, fast, tick aside): about one blit per stamp. A
  // restart at each success would add a fast tick after each, doubling that.
  expect(blits.length).toBeLessThanOrEqual(13)
  expect(blits.length).toBeGreaterThanOrEqual(8)
})

test('exactly two successes bring the fast rate back: one is not enough, three are not needed', async ($, on) => {
  const answer: Record<string, unknown> = { deny: NOT_MOUNTED }
  const clock = engine(on, { result: {} }, answer)
  await $.session.start(SESSION)
  // t = 83: the first (fast) tick is refused and the slow ticker starts, ticking
  // at 498, 913, 1328 ...
  await clock.advance(166)
  answer.deny = undefined // the pane is mounted from here on
  blits = []

  await clock.advance(534) // t = 700: one slow tick (498) has succeeded
  expect(blits).toHaveLength(1) // still slow: one success is not enough

  await clock.advance(545) // t = 1245: the second success (913), then fast ticks
  // 913, then 996, 1079, 1162, 1245: back at 12 fps after two, not three
  expect(blits.length).toBeGreaterThanOrEqual(4)
  expect(blits.length).toBeLessThanOrEqual(6)
})

const scripted = (script: (string | undefined)[]) => {
  let i = 0
  return async () => {
    const deny = script[i++]
    return deny ? { deny } : {}
  }
}

const drawsImage = async ($: Engine) => {
  const ui = await mountTerminal($)
  const found = Boolean(await ui.find({ type: 'Image', key: 'rocky' }))
  await ui.unmount()
  return found
}

test('a bad source refused 8 times in a row flips the pane to the ASCII figure', async ($, on) => {
  const clock = engine(on, { result: {} }, scripted(Array(8).fill('a bad source')))
  await $.session.start(SESSION)
  expect(await drawsImage($)).toBe(true)

  await clock.advance(83 + 415 * 9)

  expect(await drawsImage($)).toBe(false)
})

test('7 refusals, a success, and 7 more do not flip', async ($, on) => {
  const bad = 'cells that do not decode'
  const clock = engine(on, { result: {} }, scripted([...Array(7).fill(bad), undefined, ...Array(7).fill(bad)]))
  await $.session.start(SESSION)

  await clock.advance(83 + 415 * 20)

  expect(await drawsImage($)).toBe(true)
})

for (const words of [NOT_MOUNTED, NOT_MOUNTED_DOC, 'the size is not the mounted one']) {
test(`${words}, however many times, never flips`, async ($, on) => {
  const clock = engine(on, { result: {} }, { deny: words })
  await $.session.start(SESSION)

  await clock.advance(83 + 415 * 25) // 26 refusals

  expect(await drawsImage($)).toBe(true)
})
}
test('a not-mounted refusal breaks a run of other refusals', async ($, on) => {
  const bad = 'a bad source'
  const clock = engine(on, { result: {} }, scripted([...Array(7).fill(bad), NOT_MOUNTED, ...Array(7).fill(bad)]))
  await $.session.start(SESSION)

  await clock.advance(83 + 415 * 20)

  expect(await drawsImage($)).toBe(true)
})

test('only the doc wording for a file the terminal cannot read counts as an alt refusal', async ($, on) => {
  // "the terminal cannot read" in another sense is an ordinary refusal: two of
  // them do not flip
  const clock = engine(on, { result: {} }, scripted(['the terminal cannot read colors', 'the terminal cannot read colors']))
  await $.session.start(SESSION)

  await clock.advance(83 + 415 * 6)

  expect(await drawsImage($)).toBe(true)
})

test('a ticker retired while it waits for the clock does not stamp', async ($, on) => {
  world(on)
  // A clock of our own (mock.clock answers clock.now itself and cannot be held):
  // time moves only in `advance`, and one clock read can be held.
  let t = 0
  let hold = false
  let release: () => void = () => {}
  const waiters: { due: number; resolve: (answer: never) => void }[] = []
  on('clock.now', async () => {
    if (hold) {
      hold = false
      await new Promise<void>(resolve => {
        release = resolve
      })
    }
    return { value: t } as never
  })
  on('clock.every', (_, e) => new Promise<never>(resolve => waiters.push({ due: t + e.ms, resolve })))
  const settle = async () => {
    const later = (globalThis as { setTimeout?: (fn: () => void, ms: number) => unknown }).setTimeout
    for (let i = 0; i < 6; i++) {
      if (later) await new Promise<void>(resolve => later(resolve, 0))
      else for (let j = 0; j < 200; j++) await Promise.resolve()
    }
  }
  const advance = async (ms: number) => {
    const end = t + ms
    for (;;) {
      const next = waiters.filter(w => w.due <= end).sort((a, b) => a.due - b.due)[0]
      if (!next) break
      waiters.splice(waiters.indexOf(next), 1)
      t = next.due
      next.resolve({ value: undefined } as never)
      await settle()
    }
    t = end
  }
  on('command.register', () => ({ value: { command: 'rocky' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('ui.blit', () => ({ value: {} }) as never)
  let landed = 0
  on('state.set', async (_, e, next) => {
    if ((e as { key?: unknown }).key === 'frame') landed++
    return next(e)
  })
  await $.session.start(SESSION)
  hold = true
  await advance(83 * 5) // the fifth tick stamps: its clock read is held
  expect(hold).toBe(false) // the read was reached and is waiting
  expect(landed).toBe(0)

  await $.session.start(SESSION) // a restart retires that ticker
  const before = landed
  release() // the held read now returns
  await settle()

  expect(landed).toBe(before) // the retired ticker wrote nothing
})

// ---- the work guard ----

const watch = (on: On, toolResult: Record<string, unknown> = { result: {} }, home = HOME) => {
  const seen = { writes: 0, frames: 0, periods: 0, closed: 0 }
  on('clock.every', { ms: 83 }, async (_, e, next) => {
    seen.periods++
    return next(e)
  })
  const clock = engine(on, toolResult, {}, home)
  on('state.set', async (_, e, next) => {
    seen.writes++
    if ((e as { key?: unknown }).key === 'frame') seen.frames++
    return next(e)
  })
  on('ui.close', () => {
    seen.closed++
    return { value: undefined } as never
  })
  return { clock, seen }
}

type Seen = ReturnType<typeof watch>['seen']

// A session in `cwd`: starts, takes a turn and a tool call, runs half a second.
// Rocky is "here" if he opened a pane, wrote state and blitted; "away" if he did
// none of those. Anything in between fails.
const visit = async ($: Engine, w: ReturnType<typeof watch>, cwd: string) => {
  const before = { opened, writes: w.seen.writes, blits: blits.length, periods: w.seen.periods }
  cwdNow = cwd
  await $.session.start({ ...SESSION, cwd })
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await w.clock.advance(83 * 6)
  const did = {
    pane: opened - before.opened,
    state: w.seen.writes - before.writes,
    blit: blits.length - before.blits,
    ticker: w.seen.periods - before.periods,
  }
  if (did.pane > 0 && did.state > 0 && did.blit > 0 && did.ticker > 0) return 'here'
  if (did.pane === 0 && did.state === 0 && did.blit === 0 && did.ticker === 0) return 'away'
  throw new Error(`half here: ${JSON.stringify(did)}`)
}

const W = `${HOME}/Projects/work`
const P = `${HOME}/Projects/personal`

test('a personal session gets Rocky, a work session does not, and work wins when both match', async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${P}/eridian`)).toBe('here')
  expect(await visit($, w, `${W}/crosscheck`)).toBe('away')
  expect(await visit($, w, `${HOME}/Documents`)).toBe('away')
})

test('both match: work wins', { options: { personalUnder: '~/Projects', workUnder: '~/Projects/work' } }, async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${HOME}/Projects/sandbox`)).toBe('here')
  expect(await visit($, w, `${W}/crosscheck`)).toBe('away') // inside both: work wins
  expect(await visit($, w, `${W}`)).toBe('away') // the folder itself
})

test('paths compare by segment, not by prefix', { options: { personalUnder: '~/Projects', workUnder: '~/Projects/work' } }, async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${HOME}/Projects/workshop`)).toBe('here') // not inside .../work
  expect(await visit($, w, `${HOME}/Projects/work2/app`)).toBe('here')
  expect(await visit($, w, `${HOME}/Projects/work/app`)).toBe('away')
})

test('a sibling of the personal folder is not inside it', async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${HOME}/Projects/personal2/app`)).toBe('away') // not inside .../personal
  expect(await visit($, w, `${HOME}/Projects/personalized`)).toBe('away')
  expect(await visit($, w, `${P}`)).toBe('here')
})

test('case does not matter', async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${HOME}/PROJECTS/Personal/Eridian`)).toBe('here')
  expect(await visit($, w, `${HOME}/projects/WORK/app`)).toBe('away')
})

test('. and .. and trailing slashes are normalized', async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${P}/./eridian/`)).toBe('here')
  expect(await visit($, w, `${P}/../work/app`)).toBe('away')
  expect(await visit($, w, `${W}/../personal/app`)).toBe('here')
  expect(await visit($, w, `${P}//eridian//`)).toBe('here')
})

test('the two folders are options', { options: { personalUnder: '/opt/play', workUnder: '/opt/play/serious' } }, async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, '/opt/play/game')).toBe('here')
  expect(await visit($, w, '/opt/play/serious/app')).toBe('away')
  expect(await visit($, w, `${P}/eridian`)).toBe('away') // the defaults no longer apply
})

test('with no HOME a ~ path cannot be judged: Rocky stays away (fail closed)', async ($, on) => {
  const w = watch(on, { result: {} }, '')
  expect(await visit($, w, `${P}/eridian`)).toBe('away')
})

test('with no HOME even absolute folders are not trusted', { options: { personalUnder: `${P}`, workUnder: `${W}` } }, async ($, on) => {
  const w = watch(on, { result: {} }, '')
  expect(await visit($, w, `${P}/eridian`)).toBe('away')
  expect(await visit($, w, `${W}/app`)).toBe('away')
})

test('a work session leaves everything alone and tool results pass through unchanged', async ($, on) => {
  const answer = { result: { ok: 1 } }
  const w = watch(on, answer)
  cwdNow = `${W}/crosscheck`
  await $.session.start({ ...SESSION, cwd: cwdNow })
  const writes = w.seen.writes
  await $.turn.start({ text: 'go', turnId: 't1' })
  const ran = await $.tool.call({ tool: 'Bash', command: 'ls' })
  await $.turn.complete({ turnId: 't1', ...(COMPLETE as object) } as never)
  await w.clock.advance(83 * 20)

  expect(ran).toEqual(answer)
  expect(w.seen.writes).toBe(writes) // no state written
  expect(opened).toBe(0)
  expect(blits).toHaveLength(0)
  expect(w.seen.periods).toBe(0) // no ticker
})

test('/rocky says why he is not here', async ($, on) => {
  const w = watch(on)
  on('command.run', () => ({ text: 'the engine' }))
  void w
  cwdNow = `${W}/crosscheck`
  await $.session.start({ ...SESSION, cwd: cwdNow })
  expect((await $.command.run({ command: 'rocky', args: '' } as never)).text).toBe('Rocky stays home in work sessions.')

  cwdNow = `${HOME}/Documents`
  await $.session.start({ ...SESSION, cwd: cwdNow })
  expect((await $.command.run({ command: 'rocky', args: '' } as never)).text).toBe('Rocky only visits personal sessions.')

  cwdNow = `${P}/eridian`
  await $.session.start({ ...SESSION, cwd: cwdNow })
  expect((await $.command.run({ command: 'rocky', args: '' } as never)).text).toBe('Rocky is here.')
})

test('a session that moves into a work directory stands down, and back', async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${P}/eridian`)).toBe('here')

  cwdNow = `${W}/crosscheck`
  await $.turn.start({ text: 'go', turnId: 't2' }) // judged again at each turn
  expect(w.seen.closed).toBe(1) // the pane is closed
  const sent = blits.length
  const writes = w.seen.writes
  await w.clock.advance(83 * 20)
  expect(blits.length).toBe(sent) // the ticker has stopped
  expect(w.seen.writes).toBe(writes)

  cwdNow = `${P}/eridian`
  const pane = opened
  await $.turn.start({ text: 'go', turnId: 't3' })
  await w.clock.advance(83 * 6)
  expect(opened).toBe(pane + 1) // and comes back
  expect(blits.length).toBeGreaterThan(sent)
})

// The options that must never switch the work check off.
for (const bad of ['/', '.', '..', '~/..', '~/../..', 'Projects/work', '$HOME/Projects/work', '~bob/x', '${HOME}/w', ' /x', '/..']) {
  test(`workUnder ${JSON.stringify(bad)} fails closed: no Rocky anywhere`, { options: { workUnder: bad } }, async ($, on) => {
    const w = watch(on)
    expect(await visit($, w, `${P}/eridian`)).toBe('away')
  })
}

for (const bad of ['/', '.', 'rel/ative', '$HOME/p', '~bob/p']) {
  test(`personalUnder ${JSON.stringify(bad)} fails closed`, { options: { personalUnder: bad } }, async ($, on) => {
    const w = watch(on)
    expect(await visit($, w, `${P}/eridian`)).toBe('away')
  })
}

test('blank options fall back to the defaults', { options: { personalUnder: '  ', workUnder: '' } }, async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${P}/eridian`)).toBe('here')
  expect(await visit($, w, `${W}/app`)).toBe('away')
})

test('an NFD directory is inside an NFC work folder (accents do not fail open)', {
  options: { personalUnder: '~/Projects', workUnder: '~/Projects/wörk' },
}, async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${HOME}/Projects/wörk/app`)).toBe('away') // decomposed
  expect(await visit($, w, `${HOME}/Projects/wörk/app`)).toBe('away') // composed
  expect(await visit($, w, `${HOME}/Projects/wor/app`)).toBe('here')
})

test('a cwd read that fails at turn.start sends Rocky away and closes the pane', async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${P}/eridian`)).toBe('here')

  cwdBroken = true
  await $.turn.start({ text: 'go', turnId: 't2' })

  expect(w.seen.closed).toBe(1)
  const sent = blits.length
  await w.clock.advance(83 * 20)
  expect(blits.length).toBe(sent)
})

test('a cd into a work folder in the middle of a turn: the next stamp stops the ticker', async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${P}/eridian`)).toBe('here')
  expect(w.seen.closed).toBe(0)

  cwdNow = `${W}/crosscheck` // no turn boundary: nothing re-judges but the ticker's stamp
  const frames = w.seen.frames
  await w.clock.advance(83 * 6)

  expect(w.seen.closed).toBe(1) // the pane is closed
  expect(w.seen.frames).toBe(frames) // and that stamp wrote nothing
  const sent = blits.length
  await w.clock.advance(83 * 20)
  expect(blits.length).toBe(sent)
  expect(w.seen.frames).toBe(frames)
})

test('a tool call that starts a ticker while a cd lands does not leave one running', async ($, on) => {
  // A clock of our own, so one read can be held; time moves only in `advance`.
  let t = 0
  let hold = false
  let release: () => void = () => {}
  const waiters: { due: number; resolve: (answer: never) => void }[] = []
  world(on)
  on('clock.now', async () => {
    if (hold) {
      hold = false
      await new Promise<void>(resolve => {
        release = resolve
      })
    }
    return { value: t } as never
  })
  on('clock.every', (_, e) => new Promise<never>(resolve => waiters.push({ due: t + e.ms, resolve })))
  const advance = async (ms: number) => {
    const end = t + ms
    for (;;) {
      const next = waiters.filter(w => w.due <= end).sort((a, b) => a.due - b.due)[0]
      if (!next) break
      waiters.splice(waiters.indexOf(next), 1)
      t = next.due
      next.resolve({ value: undefined } as never)
      await settle()
    }
    t = end
  }
  let frames = 0
  let sent = 0
  on('command.register', () => ({ value: { command: 'rocky' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.close', () => ({ value: undefined }) as never)
  on('ui.blit', () => {
    sent++
    return { value: {} } as never
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('turn.start', () => ({ turnId: 't1' }))
  on('tool.call', () => ({ result: {} }) as never)
  on('state.set', async (_, e, next) => {
    if ((e as { key?: unknown }).key === 'frame') frames++
    return next(e)
  })

  await $.session.start(SESSION)
  cwdNow = `${W}/app`
  await $.turn.start({ text: 'go', turnId: 't1' }) // sent away: the ticker is gone
  cwdNow = `${P}/eridian` // back in a personal folder
  hold = true
  await $.tool.call({ tool: 'Bash', command: 'ls' }) // starts a ticker, its clock read held
  await settle()
  expect(hold).toBe(false) // the read was reached and waits

  cwdNow = `${W}/app` // the cd lands while the ticker is being made
  release()
  await settle()
  await advance(83 * 20)

  expect(sent).toBe(0) // no ticker was created
  expect(frames).toBe(0)
})

// ---- input hygiene: nothing odd is trusted ----

const ODD = [
  ['a NUL', '/opt/x\u0000y'],
  ['a newline', '/opt/x\ny'],
  ['a tab at the start', '\t/opt/x'],
  ['trailing whitespace', '/opt/x '],
  ['a segment starting with a space', '/opt/ x'],
  ['a segment ending with a space', '/opt/x /y'],
  ['a backslash', '/opt\\x'],
  ['a DEL', '/opt/x\u007f'],
] as const

for (const [what, bad] of ODD) {
  test(`workUnder with ${what} fails closed`, { options: { workUnder: bad } }, async ($, on) => {
    const w = watch(on)
    expect(await visit($, w, `${P}/eridian`)).toBe('away')
  })
  test(`personalUnder with ${what} fails closed`, { options: { personalUnder: bad } }, async ($, on) => {
    const w = watch(on)
    expect(await visit($, w, `${P}/eridian`)).toBe('away')
  })
  test(`a session directory with ${what} fails closed`, async ($, on) => {
    const w = watch(on)
    expect(await visit($, w, `${P}${bad}`)).toBe('away')
  })
}

for (const [what, home] of [
  ['a newline', '/Users/test\n'],
  ['trailing whitespace', '/Users/test '],
  ['a backslash', '/Users\\test'],
  ['a segment with a space around it', '/Users/ test'],
] as const) {
  test(`HOME with ${what} fails closed`, async ($, on) => {
    const w = watch(on, { result: {} }, home)
    // the session sits under that same odd HOME, so only the hygiene check stops it
    expect(await visit($, w, `${home}/Projects/personal/eridian`)).toBe('away')
  })
}

test('an ordinary space inside a directory name is fine', async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${P}/my project`)).toBe('here')
})

// ---- an explicit work folder must exist ----

test('an explicit workUnder that does not exist fails closed (a typo)', { options: { workUnder: '~/Project/work' } }, async ($, on) => {
  const w = watch(on)
  missing.add(`${HOME}/Project/work`)
  expect(await visit($, w, `${P}/eridian`)).toBe('away')
  missing.delete(`${HOME}/Project/work`) // now it exists
  expect(await visit($, w, `${P}/eridian`)).toBe('here')
})

test('a missing DEFAULT work folder is fine: nothing is work', async ($, on) => {
  const w = watch(on)
  missing.add(`${HOME}/Projects/work`)
  expect(await visit($, w, `${P}/eridian`)).toBe('here')
})

test('naming the default explicitly is not "explicit": a missing one is still fine', { options: { workUnder: '~/Projects/work' } }, async ($, on) => {
  const w = watch(on)
  missing.add(`${HOME}/Projects/work`)
  expect(await visit($, w, `${P}/eridian`)).toBe('here')
})

test('an fs.exists that fails fails closed for an explicit work folder', { options: { workUnder: '~/Elsewhere/work' } }, async ($, on) => {
  const w = watch(on)
  existsBroken = true
  expect(await visit($, w, `${P}/eridian`)).toBe('away')
})

// ---- sent away: the pane closes whether or not a ticker runs ----

test('sent away with no ticker at all: the pane still closes, at session.start and at each turn', async ($, on) => {
  const w = watch(on)
  cwdNow = `${W}/crosscheck`
  await $.session.start({ ...SESSION, cwd: cwdNow })
  expect(w.seen.closed).toBe(1)
  await $.turn.start({ text: 'go', turnId: 't1' })
  expect(w.seen.closed).toBe(2)
})

test('ensureTicker finding Rocky not allowed cancels the live old ticker too', async ($, on) => {
  let t = 0
  let hold = false
  let release: () => void = () => {}
  const waiters: { due: number; resolve: (answer: never) => void }[] = []
  let closed = 0
  let sent = 0
  let frames = 0
  world(on)
  on('clock.now', async () => {
    if (hold) {
      hold = false
      await new Promise<void>(resolve => {
        release = resolve
      })
    }
    return { value: t } as never
  })
  on('clock.every', (_, e) => new Promise<never>(resolve => waiters.push({ due: t + e.ms, resolve })))
  const advance = async (ms: number) => {
    const end = t + ms
    for (;;) {
      const next = waiters.filter(w => w.due <= end).sort((a, b) => a.due - b.due)[0]
      if (!next) break
      waiters.splice(waiters.indexOf(next), 1)
      t = next.due
      next.resolve({ value: undefined } as never)
      await settle()
    }
    t = end
  }
  on('command.register', () => ({ value: { command: 'rocky' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.close', () => {
    closed++
    return { value: undefined } as never
  })
  on('ui.blit', () => {
    sent++
    return { value: {} } as never
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('turn.start', () => ({ turnId: 't1' }))
  on('tool.call', () => ({ result: {} }) as never)
  on('state.set', async (_, e, next) => {
    if ((e as { key?: unknown }).key === 'frame') frames++
    return next(e)
  })

  await $.session.start(SESSION) // a live ticker, and an open pane
  t = 5000 // time passes with no tick landing: the ticker looks dead
  hold = true
  await $.tool.call({ tool: 'Bash', command: 'ls' }) // allowed here: ensureTicker starts, its clock read held
  await settle()
  expect(hold).toBe(false)

  cwdNow = `${W}/app`
  release()
  await settle()
  expect(closed).toBeGreaterThanOrEqual(1) // ensureTicker's "no" stood Rocky down
  await advance(83 * 20)

  expect(sent).toBe(0) // the old ticker never ticked again
  expect(frames).toBe(0)
})

test('after a cd into work, at most 4 more picture swaps happen before the pane closes', async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${P}/eridian`)).toBe('here')

  const before = blits.length
  cwdNow = `${W}/crosscheck`
  await w.clock.advance(83 * 12)

  expect(w.seen.closed).toBe(1)
  expect(blits.length - before).toBeLessThanOrEqual(4)
})

test('the pane is opened once when a session starts again after being sent away', async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${P}/eridian`)).toBe('here')
  expect(opened).toBe(1)

  cwdNow = `${W}/crosscheck`
  await $.turn.start({ text: 'go', turnId: 't2' }) // sent away
  cwdNow = `${P}/eridian`
  await $.session.start({ ...SESSION, cwd: cwdNow })
  expect(opened).toBe(2)
  await $.turn.start({ text: 'go', turnId: 't3' })

  expect(opened).toBe(2) // the turn does not open it a second time
})

// ---- a pane left open mid-turn ----

test('a tool call in a work directory closes a pane left open, and keeps doing so', async ($, on) => {
  const w = watch(on)
  cwdNow = `${W}/crosscheck`
  await $.session.start({ ...SESSION, cwd: cwdNow })
  expect(w.seen.closed).toBe(1) // session.start

  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await settle()
  expect(w.seen.closed).toBe(2) // the tool call

  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await settle()
  expect(w.seen.closed).toBe(3)
  expect(opened).toBe(0)
})

test('a turn.complete in a work directory closes a pane left open', async ($, on) => {
  const w = watch(on)
  cwdNow = `${W}/crosscheck`
  await $.session.start({ ...SESSION, cwd: cwdNow })
  const closed = w.seen.closed

  await $.turn.complete({ turnId: 't1', ...(COMPLETE as object) } as never)

  expect(w.seen.closed).toBe(closed + 1)
})

test('a cd to work mid-turn closes the pane at the next tool call, and a cd back reopens it', async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${P}/eridian`)).toBe('here')
  expect(opened).toBe(1)

  cwdNow = `${W}/crosscheck` // no turn boundary
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await settle()
  expect(w.seen.closed).toBeGreaterThanOrEqual(1)

  const sent = blits.length
  await w.clock.advance(83 * 12)
  expect(blits.length).toBe(sent) // and the ticker is gone

  cwdNow = `${P}/eridian`
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await settle()
  expect(opened).toBe(2) // reopened

  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await settle()
  expect(opened).toBe(2) // once
})

// ---- the work folder's existence is asked once per session ----

test('fs.exists is not asked when the answer is no anyway, and once per session when it is yes', { options: { workUnder: '~/Elsewhere/work' } }, async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${HOME}/Documents`)).toBe('away') // elsewhere: no need to ask
  expect(existsCalls).toBe(0)
  expect(await visit($, w, `${P}/eridian`)).toBe('here') // many checks, one question
  expect(existsCalls).toBe(1)
  await $.turn.start({ text: 'go', turnId: 't9' })
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await w.clock.advance(83 * 20)
  expect(existsCalls).toBe(1)
})

test('a missing explicit work folder stays closed for the session, and /rocky looks again', { options: { workUnder: '~/Project/work' } }, async ($, on) => {
  const w = watch(on)
  on('command.run', () => ({ text: 'the engine' }))
  missing.add(`${HOME}/Project/work`)
  await $.session.start(SESSION)
  await $.turn.start({ text: 'go', turnId: 't1' })
  expect(opened).toBe(0)

  missing.delete(`${HOME}/Project/work`) // the folder appears
  await $.turn.start({ text: 'go', turnId: 't2' })
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await settle()
  expect(opened).toBe(0) // the cached "missing" stands
  expect(blits).toHaveLength(0)

  expect((await $.command.run({ command: 'rocky', args: '' } as never)).text).toBe('Rocky is here.')
  expect(opened).toBe(1)
  void w
})

test('an fs.exists that failed stays closed for the session, and /rocky looks again', { options: { workUnder: '~/Elsewhere/work' } }, async ($, on) => {
  watch(on)
  on('command.run', () => ({ text: 'the engine' }))
  existsBroken = true
  await $.session.start(SESSION)
  existsBroken = false
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await settle()
  expect(opened).toBe(0) // the failure was cached for the session

  expect((await $.command.run({ command: 'rocky', args: '' } as never)).text).toBe('Rocky is here.')
  expect(opened).toBe(1)
})

test('a new session looks at the work folder afresh', { options: { workUnder: '~/Project/work' } }, async ($, on) => {
  const w = watch(on)
  missing.add(`${HOME}/Project/work`)
  expect(await visit($, w, `${P}/eridian`)).toBe('away')
  missing.delete(`${HOME}/Project/work`)
  expect(await visit($, w, `${P}/eridian`)).toBe('here')
})

test('a tool call and a turn start racing to reopen a parked pane open it exactly once', async ($, on) => {
  const w = watch(on)
  expect(await visit($, w, `${P}/eridian`)).toBe('here')
  expect(opened).toBe(1)

  cwdNow = `${W}/crosscheck`
  await $.tool.call({ tool: 'Bash', command: 'ls' }) // sent away: parked
  await settle()
  expect(w.seen.closed).toBeGreaterThanOrEqual(1)

  cwdNow = `${P}/eridian` // back
  // The turn start reads the directory three times (its first check, the
  // ticker's, and the one guarding the reopen): the third waits...
  holdCwdNth = 3
  const turn = $.turn.start({ text: 'go', turnId: 't2' })
  await settle()
  await $.tool.call({ tool: 'Bash', command: 'ls' }) // ...while the tool call gets in first and reopens
  await settle()
  expect(opened).toBe(2)

  releaseCwd() // now the turn start goes on: it must find the pane already open
  await turn
  await settle()
  expect(opened).toBe(2) // one reopen, not two
})

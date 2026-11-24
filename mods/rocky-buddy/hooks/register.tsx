import { atom, read, update } from 'claude-code'
import type {
  EngineInterface,
  PluginOptions,
  Register,
  StateDollar,
  TimerCall,
  UiBlitArgs,
  UiBlitResult,
} from 'claude-code'

import type { Fallback, Frame, Mood, RockyMood } from '../types'
import { ASK, isPersonAsk, outcomeFor } from './outcome'
import type { Kind } from './outcome'

const PANE = 'rocky'
const IMAGE_KEY = 'rocky'
const TITLE = 'Rocky'
// The one ticker runs at the picture's frame rate (12 fps) while blits land.
// Only every fifth tick writes the frame atom (about 415 ms), which is what the
// ASCII art animates from, what expiry reads, and what says the ticker is alive.
// While blits are refused (no pane to draw into) the ticker is recreated at the
// stamp period, one tick per stamp, until a blit lands.
const FAST_MS = 83
const WRITE_EVERY = 5
const SLOW_MS = FAST_MS * WRITE_EVERY // 415
// A stamp lands every SLOW_MS at either rate, so a ticker whose last stamp is
// older than this (over three of them) is dead.
const LIVE_MS = 1350
const HAPPY_MS = 4000
// A mood is shown this long after its last write, then reads as idle. The
// expiry is a time in state, checked at draw, so no decay write has to land;
// what it does depend on is the ticker, which stamps the clock into the frame.
// A dead ticker is revived by the next turn, tool call or /rocky. Thinking is
// long because a model turn can be silent for minutes.
const TTL_MS: Record<Mood, number> = {
  idle: 0,
  thinking: 120000,
  working: 20000,
  worry: 20000,
  happy: HAPPY_MS,
}
const FRAMES = 12 // the pictures' frames per mood, and a multiple of the ASCII art's

// One formula for which picture frame is showing, for the pane's draw and the
// ticker alike: `n` is the stamped frame counter and `k` the ticks since it.
// Clamped to 0..FRAMES-1, whatever a stored `n` holds (a negative, fractional
// or non-numeric one from a corrupt value included).
const clampFrame = (x: number) => (Number.isFinite(x) ? ((Math.floor(x) % FRAMES) + FRAMES) % FRAMES : 0)
const pictureFrame = (n: number, k: number) => clampFrame(n * WRITE_EVERY + k)

const MOOD_NAMES: readonly string[] = ['idle', 'thinking', 'working', 'worry', 'happy']
const isMood = (m: unknown): m is Mood => typeof m === 'string' && MOOD_NAMES.includes(m)

// The frames are files in the plugin: the terminal reads them itself, so no
// pixels cross `$`. Over ssh it cannot read them and draws a blank box with no
// deny; that is accepted, this is for local use. `root` is `$.plugin.root`,
// absolute by its docs; a path may be at most 3072 bytes.
// A mood that is not one of ours (a corrupt stored value) is drawn as idle, and
// the index is clamped again here, so the path always names a file that exists.
const framePath = (root: string, mood: unknown, i: number) =>
  `${root}/assets/frames/${isMood(mood) ? mood : 'idle'}-${String(clampFrame(i)).padStart(2, '0')}.png`

// The words a refused blit uses for an Image drawn as its alt ("an Image drawing
// its alt there"), or a file the terminal cannot read ("a file this terminal
// cannot read"): UiBlitResult carries them as text, with no code.
const ALT_DENY = /\bdraw(?:s|ing)? its `?alt\b|\bfile this terminal cannot read\b/i
// "Not mounted" (or a size that is not the mounted one): the pane is closed or
// being resized. Transient, and never a reason to give up on pictures.
const NOT_MOUNTED = /\bmounted\b/i
// Any other refusal (a bad source, cells that do not decode, one we do not know)
// that repeats this many times in a row, with no success between, is treated as
// permanent too: about 3.3 s at the slow rate.
const OTHER_DENIES = 8
const RECOVER_OKS = 2 // successful blits in a row, before the fast rate comes back
const ALT_DENIES = 2 // in a row, before the pane gives up on pictures

// Change a tag when the fields change: it declines what an earlier version
// stored, which outlives a reload.
const moodAtom = atom(
  { plugin: 'rocky-buddy', key: 'mood' } as const,
  { mood: 'idle', detail: '', seq: 0, gen: 0, until: 0 } satisfies RockyMood,
  { shape: 'mood-4' }, // 4: `detail` is the whole bubble line now, not a tool name
)
// `now` is the clock as of the last tick: the draw has no clock of its own, and
// a mood whose `until` it has passed reads as idle.
// Whether the pane may draw the picture: 'ascii' once the terminal has said it
// draws the Image's alt instead. session.start puts it back.
const fallbackAtom = atom({ plugin: 'rocky-buddy', key: 'fallback' } as const, 'image' satisfies Fallback, {
  shape: 'fallback-1',
})
const frameAtom = atom({ plugin: 'rocky-buddy', key: 'frame' } as const, { n: 0, now: 0 } satisfies Frame, {
  shape: 'frame-2',
})

// Five limbs: two arms raised from the shoulders, three legs, a carapace of
// `#` and `.` and no eyes. Every frame is ART_ROWS rows of at most ART_COLS
// columns, so the bubble above never jumps. The art is drawn about column 10
// (`MID`); `mirror` makes the right limb of a left one.
const ART_ROWS = 7
const BLOCK_ROWS = 3 + ART_ROWS // the bubble and the art
const ART_COLS = 20
const MID = 10

type Stamp = readonly [row: number, col: number, text: string]
type Arm = readonly Stamp[]
type Pose = {
  left: Arm
  right: Arm
  tex?: 0 | 1
  feet?: 0 | 1
  lift?: boolean
  spark?: 'left' | 'right'
}

const mirror = ([row, col, text]: Stamp): Stamp => {
  const swapped = [...text]
    .reverse()
    .map(ch => (ch === '\\' ? '/' : ch === '/' ? '\\' : ch))
    .join('')
  return [row, 2 * MID + 1 - col - text.length, swapped]
}

// Arms of the left side; the right side is the mirror of the same pose.
const up = (dx = 0): Arm => [[0, 1 + dx, '\\\\'], [1, 2, '\\\\']]
const down: Arm = [[3, 3, '//'], [4, 2, '//']]
const tuck = (dx = 0): Arm => [[1, 5 + dx, '\\\\']]
const high: Arm = [[0, 2, '\\\\']]

const draw = ({ left, right, tex = 0, feet = 0, lift = false, spark }: Pose): string[] => {
  const grid = Array.from({ length: ART_ROWS }, () => Array<string>(ART_COLS).fill(' '))
  const put = (row: number, col: number, text: string) => {
    ;[...text].forEach((ch, i) => {
      const line = grid[row]
      if (line && col + i >= 0 && col + i < ART_COLS) line[col + i] = ch
    })
  }
  const dy = lift ? -1 : 0
  const a = tex === 0 ? '# . #' : '. # .'
  const b = tex === 0 ? '. # .' : '# . #'
  const f = feet === 0 ? '_' : '.'

  put(1 + dy, 7, '.-"""-.')
  put(2 + dy, 3, `\`-./ ${a} \\.-'`)
  put(3 + dy, 6, `| ${b} |`)
  put(4 + dy, 6, '\\_.-"-._/')
  // Three legs, none vertical: left and right splay out, the back leg leans
  // to the left beside them.
  put(5 + dy, 6, '/  /    \\')
  put(6 + dy, 4, `${f}/ ${f}/      \\${f}`)
  for (const s of left) put(...s)
  for (const s of right) put(...mirror(s))
  if (spark) put(5, spark === 'left' ? 1 : 18, '*')

  return grid.map(row => row.join('').trimEnd())
}

const pair = (arm: Arm, extra: Omit<Pose, 'left' | 'right'> = {}): Pose => ({
  left: arm,
  right: arm,
  ...extra,
})

const ART: Record<Mood, string[][]> = {
  // The hands sway: both draw one column in, then back out.
  idle: [0, 1, 0].map(dx => draw(pair(up(dx)))),
  thinking: [
    draw({ left: up(), right: down }),
    draw({ left: down, right: up() }),
  ],
  working: [
    draw({ left: up(), right: down, tex: 0, spark: 'right' }),
    draw({ left: down, right: up(), tex: 1, spark: 'left' }),
  ],
  worry: [
    draw({ ...pair(tuck()), feet: 0 }),
    draw({ ...pair(tuck(-1)), feet: 1 }),
    draw({ ...pair(tuck()), feet: 0, tex: 1 }),
  ],
  happy: [
    draw(pair(up())),
    draw({ ...pair(high), lift: true }),
    draw(pair(up(1), { tex: 1 })),
  ],
}

const LINES: Record<Mood, string> = {
  idle: '...',
  thinking: 'question?',
  working: 'work work',
  worry: 'worry worry worry',
  happy: 'good good good',
}

const clip = (text: string, width: number) =>
  text.length <= width ? text : width > 2 ? `${text.slice(0, width - 2)}..` : text.slice(0, Math.max(0, width))

// The line is what the tool call narrated, or the outcome's words (any mood but idle),
// cleaned again here:
// a stored value is not trusted).
const lineFor = (m: RockyMood) => {
  const said = m.mood !== 'idle' && typeof m.detail === 'string' ? clean(m.detail, MAX_LINE) : ''
  return said !== '' ? said : LINES[m.mood]
}

const bubble = (text: string, columns: number, center: number): string[] => {
  const inner = Math.max(0, Math.min(text.length, columns - 4))
  const said = clip(text, inner).padEnd(inner)
  const width = inner + 4
  // The pointer sits at the middle; the whole bubble is centered over the body.
  const mid = Math.floor(width / 2)
  const bottom = `'${'-'.repeat(width - 2)}'`
  const pointed = `${bottom.slice(0, mid)}v${bottom.slice(mid + 1)}`
  const pad = ' '.repeat(Math.max(0, Math.min(center - mid, columns - width)))
  return [`.-${'-'.repeat(inner)}-.`, `| ${said} |`, pointed].map(row => clip(pad + row, columns))
}

// The art keeps its shape: a pane narrower than it loses the columns from both
// sides, never an ellipsis over one side. An odd drop takes the extra column
// from the left, which puts the body's axis on the pane's middle.
const fit = (rows: string[], columns: number): { rows: string[]; center: number } => {
  if (columns >= ART_COLS) {
    // wider than the art: centered, by the same margin on the bubble
    const margin = Math.floor((columns - ART_COLS) / 2)
    return { rows: rows.map(row => (row ? ' '.repeat(margin) + row : row)), center: MID + margin }
  }
  const drop = ART_COLS - columns
  const left = Math.ceil(drop / 2)
  return {
    rows: rows.map(row => row.padEnd(ART_COLS).slice(left, left + columns).trimEnd()),
    center: MID - left,
  }
}

// The picture is 240 x 210 px, 1.14 wide for each of its height. A terminal cell
// is about twice as tall as it is wide (an assumption: terminals differ), which
// would make 2.29 columns per row; 2.4 is used so the default is a round 24 x 10
// (a 5% horizontal stretch). The box shrinks to the pane: at most its columns,
// and its rows less the bubble's three.
const IMAGE_COLUMNS = 24
const IMAGE_ROWS = 10
const COLUMNS_PER_ROW = 2.4
const imageBox = (columns: number, bodyRows: number) => {
  const maxRows = Math.max(1, Math.min(IMAGE_ROWS, bodyRows - 3))
  const cols = Math.max(1, Math.min(IMAGE_COLUMNS, columns, Math.floor(maxRows * COLUMNS_PER_ROW)))
  return { columns: cols, rows: Math.max(1, Math.min(maxRows, Math.round(cols / COLUMNS_PER_ROW))) }
}

const quiet = () => {}

// What the work guard reads, fresh each time it is asked.
type Judging = Pick<EngineInterface, 'session' | 'env' | 'fs'>
type Clocked = StateDollar & Judging & { clock: { now: () => Promise<number> } }
type Ticking = Clocked & {
  clock: { every: TimerCall }
  plugin: { root: string }
  ui: {
    blit: (args: UiBlitArgs) => Promise<UiBlitResult>
    close: EngineInterface['ui']['close']
    open: EngineInterface['ui']['open']
  }
}

// The one way a mood changes. `change` sees the stored mood and answers the
// next one, or undefined to leave it; a change bumps `seq` and stamps `until`
// from a clock read taken just before the write. It writes only where Rocky is
// allowed, checked right before the write.
const apply = async (
  $: Clocked,
  change: (m: RockyMood, now: number) => (Pick<RockyMood, 'mood' | 'detail' | 'gen'> & { ttl?: number }) | undefined,
) => {
  const now = await $.clock.now()
  if (!(await allowed($))) return
  await update($, moodAtom, (m): RockyMood => {
    const next = change(m, now)
    if (!next) return m
    const { ttl, ...mood } = next
    return { ...mood, seq: m.seq + 1, until: now + (ttl ?? TTL_MS[next.mood]) }
  })
}

// A turn boundary: `gen` moves, the mood and its expiry do not.
const bumpGen = async ($: Clocked) => {
  if (!(await allowed($))) return
  await update($, moodAtom, (m): RockyMood => ({ ...m, gen: m.gen + 1 }))
}

// The one timer: every tick swaps the picture, and every fifth (every one, once
// slow) stamps the clock into the frame (which expires moods and shows the
// ticker is alive). A refused period ends a `clock.every`, and the timer it
// returned can only cancel, so a ticker is judged dead when the frame's stamp
// (or the start of this ticker, while its first stamp is still due) is older
// than LIVE_MS, and a call, a turn or /rocky starts a new one. A reload drops
// the environment, all of this with it, and session.start starts afresh.
//
// Each ticker owns its own state, so a restart cannot let two ticks overlap or
// reset another's: the old one is marked dead and its late results are dropped.
type Ticker = {
  cancel: () => void
  dead: boolean
  busy: boolean // a tick still waiting on the host: the next one is skipped
  period: number
  n: number // the frame counter as of the last stamp
  k: number // ticks since that stamp
  oks: number // successful blits in a row, while slow
  lastNow: number // the clock at the last real read; ticks between add their periods
}

let ticker: Ticker | undefined
let startedAt = Number.NEGATIVE_INFINITY
let useImages = true // false once the terminal said, twice, it draws the alt
let altDenies = 0
let otherDenies = 0

const tick = async ($: Ticking, st: Ticker) => {
  if (st.dead || st.busy) return
  st.busy = true
  try {
    const slow = st.period === SLOW_MS
    let now: number
    if (slow || st.k >= WRITE_EVERY - 1) {
      now = await $.clock.now()
      // Retired while waiting for the clock: it must not stamp over its successor.
      if (st.dead) return
      // Rocky may have been sent away (a cd into a work folder) since the last
      // stamp: then this ticker stops, closes the pane and writes nothing. Only
      // stamps ask (a host read at 12 Hz would be too much), so after such a cd
      // at most WRITE_EVERY - 1 = 4 more picture swaps (about 330 ms of Rocky's
      // own frames, no work data) happen before the pane closes.
      if (!(await allowed($))) {
        await standDown($, st)
        return
      }
      if (st.dead) return
      st.lastNow = now
      st.n = (await update($, frameAtom, (f): Frame => ({ n: (f.n + 1) % FRAMES, now }))).n
      st.k = 0
    } else {
      st.k++
      now = st.lastNow + st.k * st.period
    }
    if (st.dead || !useImages) return

    // The mood is read each tick (a state read at 12 Hz): a write to it must
    // show within a frame. Expiry is judged on the estimated clock.
    const m = await read($, moodAtom)
    const mood: unknown = m.until > now ? m.mood : 'idle'
    const file = framePath($.plugin.root, mood, pictureFrame(st.n, st.k))
    const blit = await $.ui.blit({ requestId: PANE, key: IMAGE_KEY, source: { file, format: 'png' } })
    if (st.dead) return

    if (!blit.deny) {
      altDenies = 0
      otherDenies = 0
      // A pane is drawing again, once it has done so twice running: a flapping
      // pane must not restart the ticker at every success.
      if (slow && ++st.oks >= RECOVER_OKS) await ensureTicker($, true, FAST_MS)
      return
    }
    st.oks = 0
    let giveUp = false
    if (ALT_DENY.test(blit.deny)) {
      altDenies++
      otherDenies = 0
      giveUp = altDenies >= ALT_DENIES
    } else if (NOT_MOUNTED.test(blit.deny)) {
      altDenies = 0
      otherDenies = 0
    } else {
      altDenies = 0
      giveUp = ++otherDenies >= OTHER_DENIES
    }
    if (giveUp) {
      useImages = false
      if (await allowed($)) await update($, fallbackAtom, (): Fallback => 'ascii')
    }
    // Nothing to draw into, or no pictures: one tick per stamp is enough.
    if (!slow) await ensureTicker($, true, SLOW_MS)
  } finally {
    st.busy = false
  }
}

const ensureTicker = async ($: Ticking, restart = false, period = FAST_MS) => {
  const now = await $.clock.now()
  const seen = ticker
  const frame = await read($, frameAtom)
  if (!restart && seen) {
    if (now - Math.max(frame.now, startedAt) <= LIVE_MS) return
    // Another check restarted it while this one read: leave the new one be.
    if (ticker && ticker !== seen) return
  }

  // Asked last, after every await above: a ticker is never created for a
  // session Rocky has been sent away from. Nothing awaits between this check
  // and the swap below.
  if (!(await allowed($))) {
    await standDown($) // an old ticker, if any, stops too
    return
  }

  // The new timer first: if `$.clock.every` throws, the old ticker is kept and
  // the error goes to the caller.
  const st: Ticker = { cancel: () => {}, dead: false, busy: false, period, n: frame.n, k: 0, oks: 0, lastNow: now }
  const timer = $.clock.every(period, () => {
    tick($, st).catch(quiet)
  })
  st.cancel = () => timer.cancel()

  const old = ticker
  ticker = st
  startedAt = now
  if (old) old.dead = true
  try {
    old?.cancel()
  } catch {
    // the old one is gone either way
  }
}

// ---- what the bubble says about the work ------------------------------------
// The bubble names what Rocky sees, in his doubled-verb way, from the tool and
// its input. Only these may ever be shown: the kind of tool, a file's BASENAME
// (never a directory, and not at all if the name looks sensitive or the file is
// in the work folder), the first word of a simple Bash command (never its
// arguments), a git subcommand, a subagent type, an MCP server's name. Nothing
// is worked out by parsing shell syntax: a Bash line is built from allow-listed
// shapes only, so anything unusual says `run run` and can leak nothing.
//
// Every shown token is cleaned with an ALLOW-list (below) and capped.
const MAX_SHOWN = 24 // one shown token
const MAX_LINE = 60 // a whole bubble line (a verb pair and a token)

// Letters, numbers, punctuation, symbols and the plain space are all that is
// ever drawn: control, format (zero-width, bidi, soft hyphen, word joiners,
// interlinear annotation, tag characters), variation selectors and the other
// invisible characters are dropped. Escape sequences go first, whole.
const ALLOWED = /^[\p{L}\p{N}\p{P}\p{S} ]$/u

const clean = (text: string, max = MAX_SHOWN): string => {
  const kept = Array.from(
    text
      .replace(/\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)?/g, '') // OSC sequences
      .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '') // CSI sequences
      .replace(/[\u0080-\u009f]/g, '') // C1 controls, including their introducers
      .normalize('NFC')
      .replace(/[\t\n\r\u000b\u000c]|\p{Z}/gu, ' '),
  )
    .filter(ch => ALLOWED.test(ch))
    .join('')
    .replace(/ +/g, ' ')
    .trim()
  const chars = Array.from(kept)
  return chars.length > max ? `${chars.slice(0, max - 1).join('')}\u2026` : kept
}

// File names that may carry or point at a secret are never named.
const SENSITIVE_NAME: RegExp[] = [
  /^\.env/,
  /\.env(\.|$)/, // prod.env
  /\.(pem|key|p12|pfx|kdbx|keystore|jks|asc|gpg|tfvars|tfstate|p8|ppk|age)(\.|~|$)/, // also server.key.bak, id.pem~
  /^id_/,
  /credential|secret|token|passw/,
  /service[-_]?account|serviceaccountkey/,
  /api[-_]?key/,
  /^\.(npmrc|netrc|pgpass|pypirc)$/,
  /vault[-_]?pass/,
  /kubeconfig/,
  /^auth\.json/,
  /\.envrc/,
  /^authorized_keys$/,
  /^known_hosts$/,
  /[a-z0-9_-]{24,}/, // a token-shaped run
  /(^|[^a-z0-9])(ghp_|gho_|ghu_|ghs_|github_pat_|sk-|xox|akia)/, // known token prefixes
]
const sensitive = (name: string) => {
  const lower = name.toLowerCase()
  return SENSITIVE_NAME.some(re => re.test(lower))
}

// The file's whole name, cleaned but not shortened: it is judged before it is
// cut for the bubble. A path ending in a slash names a directory: no name.
const baseName = (path: string): string | undefined => {
  if (/[\\/]$/.test(path)) return undefined
  const last = path.split(/[\\/]/).filter(part => part !== '').pop()
  const name = last === undefined ? '' : clean(last, Number.POSITIVE_INFINITY)
  return name === '' ? undefined : name
}

// Where a path lands, for the work check only: lenient (no hygiene rejection,
// so an odd name is still compared), NFC and case-folded like the guard.
const looseSegments = (path: string): string[] => {
  const out: string[] = []
  for (const part of path.split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') out.pop()
    else out.push(part.normalize('NFC').toLowerCase())
  }
  return out
}

// True when the file is inside the work folder, or when that cannot be told.
const inWork = (path: string, v: Verdict): boolean => {
  if (!v.workSegs) return true
  // A path that starts with ~ or holds a $ cannot be placed (the file system
  // would expand it): treated as work.
  if (path.startsWith('~') || path.includes('$')) return true
  const full = path.startsWith('/') ? path : v.cwd === undefined ? undefined : `${v.cwd}/${path}`
  return full === undefined || within(looseSegments(full), v.workSegs)
}

// Bash, by allow-list. The command word is the command's first word only if it
// is plain (letters, digits, `.`, `_`, `/`, `-`) and ends at a space or the end
// of the command: so `X=$(...) foo`, `TOKEN=abc\ def cmd`, quotes, `$`, and
// every other shell construct give no word at all. A wrapper (`sudo`, `env`...)
// is not looked through. The words after it are used only to recognise a few
// shapes, and only while each is plain; nothing from them is ever shown except
// a git subcommand.
const COMMAND_WORD = /^[A-Za-z0-9._/-]+(?=\s|$)/
const PLAIN_WORD = /^[A-Za-z0-9._:-]+$/
const WRAPPERS = ['sudo', 'env', 'command', 'exec', 'time', 'nice', 'nohup', 'xargs', 'doas']
const TEST_SCRIPT = /^test(:[A-Za-z0-9._-]+)?$/

// The only git subcommands ever shown: anything else (a word typed in that slot
// could be anything, a token included) is `git git`.
const GIT_SUBCOMMANDS = [
  'add', 'am', 'bisect', 'blame', 'branch', 'checkout', 'cherry-pick', 'clean', 'clone', 'commit', 'config',
  'diff', 'fetch', 'grep', 'init', 'log', 'merge', 'mv', 'pull', 'push', 'rebase', 'reflog', 'remote',
  'reset', 'restore', 'revert', 'rm', 'show', 'stash', 'status', 'switch', 'tag', 'worktree',
]

// A line for the bubble, and what the call was if it is one Rocky reacts to the end of.
type Shape = { line: string; kind?: Kind; runner?: string }

const bashShape = (command: string, v: Verdict): Shape => {
  const text = command.replace(/^\s+/, '')
  const found = COMMAND_WORD.exec(text)
  if (!found) return { line: 'run run' }
  // A script given by path is a file like any other: one in the work folder (or
  // one that cannot be placed) is not named.
  if (found[0].includes('/') && inWork(found[0], v)) return { line: 'run run' }
  // Judged on the whole word, before it is shortened for the bubble.
  const raw = found[0].split('/').filter(part => part !== '').pop() ?? ''
  const name = clean(raw)
  const lower = name.toLowerCase()
  if (name === '' || WRAPPERS.includes(lower) || sensitive(raw)) return { line: 'run run' }

  // The plain words after the command word, up to the first one that is not.
  const after: string[] = []
  let unclear = false // a word that is not plain stopped the list
  for (const word of text.slice(found[0].length).split(/\s+/).filter(w => w !== '').slice(0, 8)) {
    if (!PLAIN_WORD.test(word)) {
      unclear = true
      break
    }
    after.push(word.toLowerCase())
  }
  const isTest =
    ['pytest', 'bats'].includes(lower) ||
    (['npm', 'pnpm', 'yarn'].includes(lower) &&
      (TEST_SCRIPT.test(after[0] ?? '') || (after[0] === 'run' && TEST_SCRIPT.test(after[1] ?? '')))) ||
    ((lower === 'go' || lower === 'cargo') && after[0] === 'test') ||
    (lower === 'claude' && after[0] === 'plugin' && after[1] === 'test') ||
    (lower === 'uv' && after[0] === 'run' && after.includes('pytest'))
  if (isTest) return { line: `test test: ${name}`, kind: 'test', runner: lower }
  if (lower === 'git') {
    const sub = after[0]
    if (sub === undefined) return { line: unclear ? `run run: ${name}` : 'git git' }
    if (sub.startsWith('-')) return { line: 'git git' }
    if (!GIT_SUBCOMMANDS.includes(sub)) return { line: 'git git' }
    return { line: `git git: ${sub}`, kind: sub === 'commit' ? 'commit' : sub === 'push' ? 'push' : undefined }
  }
  // `gh pr merge`: only these literal words count
  if (lower === 'gh' && after[0] === 'pr' && after[1] === 'merge') return { line: `run run: ${name}`, kind: 'merge' }
  return { line: `run run: ${name}` }
}

const fallbackLine = (tool: string) => {
  const name = clean(tool)
  return name === '' ? 'work work' : `work work: ${name}`
}

// The bubble line for a tool call. Never throws: an input that is not what the
// tool's type says falls back to the tool's name.
const narrateLine = (tool: string, input: Readonly<Record<string, unknown>>, v: Verdict): string => {
  try {
    const pathLine = (verb: string, path: unknown) => {
      if (typeof path !== 'string' || inWork(path, v)) return `${verb} ${verb}`
      const name = baseName(path)
      return name === undefined || sensitive(name) ? `${verb} ${verb}` : `${verb} ${verb}: ${clean(name)}`
    }
    switch (tool) {
      case 'Edit':
      case 'Write':
        return pathLine('edit', input.file_path)
      case 'NotebookEdit':
        return pathLine('edit', input.notebook_path)
      case 'Read':
        return pathLine('read', input.file_path)
      case 'Grep':
      case 'Glob':
        return 'search search' // never the pattern: it can be anything the model is looking for
      case 'Bash':
        return typeof input.command === 'string' ? bashShape(input.command, v).line : fallbackLine(tool)
      case 'WebFetch':
      case 'WebSearch':
        return 'look look: web'
      case 'Agent':
      case 'Task': {
        const kind = typeof input.subagent_type === 'string' ? clean(input.subagent_type) : ''
        return kind === '' ? 'friend friend' : `friend friend: ${kind}`
      }
      default: {
        if (tool.startsWith('mcp__')) {
          const parts = tool.split('__')
          const server = parts.length >= 3 ? clean(parts[1] ?? '') : ''
          if (server !== '') return `tool tool: ${server}`
        }
        return fallbackLine(tool)
      }
    }
  } catch {
    return fallbackLine(tool)
  }
}

// The line, and for Bash what kind of command it was (see Shape).
const narrate = (tool: string, input: Readonly<Record<string, unknown>>, v: Verdict): Shape => {
  if (tool === 'Bash' && typeof input.command === 'string') {
    try {
      return bashShape(input.command, v)
    } catch {
      return { line: fallbackLine(tool) }
    }
  }
  return { line: narrateLine(tool, input, v) }
}

// ---- the work guard -------------------------------------------------------
// Rocky is for personal sessions. `allowed($)` is the one question, asked fresh
// (the session's directory and $HOME are read each time, nothing is cached)
// immediately before every effect: opening the pane, creating the ticker, each
// stamp, each state write, each draw. It is true only where the session's
// directory is inside `personalUnder` and NOT inside `workUnder` (work wins).
// Anything it cannot establish is false: a failed read, an unknown HOME, an
// option it cannot parse.
//
// Options: blank (or unset) means the default. Anything else must, after `~/`
// expansion, be an absolute path with at least one segment; `~user`, an
// unexpanded `$`, a relative path or one with no segments is INVALID, and an
// invalid option makes `allowed` false everywhere (so a bad `workUnder` can
// never switch the work check off). Paths compare per segment (so
// ~/Projects/workshop is not inside ~/Projects/work), NFC-normalized and
// case-insensitive (the macOS default), with `.`/`..` and trailing slashes
// normalized. Symlinks are NOT resolved: `$.fs` offers no realpath, so a link
// into a work directory from a personal one is judged by the path it is
// reached by.
const DEFAULT_PERSONAL = '~/Projects/personal'
const DEFAULT_WORK = '~/Projects/work'

type Verdict = {
  ok: boolean
  why: 'ok' | 'work' | 'elsewhere' | 'unknown'
  workSegs?: string[] // the work folder, for hiding work paths in the bubble
  cwd?: string
}

let config: PluginOptions = {} // the plugin's options, as register received them
// The answer of `$.fs.exists` for an explicitly named work folder, taken once per
// session (at its first check) so later checks cost nothing. A failed or missing
// answer stays closed until the next session.start, or /rocky asks again.
let workChecked: { path: string; ok: boolean } | undefined
let lastPre: Promise<unknown> = Promise.resolve() // the latest tool call's start-of-call write
let parked = false // Rocky closed the pane himself (sent away): a later allowed turn opens it again

// An absolute path as lower-cased NFC segments; undefined if it is not
// absolute or `..` climbs out of the root.
// Control characters, backslashes, and whitespace around the path or around any
// segment are refused outright (fail closed): they are how a path is made to
// look like one thing to this check and another to the file system.
const UNCLEAN = /[\u0000-\u001f\u007f\\]/
const toSegments = (path: string): string[] | undefined => {
  if (!path.startsWith('/') || UNCLEAN.test(path) || path !== path.trim()) return undefined
  const out: string[] = []
  for (const raw of path.split('/')) {
    if (raw !== raw.trim()) return undefined
    if (raw === '' || raw === '.') continue
    if (raw === '..') {
      if (out.length === 0) return undefined
      out.pop()
      continue
    }
    out.push(raw.normalize('NFC').toLowerCase())
  }
  return out
}

// `~` and `~/...` expand from HOME; any other `~`, or a `$`, is invalid.
const expand = (text: string, home: string): string | undefined => {
  if (text === '~' || text.startsWith('~/')) return home + text.slice(1)
  if (text.startsWith('~') || text.includes('$')) return undefined
  return text
}

type Root = { segs: string[]; path: string; explicit: boolean }

const optionRoot = (raw: unknown, fallback: string, home: string): Root | undefined => {
  if (raw !== undefined && typeof raw !== 'string') return undefined
  const blank = raw === undefined || raw.trim() === ''
  const text = blank ? fallback : raw
  const full = expand(text, home)
  const segs = full === undefined ? undefined : toSegments(full)
  if (!segs || segs.length === 0 || full === undefined) return undefined
  return { segs, path: full, explicit: !blank && text !== fallback }
}

const within = (dir: string[], root: string[]) =>
  root.length > 0 && root.length <= dir.length && root.every((seg, i) => dir[i] === seg)

// What `judge` can say without touching the file system, and the work folder
// the caller must still find on disk when the option named it explicitly.
type Judged = Verdict & { checkWork?: string }

const judge = (cwd: string, home: string | undefined, options: PluginOptions): Judged => {
  const homeSegs = home === undefined ? undefined : toSegments(home)
  const dir = toSegments(cwd)
  if (home === undefined || !homeSegs || homeSegs.length === 0 || !dir) return { ok: false, why: 'unknown' }
  const personal = optionRoot(options.personalUnder, DEFAULT_PERSONAL, home)
  const work = optionRoot(options.workUnder, DEFAULT_WORK, home)
  if (!personal || !work) return { ok: false, why: 'unknown' }
  const checkWork = work.explicit ? work.path : undefined
  const extra = { checkWork, workSegs: work.segs, cwd }
  if (within(dir, work.segs)) return { ok: false, why: 'work', ...extra } // work wins
  if (!within(dir, personal.segs)) return { ok: false, why: 'elsewhere', ...extra }
  return { ok: true, why: 'ok', ...extra }
}

const verdict = async ($: Judging): Promise<Verdict> => {
  try {
    const cwd = await $.session.cwd()
    const home = await $.env.get('HOME')
    const { checkWork, ...v } = judge(cwd, home, config)
    // A work folder named on purpose must exist: a typo (~/Project/work) would
    // otherwise leave nothing marked as work. A missing DEFAULT is fine. Asked
    // only when the answer would otherwise be yes, and only once per session.
    if (v.ok && checkWork !== undefined) {
      if (workChecked?.path !== checkWork) {
        let found = false
        try {
          found = await $.fs.exists(checkWork)
        } catch {
          found = false // fail closed for this session
        }
        workChecked = { path: checkWork, ok: found }
      }
      if (!workChecked.ok) return { ok: false, why: 'unknown' }
    }
    return v
  } catch {
    return { ok: false, why: 'unknown' }
  }
}

const allowed = async ($: Judging) => (await verdict($)).ok

// Send Rocky away: whatever ticker is running stops and the pane closes,
// whether or not there is a ticker (closing a pane that is not open is
// harmless). `from` is a ticker that is stopping itself: it dies too.
const standDown = async ($: Ticking, from?: Ticker) => {
  if (from) from.dead = true
  const old = ticker
  ticker = undefined
  if (old) old.dead = true
  try {
    old?.cancel()
  } catch {
    // gone either way
  }
  parked = true
  await $.ui.close({ id: PANE }).catch(quiet)
}

export const register: Register = (on, options) => {
  config = options

  on('session.start', async ($, e, next) => {
    // A new session may draw pictures again, and checks its work folder afresh.
    workChecked = undefined
    useImages = true
    altDenies = 0
    otherDenies = 0

    // The command exists everywhere, so /rocky can say why he is not here.
    try {
      await $.command.register({
        name: 'rocky',
        description: 'Show Rocky, the helper in a side pane (personal sessions only)',
      })
    } catch {
      // the pane still opens without its slash command
    }

    // Each effect is asked about just before it, so a `cd` that lands between
    // two of them cannot get the second one through.
    if (!(await allowed($))) {
      // Whatever an earlier session left running stops too, and a pane left
      // open closes (with or without a ticker).
      await standDown($).catch(quiet)
      return next(e)
    }
    try {
      await ensureTicker($, true) // asks again, last
    } catch {
      // a later turn, call or /rocky tries again
    }
    if (await allowed($)) {
      parked = false // opened here: the turn.start reopen has nothing to do
      $.ui.open({ id: PANE, title: TITLE }).catch(quiet)
    }

    // A reload re-runs this: clear whatever mood the last session left.
    try {
      await apply($, m => ({ mood: 'idle', detail: '', gen: m.gen }))
      if (await allowed($)) await update($, fallbackAtom, (): Fallback => 'image')
    } catch {
      // state trouble must not stop the pane opening
    }

    return next(e)
  })

  on('command.run', { command: 'rocky' }, async $ => {
    workChecked = undefined // asked for by the person: look at the work folder again
    const v = await verdict($)
    if (!v.ok) {
      return {
        text: v.why === 'work' ? 'Rocky stays home in work sessions.' : 'Rocky only visits personal sessions.',
      }
    }
    await ensureTicker($).catch(quiet)
    if (await allowed($)) {
      parked = false
      await $.ui.open({ id: PANE, title: TITLE })
    }

    return { text: 'Rocky is here.' }
  })

  // `gen` counts main turn boundaries: a tool call that settles after one it
  // did not start in writes nothing. Only the main turn moves it: turn.start
  // and turn.complete with an agentId (a subagent's loop) do nothing here,
  // while a subagent's tool calls still move Rocky.
  on('turn.start', async ($, e, next) => {
    if (!(await allowed($))) {
      // The directory changed under the session: Rocky leaves (the pane closes
      // even if no ticker is running).
      await standDown($).catch(quiet)
      return next(e)
    }
    await ensureTicker($).catch(quiet)
    // Back from being sent away: the pane we closed opens again.
    // `parked` is read after the answer, in the same breath as it is cleared and
    // the pane opened (as the tool path does), so two callers cannot both open it.
    if (await allowed($)) {
      if (parked) {
        parked = false
        $.ui.open({ id: PANE, title: TITLE }).catch(quiet)
      }
    }
    if (!e.agentId) await apply($, m => ({ mood: 'thinking', detail: '', gen: m.gen + 1 }))

    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (!(await allowed($))) {
      await standDown($).catch(quiet) // idempotent: closes a pane left open
      return next(e)
    }
    // The boundary first, so a call settling during the completion is late.
    if (!e.agentId) await bumpGen($)
    const done = await next(e)
    if (e.agentId) return done

    await apply($, m => ({ mood: 'happy', detail: '', gen: m.gen + 1 }))

    return done
  })

  // The hook awaits nothing of Rocky's: his pre-write and post-write run beside
  // the call, and the result goes back exactly as `next` produced it.
  on('tool.call', async ($, e, next) => {
    let gen: number | undefined
    let shape: Shape = { line: fallbackLine(e.tool) } // narrated from the input in the chain below
    const pre = (async () => {
      const v = await verdict($)
      if (!v.ok) {
        // Sent away: stop any ticker and close the pane (harmless if nothing
        // is open), so a pane left open in a work directory does not stay.
        await standDown($)
        return
      }
      shape = narrate(e.tool, e, v)
      // Back from being sent away: the pane we closed opens again.
      if (parked) {
        parked = false
        $.ui.open({ id: PANE, title: TITLE }).catch(quiet)
      }
      // Worry holds through the next call's start; a call that succeeds ends it.
      void ensureTicker($).catch(quiet)
      await apply($, (m, now) => {
        gen = m.gen
        // a worry that has run out no longer holds
        return m.mood === 'worry' && m.until > now
          ? undefined
          : { mood: 'working', detail: shape.line, gen: m.gen }
      })
    })().catch(quiet)
    lastPre = pre // the permission check (inside `next`) waits for it before it writes

    const ran = await next(e)
    const failed = 'deny' in ran ? ran.deny !== undefined : ran.isError === true
    // The result as the model read it: only a count of failed tests is ever taken
    // from it (see outcome.ts).
    const text = 'text' in ran && typeof ran.text === 'string' ? ran.text : undefined
    void pre
      .then(async () => {
        const started = gen
        if (started === undefined) return
        await apply($, m => {
          if (m.gen !== started) return undefined // a turn boundary passed: late
          const outcome = outcomeFor(shape.kind, shape.runner, failed, text)
          if (outcome) return { ...outcome, gen: m.gen }
          if (failed) return { mood: 'worry', detail: '', gen: m.gen }
          // a question put to the person, or a worry, is over once the call ran
          return m.mood === 'worry' || m.mood === 'thinking'
            ? { mood: 'working', detail: shape.line, gen: m.gen }
            : undefined
        })
      })
      .catch(quiet)

    return ran
  }).catch(($, e, next) => next(e))

  // Observes the permission verdict and changes nothing: what `next` settled to
  // goes back as it came (an ask handed up as heard stays the engine's own).
  // A person is being asked: Rocky wonders, for a minute or until the call goes on.
  on('tool.check', async ($, e, next) => {
    // (A throw from `await next(e)` itself is left to the `.catch`, which re-runs the chain beneath, as documented.)
    const r = await next(e)
    // Nothing here may throw before `return r`: the verdict goes back untouched.
    try {
      if (isPersonAsk(r?.decision, e.approver)) {
        // after the call's own start-of-call write, so the question is the last word
        void lastPre
          .then(() => apply($, m => ({ mood: ASK.mood, detail: ASK.detail, gen: m.gen, ttl: ASK.ttl })))
          .catch(quiet)
      }
    } catch {
      // Rocky's wondering is not worth the verdict
    }
    return r
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e, next) => {
    if (!(await allowed($))) return next(e)
    const table = $.ui.resolve(e)
    const { Box, Text } = table
    // Only the terminal surface has an Image.
    const Image = e.surface === 'terminal' && 'Image' in table ? table.Image : undefined
    const columns = Math.max(1, e.props.bodyColumns)
    const bodyRows = e.props.scroll.bodyRows
    const stored = await read($, moodAtom)
    const frame = await read($, frameAtom)
    const fallback = await read($, fallbackAtom)
    // An expired mood reads as idle; nothing was written to make it so.
    // An unknown mood (a corrupt stored value) reads as idle too.
    const m: RockyMood =
      isMood(stored.mood) && stored.until > frame.now ? stored : { ...stored, mood: 'idle', detail: '' }

    // Blank rows above center the bubble and the figure in a pane that is taller.
    const spacer = (rows: number) =>
      Array.from({ length: Math.max(0, Math.floor((bodyRows - rows) / 2)) }, (_, i) => (
        <Text key={`p${i}`} mono wrap="truncate">{' '}</Text>
      ))

    if (Image && fallback === 'image') {
      const file = framePath($.plugin.root, m.mood, pictureFrame(frame.n, 0))
      const box = imageBox(columns, bodyRows)
      const margin = Math.floor((columns - box.columns) / 2)
      return (
        <Box flexDirection="column">
          {spacer(3 + box.rows)}
          {bubble(lineFor(m), columns, margin + Math.floor(box.columns / 2)).map((row, i) => (
            <Text key={`b${i}`} mono wrap="truncate" dimColor={m.mood === 'idle'}>{row}</Text>
          ))}
          <Box marginLeft={margin}>
            <Image key={IMAGE_KEY} source={{ file, format: 'png' }} columns={box.columns} rows={box.rows} alt={lineFor(m)} />
          </Box>
        </Box>
      )
    }

    const frames = ART[m.mood] ?? ART.idle
    const { rows: art, center } = fit(frames[frame.n % frames.length] ?? [], columns)

    return (
      <Box flexDirection="column">
        {spacer(BLOCK_ROWS)}
        {bubble(lineFor(m), columns, center).map((row, i) => (
          <Text key={`b${i}`} mono wrap="truncate" dimColor={m.mood === 'idle'}>{row}</Text>
        ))}
        {art.map((row, i) => (
          <Text key={`a${i}`} mono wrap="truncate">{row || ' '}</Text>
        ))}
      </Box>
    )
  })
}

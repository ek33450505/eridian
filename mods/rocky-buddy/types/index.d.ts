export type Mood = 'idle' | 'thinking' | 'working' | 'worry' | 'happy'

// `seq` rises with every mood change. `gen` rises at each main turn.start and
// turn.complete: a tool call that settles under another gen than it started in
// writes nothing. `until` is the clock time the mood is shown until; later, it
// reads as idle (nothing is written to expire it). `friendStarts` are the start
// times of the subagents running: the bubble counts those started less than 30
// minutes before the draw, entry by entry.
export type RockyMood = {
  mood: Mood
  detail: string
  seq: number
  gen: number
  until: number
  friendStarts: number[]
  // True when the assistant set this mood itself (rocky_mood): later tool-call
  // narration does not replace it while it lasts.
  says: boolean
}

// The subagents Rocky has seen start and not yet end, by agent id: when, and
// the (sanitized) type to name them by. An entry older than 30 minutes is a
// completion that never came, and is dropped. `gone` marks the other order: an
// end seen before its start (kept for a minute, so the late start is dropped).
export type Friend = { at: number; type: string; gone?: true }
export type Friends = Record<string, Friend>

// `n` picks the animation frame; `now` is the clock at the last tick.
export type Frame = { n: number; now: number }

// Whether the pane draws the picture or the ASCII figure.
export type Fallback = 'image' | 'ascii'

declare module 'claude-code' {
  interface PluginState {
    'rocky-buddy': {
      mood: Shaped<RockyMood>
      frame: Shaped<Frame>
      fallback: Shaped<Fallback>
      friends: Shaped<Friends>
    }
  }
}

export type Mood = 'idle' | 'thinking' | 'working' | 'worry' | 'happy'

// `seq` rises with every mood change. `gen` rises at each main turn.start and
// turn.complete: a tool call that settles under another gen than it started in
// writes nothing. `until` is the clock time the mood is shown until; later, it
// reads as idle (nothing is written to expire it).
export type RockyMood = { mood: Mood; detail: string; seq: number; gen: number; until: number }

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
    }
  }
}

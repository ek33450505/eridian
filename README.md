# hey-claude

Hands-free, fully-local voice conversation with Claude Code on macOS. Say "hey claude", talk, and hear the reply. Interrupt it by talking over it.

- **Local audio only.** Wake word, VAD, speech-to-text and text-to-speech all run on-device. No API keys, and no audio leaves the Mac. The text of each turn still goes to Claude.
- **Two front ends, one core.** (A) Speak replies from, and send voice into, real Claude Code sessions (Stop hook + Channels). (B) A standalone voice REPL driving a persistent `claude -p` stream-json process.

**Status:** planning / Phase 0 spike. Nothing runnable yet.

See [`docs/plan.md`](docs/plan.md) for the research, verified component picks, architecture and phased plan.

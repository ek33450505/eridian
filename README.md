# undertone

Hands-free, local-first voice conversation with Claude Code on macOS. Say "hey jarvis", talk, and hear the reply. Interrupt it by talking over it.

- **No audio leaves the Mac.** Wake word, VAD, speech-to-text and text-to-speech all run on-device. No API keys, no quotas, no cloud fallbacks. Only the transcribed text of each turn goes to Claude.
- **Claude by default, fully offline if you want it.** Replies come from Claude for quality. An opt-in `--offline` mode swaps in a local model (Ollama), and then the whole loop works with Wi-Fi off.
- **Two front ends, one core.** (A) Speak replies from, and send voice into, real Claude Code sessions (Stop hook + Channels). (B) A standalone voice REPL driving a persistent `claude -p` stream-json process.

**Status:** planning / Phase 0. Nothing runnable yet. `training/` holds an optional Docker image for training a custom wake phrase locally.

See [`docs/plan.md`](docs/plan.md) for the research, verified component picks, architecture and phased plan.

# undertone

Hands-free, local-first voice conversation with Claude Code on macOS. Say "hey jarvis", talk, and hear the reply. Interrupt it by talking over it.

- **No audio leaves the Mac.** Wake word, VAD, speech-to-text and text-to-speech all run on-device. No API keys, no quotas, no cloud fallbacks. Only the transcribed text of each turn goes to Claude.
- **Claude by default, fully offline if you want it.** Replies come from Claude for quality. An opt-in `--offline` mode swaps in a local model (Ollama), and then the whole loop works with Wi-Fi off.
- **Two front ends, one core.** (A) Speak replies from, and send voice into, real Claude Code sessions (Stop hook + Channels). (B) A standalone voice REPL driving a persistent `claude -p` stream-json process.

**Status:** Phase 1 (speak replies) built. `training/` holds an optional Docker image for training a custom wake phrase locally.

See [`docs/plan.md`](docs/plan.md) for the research, verified component picks, architecture and phased plan.

## Phase 1: spoken replies

A tiny local speaker daemon speaks a short summary of each Claude Code reply out loud, for sessions that opt in. Nothing speaks unless you turn it on.

```sh
uv sync                 # install (stdlib only, no runtime deps)
uv run undertone serve  # run the speaker daemon in the foreground
uv run undertone on     # opt this machine in to speaking replies
uv run undertone off    # opt back out
uv run undertone status # daemon up? opted in?
```

Opt a single session in without touching the flag file:

```sh
UNDERTONE_SPEAK=1 claude
```

Register the hooks in your Claude Code `settings.json` (prints the JSON block, doesn't install it):

```sh
uv run undertone hooks-snippet
```

This wires a `Stop` hook (async, speaks the reply) and a `UserPromptSubmit` hook (asks Claude to end each reply with one `SPOKEN: ...` line, used verbatim when present; otherwise the reply is stripped of code/markdown/links and the first couple of sentences are spoken). The daemon never speaks on `stop_hook_active` turns (hook-forced continuations), and env `UNDERTONE_SAY_CMD` (default `say`) / `UNDERTONE_VOICE` (default `Evan (Enhanced)`, a one-time download in System Settings → Accessibility → Spoken Content → Manage Voices) are overridable — tests always point `UNDERTONE_SAY_CMD` at a stub, never real audio.

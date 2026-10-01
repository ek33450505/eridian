# undertone

Hands-free, local-first voice loop for Claude Code on macOS (Apple Silicon). Say "hey jarvis" and ask: either into your running interactive Claude Code session (`undertone attach` + `undertone session`), or in a standalone voice conversation (`undertone chat`). You hear Claude's answer, and saying "hey jarvis" again mid-reply cuts it off — or talk over it without the wake word. For 6 seconds after an answer you can ask a follow-up without the wake word.

## Local-first by design (the point of this repo)

- **No audio leaves the Mac.** Microphone input, wake word detection, VAD, speech-to-text, and text-to-speech all run on-device in the pipeline.
- **No API keys for the pipeline itself.** It uses your existing Claude Code login. No TTS quotas, no STT rates, no separate subscriptions.
- **No cloud fallbacks.** The entire pipeline is local or Claude-only; no silent OpenAI TTS handoff, no degraded-mode alternatives.
- **Text, not audio, goes to Anthropic.** That's each turn's transcribed question, plus the normal Claude Code traffic: files Claude reads (working directory only) and its WebSearch queries. Contrast: Claude Code's `/voice` streams the raw audio to Anthropic; VoiceMode defaults to OpenAI's `https://api.openai.com/v1` for STT with a silent TTS fallback.
- **You can verify it yourself.** See "Verify it yourself" below.

## What it is

undertone lets you talk with Claude without typing: microphone → wake word → silence detection → speech-to-text → Claude → sentence splitting → text-to-speech. Everything except Claude itself runs on your Mac. There are two front ends: **(A)** voice into a running Claude Code session (via a **Channel** MCP server), using `undertone attach` + `undertone session`, and **(B)** the standalone `undertone chat` voice REPL. This README covers both; architecture details are in [`docs/architecture.md`](docs/architecture.md).

### One-time network actions (the only Hugging Face contact)

- `undertone models fetch` downloads the ~2.5 GB STT model from Hugging Face once. After that, `HF_HUB_OFFLINE=1` is forced at import, so the Hub is never contacted again.
- The optional `training/` Docker image downloads datasets and checkpoints when built or used.
- Everything else: local or Claude-only.

### Verify it yourself

**Offline pipeline check:** run `bash scripts/verify-offline.sh`. It generates a "Hey Jarvis. … What is the capital of France?" clip with `say`, then runs wake word → VAD → STT → TTS inside a macOS `sandbox-exec` profile that denies all network access. The check is self-proving: a canary inside the sandbox must fail to connect and resolve, or the check fails.

```
[PASS] network canary - TCP and DNS both blocked
[PASS] clip format - 16 kHz mono 16-bit
[PASS] wake word - 1 wake event(s)
[PASS] VAD + STT - heard ['What is the capital of France?'], expected to contain 'capital of france'
[PASS] TTS exit - say rc=0
[PASS] TTS output - 102246 bytes
RESULT: PASS
OFFLINE CHECK: PASS (wake, VAD, STT, TTS ran with network denied)
```

(Why not Docker `--network none`? MLX and `say` are macOS-only.)

**Live connection check:** while `undertone chat` is running, in another terminal:
```bash
pid=$(pgrep -nf 'undertone chat')               # newest match: undertone's Python process
lsof -a -i -n -P -p "$pid"                      # expect no output: no sockets
lsof -a -i -n -P -p "$(pgrep -d, -P "$pid")"    # its children (the claude child): expect only :443 to Anthropic
```

Measured 2026-10-01 during a WebSearch turn: the undertone Python process had **zero sockets**. The `claude` child had only TCP :443 to `160.79.104.10` and `2607:6bc0::10`. `whois` says both belong to **Anthropic, PBC**. `dig +short api.anthropic.com` returns exactly those. This is a snapshot, not a proof—run it yourself.

## What it looks like

```
undertone chat: working in /Users/edkubiak/Projects/personal/undertone
Claude can read and search files under it and search the web; it cannot run commands or edit files.
Say 'hey jarvis'. Ctrl-C to quit.
you: Let's start a new greenfield project.
Sure, happy to start something new. What do you want to build, and do you have a name or language in mind?
[latency] speech end → first say: 3.15 s (first token 1.95 s)
```

## Quick start

Requirements: macOS on Apple Silicon, `uv`, Claude Code logged in, and microphone permission for your terminal app. For Evan (Enhanced), go to System Settings → Accessibility → Spoken Content → System Voice → Manage Voices → English (US) → Evan.

```bash
uv sync
uv run undertone models fetch     # one-time ~2.5 GB download
uv run undertone chat             # say "hey jarvis"
```

Run `chat` from a project directory, because Claude's file tools are confined to it.

## Usage

### Voice into a running Claude Code session (Path A)

**Terminal 1:** Run the voice input listener.
```bash
uv run undertone attach [--no-beep] [--no-aec] [--follow-up SECONDS]
```
- `--follow-up SECONDS`: seconds to keep listening after a reply without the wake word (default 6; 0 disables)
- `--no-beep`: skip the audible wake/follow-up cues
- `--no-aec`: play speech with `say` directly (no echo cancellation, no talk-over, wake-word barge-in only); use if AEC misbehaves
- Say "hey jarvis", then your question. It lands in the session, and Claude's `reply` is spoken aloud. The follow-up window opens only after an answer, never after an acknowledgement or a confirmation request.
- **Echo cancellation on:** talk over a reply to stop it without the wake word. **Echo cancellation off:** say "hey jarvis" to stop a reply.

**Terminal 2:** from **your project directory**, start an interactive Claude Code session wired to receive voice turns. Use the venv's executable, because `uv run` only works inside the undertone checkout:
```bash
~/Projects/personal/undertone/.venv/bin/undertone session [claude args ...]
```

This launches `claude --mcp-config '<inline json>' --settings '<inline json>' --dangerously-load-development-channels server:undertone --allowedTools mcp__undertone__reply`, with the voice-turn hard control (`voice_guard`) injected via `--settings`. Your voice input from Terminal 1 arrives as `← undertone: <text>`, and Claude can call the `reply` tool to speak answers. The command generates a session token and refuses `--settings`, `--settings=…`, `--bare` and `--safe-mode`, since each would override the guard.

### Standalone voice conversation (Path B)

`undertone chat [--no-beep] [--no-aec] [--resume] [--follow-up SECONDS]`
- `--follow-up SECONDS`: how long after a reply you can ask a follow-up without the wake word (default 6; 0 disables)
- `--resume`: continue the last chat session (uses the saved session ID)
- `--no-beep`: no cue sounds (normally Tink on wake, and Pop when the follow-up window opens)
- `--no-aec`: play speech with `say` directly (no echo cancellation, no talk-over, wake-word barge-in only); use if AEC misbehaves
- Ctrl-C to quit
- **Echo cancellation on:** talk over a reply to stop it without the wake word. **Echo cancellation off:** say "hey jarvis" to stop a reply.

### Utilities

- `undertone listen`: mic → wake word → VAD → STT; prints one JSON event per line (debugging)
- `undertone models fetch`: download the STT model (one-time ~2.5 GB)

## Architecture

```
Path A (undertone attach + undertone session)
  mic ──► DuplexAudio (AEC) ──► wake word → VAD → STT → voice.sock → channel (mcp_channel.py) → Claude Code session
   ▲                            │ (also active during playback = barge-in, talk-over)            │ reply tool
   │                            ▼                                                                ▼
  speaker ◄── rendered TTS ◄── sentence chunker ◄── voice.sock ◄── channel ◄────────────────────┘

Path B (undertone chat)
  mic ──► DuplexAudio (AEC) ──► wake word → VAD → STT ──► brain (persistent claude -p)
   ▲                            │ (also active during playback = barge-in, talk-over)            │ streamed text
   │                            ▼                                                                ▼
  speaker ◄── rendered TTS ◄── sentence chunker ◄────────────────────────────────────────────────┘
```

The wake detector stays live while Claude thinks or speaks. Saying "hey jarvis" again stops the speech (barge-in). You can also talk over a reply to stop it without the wake word (echo cancellation provides the speaker reference signal). In `chat`, both also interrupt Claude's turn. A channel can't interrupt a running turn, so in Path A your new question queues until Claude finishes.

### Pipeline details

- **Echo cancellation:** WebRTC AEC3 via livekit's `AudioProcessingModule(echo_cancellation=True, noise_suppression=True, high_pass_filter=True)`. Full-duplex 16 kHz mono int16 stream paired with rendered speech. Offline measurement (2026-10-01): ERLE ≈ 35 dB, Silero VAD fired on 0/217 residual chunks (189/217 without AEC). **NOT yet live-tested with real speakers and mic.**
- **Wake word:** pymicro-wakeword with built-in "Hey Jarvis" model (0.97 cutoff, 10 ms frames at 16 kHz)
- **VAD:** Silero VAD end-of-turn detection after 0.7 s of silence; talk-over at 0.3 s of sustained speech (≥0.6 confidence)
- **STT:** parakeet-mlx `mlx-community/parakeet-tdt-0.6b-v3` (in-memory float32 processing)
- **Brain:** persistent `claude -p` stream-json subprocess with file tools confined to working directory
- **Chunker:** splits Claude's reply into sentences
- **TTS:** each sentence rendered to 16 kHz mono int16 WAV by `say -o` (rendering time ~0.62 s per 4.7 s sentence), then played through the echo canceller for a reference signal. macOS `say` voice is **Evan (Enhanced)** (default); override with `UNDERTONE_VOICE` env var

### Measured latencies (M4 Max)

- Wake word: 6/6 detected in 30-min live run, 0 false accepts, 2.3% CPU average
- STT: median 87 ms (44 ms on synthetic speech)
- Claude first token: 1.0–2.0 s warm, 2.0–2.5 s on first turn (including spawn)
- `say` first audio: 0.73 s
- **End-to-end (speech end → first `say`):** 3.15 s (first token 1.95 s)

### Modules

`audio.py`, `listener.py`, `stt.py`, `brain.py`, `chunker.py`, `duplex.py`, `player.py`, `chat.py`, `speaker.py`, `spoken.py`, `mcp_channel.py`, `voice.py`, `voice_guard.py`, `paths.py`, `cli.py`.

## Safety model for voice

### Path A: voice into a Claude Code session

Voice turns keep full session power but are unverified input. A hard control (`voice_guard.py`) gates tool calls during spoken turns: before anything destructive, irreversible or outward-facing, Claude must send a `reply` tool with `kind="confirm"` asking the user to speak the wake word, then "confirm <word1> <word2>" with two uncommon nouns. The listener strips the wake word from every transcript, so Claude receives "confirm <word1> <word2>". The words must come immediately after "confirm"; case and punctuation are ignored. Both words must be at least 3 letters, uncommon, and fresh (not used before in the session); one attempt, valid 300 s. Any other message cancels the challenge. On a successful confirm, gated calls are allowed for the rest of the turn. The confirm flow is enforced by hooks injected into the session; see [`SECURITY.md`](SECURITY.md) §7a for the gating rules and residual risks.

- **Confirm by wake word only:** every utterance carries a `via` field (`wake`, `follow_up`, or `talk_over`). Only a wake-word turn (`via: "wake"`) can satisfy a confirm challenge, preventing a mishearing of ambient audio or undertone's own speech (via echo cancellation imperfection) from bypassing the guard. Talk-over and follow-up turns cannot grant gated calls.

- **Talk-over barge-in:** while Claude is speaking, 0.3 s of sustained speech stops the reply without saying the wake word. Your speech becomes your next turn, with 0.5 s of your first words preserved. Exception: while a confirm prompt (or reply of unknown kind) is playing, talk-over stops the speech but you must still say "hey jarvis" plus the answer. In `chat`, talk-over also interrupts Claude's in-flight turn.

- **Wake word never spoken:** `speaker.build_say_argv()` runs `spoken.sanitize_for_tts()`, which strips invisible characters, removes `[` and `]` (macOS `say` uses `[[…]]` for inline commands), and replaces any utterance of "hey jarvis" or obfuscated forms with "the wake word". Undertone's instructions also tell Claude never to say it.

### Path B: standalone `undertone chat`

Headless `claude -p` bypasses Claude Code's interactive confirm-pauses, and the microphone is open. This is fail-closed by design:

- **Restricted tools:** runs with `--restricted --strict-mcp-config --tools Read,Grep,Glob,WebSearch --permission-prompts none`. File tools (Read, Glob, Grep) are confined to the working directory; `~/.ssh/config` and symlinks pointing outside it are refused. No Bash (even `git diff --output=` writes files), no WebFetch (exfiltration channel for prompt-injected content).
- **Verified init:** every `system/init` must advertise exactly that tool set and no MCP servers. Any output before a verified init kills the child.
- **Safe workspace fallback:** when started from a directory that's too broad or sensitive (home or any ancestor, any dot-directory under home, `~/Library`, top level of `~/Documents`/`~/Desktop`/`~/Downloads`), it switches to an empty `~/.undertone/workspace`.
- **Terminal safety:** control characters are stripped from model output. `say` always gets `--` before the text (guards against argv flag injection).
- **Hook isolation:** CAST/user hooks don't run in the voice child.

### Why voice can't change your code (Path B only)

Code changes belong in Path A (a real session with its full tools and guards), not in standalone voice. `undertone chat` has no Write or Edit tools.

## Components and licenses

| Component | Role / license |
|---|---|
| pymicro-wakeword 2.5.0 | wake word; Apache-2.0 |
| "Hey Jarvis" model (built in) | wake model by Kevin Ahrendt (microWakeWord), shipped inside the Apache-2.0 package; no separate model license or training-data terms are published |
| pysilero-vad 3.4.0 | VAD; MIT (Silero VAD) |
| parakeet-mlx 0.5.2 | STT package; Apache-2.0 |
| parakeet-tdt-0.6b-v3 | STT model by NVIDIA; **CC-BY-4.0** — attribution: NVIDIA |
| mlx | ML framework; MIT |
| sounddevice 0.5.6 | Audio I/O; MIT |
| livekit 1.1.20 | echo cancellation; Apache-2.0 |
| numpy | Linear algebra; BSD-3-Clause and others |
| macOS `say` | text-to-speech; part of macOS |
| Claude Code | Anthropic's terms |

**Optional training:** `training/` contains an optional Docker image for training custom wake phrases. Uses openWakeWord 0.6.0: code Apache-2.0, but pretrained models and **any trained model are CC BY-NC-SA 4.0 (non-commercial)**. See [`training/README.md`](training/README.md).

## Status and roadmap

| Phase | What | State |
|---|---|---|
| **0** | measurement spike (latency, CPU, false-accept rate) | ✓ done |
| **1** | spoken replies in real Claude Code sessions via Stop hook | ✓ retired, replaced by Phase 3 |
| **2a** | listening core: `undertone listen` prints JSON events, `undertone models fetch` | ✓ done |
| **2b** | `undertone chat [--no-beep] [--resume] [--follow-up SECONDS]` (standalone voice REPL) | ✓ done |
| **D** | documentation: this README, local-first, architecture, training, SECURITY, CHANGELOG, offline check | ✓ done (refreshed at each phase end) |
| **3** | voice into *running* Claude Code sessions (Channel MCP server + reply tool) | ✓ done (PR #4) |
| **4a** | echo cancellation via livekit AEC + talking over replies without wake word | built, live test pending |
| **4b** | Kokoro voice | planned |
| **5** | opt-in `--offline` brain (Ollama `gpt-oss:20b`); entire loop works with Wi-Fi off | planned |

For the full research and component justification: see [`docs/plan.md`](docs/plan.md).

## Testing

359 unit tests run with `uv run python -m unittest discover -s tests`. They use fakes only: no microphone, no speakers, no real `claude`, never `~/.undertone`. Temp directories and stubs isolate each test.

Plus `bash scripts/verify-offline.sh` for the offline pipeline check: wake word → VAD → STT → TTS inside a `sandbox-exec` profile that denies all network access. Now also covers echo cancellation (ERLE ≥ 20 dB, and no speech-like run on the residual long enough to trigger talk-over). See "Verify it yourself" above.

## Environment variables

- `UNDERTONE_HOME` (default `~/.undertone`) — state directory for sockets and workspace
- `UNDERTONE_SESSION` (set by `undertone session`) — session token passed to the channel via MCP config and to hooks; internal use only
- `UNDERTONE_VOICE` — voice name for `say` (e.g., `"Samantha"`); empty string uses system voice
- `UNDERTONE_SAY_CMD` (tests only) — override `say` with a stub
- `UNDERTONE_CLAUDE_CMD` (tests only) — the binary the safety flags are applied to; `system/init` checks that it actually advertises the restricted tool set, so a wrapper that drops the flags is caught

## Documentation

- [`docs/local-first.md`](docs/local-first.md) — data-flow audit, verify scripts, one-time network actions
- [`docs/architecture.md`](docs/architecture.md) — design rationale, module breakdown, protocol details
- [`docs/training.md`](docs/training.md) — custom wake phrase training with Docker
- [`docs/plan.md`](docs/plan.md) — research, component picks, and phase breakdown
- [`SECURITY.md`](SECURITY.md) — safety model, sandbox details, threat model
- [`CHANGELOG.md`](CHANGELOG.md) — version history
- [`training/README.md`](training/README.md) — wake-phrase training setup
- [`LICENSE`](LICENSE) — Apache-2.0

---

**License:** Apache-2.0 (see [`LICENSE`](LICENSE)).

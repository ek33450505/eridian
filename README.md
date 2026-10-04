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

- `undertone models fetch` downloads the STT model (~2.5 GB) and the Kokoro voice (57 files) from Hugging Face once. After that, `HF_HUB_OFFLINE=1` is forced at import, so the Hub is never contacted again.
- The optional `training/` Docker image downloads datasets and checkpoints when built or used.
- Everything else: local or Claude-only.

### Verify it yourself

**Offline pipeline check:** run `bash scripts/verify-offline.sh`. It generates a "Hey Jarvis. … What is the capital of France?" clip with `say`, then runs wake word → VAD → STT → TTS → Kokoro → self-screen → echo cancellation inside a macOS `sandbox-exec` profile that denies all network access. The check is self-proving: a canary inside the sandbox must fail to connect and resolve, or the check fails.

```
offline check
  [PASS] network canary - TCP and DNS both blocked
  [PASS] clip format - 16 kHz mono 16-bit
  [PASS] wake word - 1 wake event(s)
  [PASS] VAD + STT - heard ['What is the capital of France?'], expected to contain 'capital of france'
  [PASS] TTS exit - say rc=0
  [PASS] TTS output - 102246 bytes
  [PASS] Kokoro telemetry off - ORT_DISABLE_TELEMETRY=1
  [PASS] Kokoro render - 2 pieces, 3.87 s audio, first audio in 0.286 s
  [PASS] Kokoro self-screen - homophone blocked; ordinary sentence played
  [PASS] echo cancellation - ERLE 43.9 dB (need >= 20); longest speech-like run 3 chunks (talk-over needs 10); VAD>=0.5 on 3/177 chunks
RESULT: PASS
OFFLINE CHECK: PASS (wake, VAD, STT, TTS, Kokoro, self-screen, echo cancellation ran with network denied)
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

Requirements: macOS on Apple Silicon, `uv`, Claude Code logged in, and microphone permission for your terminal app. For Evan (Enhanced), go to System Settings → Accessibility → Spoken Content → System Voice → Manage Voices → English (US) → Evan. For Kokoro, install espeak-ng via Homebrew.

```bash
brew install espeak-ng
uv sync
uv run undertone models fetch     # one-time ~2.5 GB download (STT + Kokoro voice)
uv run undertone chat             # say "hey jarvis"
```

Run `chat` from a project directory, because Claude's file tools are confined to it.

## Usage

### Voice into a running Claude Code session (Path A)

**Terminal 1:** Run the voice input listener.
```bash
uv run undertone attach [--no-beep] [--no-aec] [--announce {all,permission,off}] [--follow-up SECONDS] [--tts {kokoro,say}]
```
- `--announce {all,permission,off}`: announcements when Claude finishes or needs you (default `all`; `permission` skips "finished"; `off` disables)
- `--follow-up SECONDS`: seconds to keep listening after a reply without the wake word (default 6; 0 disables)
- `--no-beep`: skip the audible wake/follow-up cues
- `--no-aec`: play speech with `say` directly (no echo cancellation, no talk-over, wake-word barge-in only); use if AEC misbehaves
- `--tts {kokoro,say}`: TTS engine (default `kokoro`). Falls back to `say` if espeak-ng is missing or the model isn't fetched.
- Say "hey jarvis", then your question. It lands in the session, and Claude's `reply` is spoken aloud. The follow-up window opens only after an answer, never after an acknowledgement or a confirmation request.
- **Echo cancellation on:** talk over a reply to stop it without the wake word. **Echo cancellation off:** say "hey jarvis" to stop a reply.

**Terminal 2:** from **your project directory**, start an interactive Claude Code session wired to receive voice turns. Use the venv's executable, because `uv run` only works inside the undertone checkout:
```bash
~/Projects/personal/undertone/.venv/bin/undertone session [claude args ...]
```

This launches `claude --mcp-config '<inline json>' --settings '<inline json>' --dangerously-load-development-channels server:undertone --allowedTools mcp__undertone__reply`, with the voice-turn hard control (`voice_guard`) injected via `--settings`. Your voice input from Terminal 1 arrives as `← undertone: <text>`, and Claude can call the `reply` tool to speak answers. undertone announces when Claude finishes or needs your permission, opening a no-wake-word follow-up window after "Claude finished." The command generates a session token and refuses `--settings`, `--settings=…`, `--bare` and `--safe-mode`, since each would override the guard.

### Standalone voice conversation (Path B)

`undertone chat [--no-beep] [--no-aec] [--resume] [--follow-up SECONDS] [--tts {kokoro,say}]`
- `--follow-up SECONDS`: how long after a reply you can ask a follow-up without the wake word (default 6; 0 disables)
- `--resume`: continue the last chat session (uses the saved session ID)
- `--no-beep`: no cue sounds (normally Tink on wake, and Pop when the follow-up window opens)
- `--no-aec`: play speech with `say` directly (no echo cancellation, no talk-over, wake-word barge-in only); use if AEC misbehaves
- `--tts {kokoro,say}`: TTS engine (default `kokoro`). Falls back to `say` if espeak-ng is missing or the model isn't fetched.
- Ctrl-C to quit
- **Echo cancellation on:** talk over a reply to stop it without the wake word. **Echo cancellation off:** say "hey jarvis" to stop a reply.

### Utilities

- `undertone listen`: mic → wake word → VAD → STT; prints one JSON event per line (debugging)
- `undertone models fetch`: download the STT model (one-time ~2.5 GB)
- `undertone safe-phrase [--clear | --status]`: set your own voice-confirm phrase (hidden prompt, entered twice; 1–3 words, at least 2 words or 8 letters). It's stored only as an scrypt hash in `~/.undertone/safe_phrase.json`. Pick something you never say otherwise, and don't say it on calls or near recording devices: anyone who hears it can confirm. Restart `attach` and `session` after changing it.

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

- **Echo cancellation:** WebRTC AEC3 via livekit's `AudioProcessingModule(echo_cancellation=True, noise_suppression=True, high_pass_filter=True)`. Full-duplex 16 kHz mono int16 stream paired with rendered speech. Offline measurement (2026-10-01): ERLE ≈ 35 dB, Silero VAD fired on 0/217 residual chunks (189/217 without AEC). Live-tested 2026-10-02 on the MacBook Pro speakers and mic in one room; other rooms, devices and volumes are untested.
- **Wake word:** pymicro-wakeword with built-in "Hey Jarvis" model (0.97 cutoff, 10 ms frames at 16 kHz)
- **VAD:** Silero VAD end-of-turn detection after 0.7 s of silence; talk-over at 0.3 s of sustained speech (≥0.6 confidence)
- **STT:** parakeet-mlx `mlx-community/parakeet-tdt-0.6b-v3` (in-memory float32 processing)
- **Brain:** persistent `claude -p` stream-json subprocess with file tools confined to working directory
- **Chunker:** splits Claude's reply into sentences
- **TTS:** Kokoro-82M (default; phoneme-based, ~0.085 s per second of audio) or macOS `say` (fallback). Kokoro uses espeak-ng for grapheme-to-phoneme, onnxruntime 1.30 for inference. `say` voice is **Evan (Enhanced)**; override with `UNDERTONE_VOICE` env var. Kokoro voice is **am_adam**; override with `UNDERTONE_KOKORO_VOICE`. All TTS output is run through a single scrub (`speaker.prepare_speech`), then played through the echo canceller for a reference signal

### Measured latencies (M4 Max)

- Wake word: 6/6 detected in 30-min live run, 0 false accepts, 2.3% CPU average
- STT: median 87 ms (44 ms on synthetic speech)
- Claude first token: 1.0–2.0 s warm, 2.0–2.5 s on first turn (including spawn)
- TTS: `say` first audio ~0.54–0.56 s (process startup dominates); Kokoro ~0.05 s + 0.085 s per second of audio (first piece capped at 32 phoneme tokens ≈ 5–6 words, so first audio is 0.12–0.27 s on an idle M4 Max, after trimming ~0.4 s of leading silence; other onnxruntime processes running at the same time slow it down)
- **End-to-end (speech end → first audio):** `say` ~3.15 s (first token 1.95 s); Kokoro is faster due to lower TTS latency

### Modules

`audio.py`, `listener.py`, `stt.py`, `brain.py`, `chunker.py`, `duplex.py`, `player.py`, `chat.py`, `speaker.py`, `spoken.py`, `mcp_channel.py`, `voice.py`, `voice_guard.py`, `announce.py`, `paths.py`, `cli.py`.

## Safety model for voice

### One scrub, one gate

Every text that undertone speaks goes through `speaker.prepare_speech` before any TTS engine sees it: NUL-strip, NFKC normalize, `spoken.sanitize_for_tts` (wake-word replace, bracket strip, zero-width chars), and a 1000-character cap (`MAX_SPEECH_CHARS`). This is the single place text is cleaned for TTS. Both `say` and Kokoro-82M exit via this scrub.

### Acoustic self-screen (Kokoro only)

Homophones ("hey jarviss", "djarvis", "jarvice") pass the text scrub but fire the wake detector when spoken by Kokoro. `screen.ScreenedSink` wraps the echo-cancellation playback path (AEC on only; `--no-aec` bypasses this): before each speaker chunk plays, a fresh wake detector (cutoff `0.8`; live detector fires at 0.97) runs on the last 2 s of played audio plus lookahead silence. A firing chunk is dropped, with `[skipped speech that sounded like the wake word]`. Calibration: 0/200 ordinary sentences blocked; 67/80 homophone attack clips blocked. Fails closed: any detector error blocks the chunk.

### Path A: voice into a Claude Code session

Voice turns keep full session power but are unverified input. A hard control (`voice_guard.py`) gates tool calls during spoken turns. Built-in read tools, read-only shell commands, test runs and five read-only cast-record MCP tools (`cast_decisions`, `cast_incidents`, `cast_cost`, `cast_sessions`, `cast_ask`) run ungated on a voice turn; every other tool call needs a confirm. For destructive, irreversible or outward-facing actions, Claude makes the tool call first; after the voice guard refuses it, Claude sends a `reply` tool with `kind="confirm"`. If you have set a safe phrase (`undertone safe-phrase`), you confirm by saying the wake word, then your phrase. Claude never sees the phrase; it gets `[safe phrase]`. Without one, Claude asks you to say the wake word, then "confirm <word1> <word2>" with two uncommon nouns. The listener strips the wake word from every transcript, so Claude receives "confirm <word1> <word2>". The words must come immediately after "confirm"; case and punctuation are ignored. Both words must be at least 3 letters, uncommon, and fresh (not used before in the session); one attempt, valid 300 s. Any other message cancels the challenge. On a successful confirm, gated calls are allowed for the rest of the turn. Before Claude's confirm prompt, undertone says what its guard blocked ("undertone blocked: …") in its own voice, a second voice Claude's replies never use (`af_heart` for Kokoro, `Samantha` for `say`). The read-back uses fixed words, short names and location classes, never argument values. It reports what was blocked; it does not limit what the confirm then allows. The confirm flow is enforced by hooks injected into the session; see [`SECURITY.md`](SECURITY.md) §7a for the gating rules and residual risks.

- **Confirm by wake word only:** every utterance carries a `via` field (`wake`, `wake_barge_in`, `follow_up`, or `talk_over`). Only an exact wake-word turn (`via: "wake"`, detected during silence) can satisfy a confirm challenge. A wake word heard while undertone is speaking, or within 1–10 s after (configurable via `UNDERTONE_SELF_WAKE_GUARD_S`, default 1.0 s), lands as `via: "wake_barge_in"` and cannot grant gated calls—fail-closed. Talk-over and follow-up turns also cannot grant gated calls. While a confirm is pending, such a too-early wake turn isn't sent at all: undertone says "That was too soon after I spoke…" and the confirm stays pending, so wait a moment after it stops speaking. This prevents ambient audio, undertone's own speech leaking through echo cancellation imperfection, or Kokoro homophones from bypassing the guard.

- **Talk-over barge-in:** while Claude is speaking, 0.3 s of sustained speech stops the reply without saying the wake word. Your speech becomes your next turn, with 0.5 s of your first words preserved. Exception: a confirm prompt (or a reply of unknown kind) can't be talked over, so you always hear the whole phrase; "hey jarvis" still interrupts it, and you answer with "hey jarvis" plus the phrase. In `chat`, talk-over also interrupts Claude's in-flight turn.

- **Confirmation hint:** the new phrase is `[confirmation needed: when I finish, say "hey jarvis" and the phrase]`, clarifying that a silent period before the wake word is required.

- **Announcements:** `undertone attach` speaks fixed templates ("Claude finished.", "Claude needs your permission.", etc.) when Claude ends a turn or asks for permission, and only for the session it is attached to. They use the same speech path as replies (one text scrub, plus the acoustic self-screen with Kokoro), and never include Claude's text. The `--announce` flag controls them. "Claude finished." opens the follow-up window; the other announcements don't. A follow-up turn can't grant a confirmation, and "Claude finished." is suppressed while a confirm is pending, so its window can't use up the confirm challenge.

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
| Kokoro-82M weights | TTS model; Apache-2.0 |
| onnxruntime 1.30 | ONNX runtime; MIT |
| espeak-ng | grapheme-to-phoneme for Kokoro; GPL-3.0-or-later (installed separately via Homebrew, run as subprocess) |
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
| **4a** | echo cancellation via livekit AEC + talking over replies without wake word | ✓ done (live-tested 2026-10-02 (MacBook Pro speakers and mic, one room)) |
| **4b** | Kokoro voice | ✓ done (live-tested 2026-10-02) |
| **6** | Jarvis speaks first: spoken "Claude finished" / "needs your permission" via Stop and PermissionRequest hooks | ✓ built (live test pending) |
| **5** | opt-in `--offline` brain (Ollama `gpt-oss:20b`); entire loop works with Wi-Fi off | planned |

For the full research and component justification: see [`docs/plan.md`](docs/plan.md).

## Testing

Unit tests run with `uv run python -m unittest discover -s tests`. They use fakes only: no microphone, no speakers, no real `claude`, never `~/.undertone`. Temp directories and stubs isolate each test.

Plus `bash scripts/verify-offline.sh` for the offline pipeline check: wake word → VAD → STT → TTS → Kokoro → self-screen → echo cancellation inside a `sandbox-exec` profile that denies all network access. Covers: network canary, clip format, wake word, VAD + STT, TTS exit/output, Kokoro telemetry off, Kokoro render, Kokoro self-screen, and echo cancellation (ERLE ≥ 20 dB, no speech-like run long enough to trigger talk-over). See "Verify it yourself" above.

## Environment variables

- `UNDERTONE_HOME` (default `~/.undertone`) — state directory for sockets and workspace
- `UNDERTONE_SESSION` (set by `undertone session`) — session token passed to the channel via MCP config and to hooks; internal use only
- `UNDERTONE_VOICE` — voice name for `say` (e.g., `"Daniel"`); empty string uses system voice
- `UNDERTONE_KOKORO_OWN_VOICE` — Kokoro voice for undertone's own words, the confirm read-back (default `af_heart`); keep it different from `UNDERTONE_KOKORO_VOICE`
- `UNDERTONE_KOKORO_VOICE` — voice name for Kokoro (e.g., `am_michael`, `bm_george`; default `am_adam`)
- `UNDERTONE_OWN_VOICE` — `say` voice for undertone's own words (default `Samantha`); keep it different from `UNDERTONE_VOICE`
- `UNDERTONE_SELF_WAKE_GUARD_S` — window (seconds) after undertone stops speaking during which a wake word is treated as barge-in, not exact wake (default 1.0 s, floor 1.0, cap 10.0; for AirPlay latency, try 3)
- `UNDERTONE_ESPEAK_CMD` (tests only) — path to espeak-ng or stub
- `UNDERTONE_TEST_REAL_KOKORO` (tests only) — set to `1` to run real Kokoro smoke tests
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

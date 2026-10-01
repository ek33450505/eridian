# Architecture

## Pipeline Diagram

```
Path A (undertone attach + undertone session):
  mic → wake ("hey jarvis") → VAD (0.7 s silence) → STT → voice.sock → channel (mcp_channel.py) → Claude Code session
                                                                                       │ reply tool
  speaker ← say ← speech queue (player.py) ← sentence chunker ← voice.sock ← channel ◄─┘

Path B (undertone chat):
  mic → wake ("hey jarvis") → VAD (0.7 s silence) → STT → brain (persistent claude -p)
  speaker ← say ← speech queue (player.py) ← sentence chunker ◄─ streamed text ─┘
```

The wake detector stays live while Claude is thinking or speaking. Saying "hey jarvis" again stops the speech and interrupts the turn (barge-in).

### Brain Adapters

There are two brains:

- **Brain A (Claude Code sessions):** A Channel MCP server (`mcp_channel.py`) receives transcripts from `undertone attach` (via `~/.undertone/voice.sock`) and injects them as notifications into a running session. Claude calls the `reply` tool to speak answers.
- **Brain B (`undertone chat`):** One persistent `claude -p` stream-json child, reading replies sentence by sentence.

## Modules

| Module | Role | Key Details |
|---|---|---|
| `audio.py` | sounddevice RawInputStream, 320-byte (10 ms) int16 frames | Lazy import; not loaded by light commands like `undertone channel` |
| `listener.py` | Pure state machine: IDLE→LISTENING→IDLE over frame stream | VAD in 1024-byte (32 ms) chunks; 0.7 s silence → end of turn; 4 s no-speech timeout; 15 s max utterance; 0.15 s VAD ignore window after wake. Strips leading wake phrase from transcripts. Wake detector is re-created after every turn (Phase 0 found a stale one could double-trigger). |
| `stt.py` | ParakeetSTT: warm-up on load (first call ~823 ms → 54 ms after warm-up, Phase 2a); HF_HUB_OFFLINE forced at import | Model: mlx-community/parakeet-tdt-0.6b-v3. Float32 in-memory, never written to disk. |
| `brain.py` | ClaudeBrain: one persistent child; reader thread → queue; `ask()` yields TextDelta/ToolUse/TurnEnd; `interrupt()` writes control_request JSON (~10 ms response) | 120 s turn timeout; stderr → 0600 log; atomic 0600 session file. Process startup cost eliminated (one-shot `claude -p` is 5.5–13 s; persistent child gives ~1.0–2.0 s to first token, measured on M4 Max). |
| `chunker.py` | SentenceChunker: stream of text deltas → sentences | `.!?` + whitespace, or newline; skips decimals and abbreviations (e.g., "e.g.", "etc."); fenced code → "Code's on screen." once per turn; 240-char soft cap. |
| `player.py` | SpeechQueue: sequential `say` per sentence; `stop()` for barge-in with generation counter | Closes race between dequeue and spawn via generation counter. Worker thread owned by SpeechQueue. |
| `chat.py` | ChatSession: main thread owns mic and Listener; ONE brain worker thread runs turns sequentially | An interrupted turn drains to its end before the next message is written. Generation counter bumped on every utterance and barge-in discards stale output. After a completed reply, worker waits for player idle, then posts to control queue; main thread drains → follow-up window. |
| `speaker.py` | build_say_argv: constructs `say` argv with `--` to guard against flag injection | Single point where `say` text is prepared; speaks replies in Path A (via reply tool) and Path B (via player queue). Runs `spoken.sanitize_for_tts()` to filter wake word. |
| `spoken.py` | Reply sanitization for TTS: strips invisible characters, removes `[`/`]`, replaces wake word forms with "the wake word" | Used by speaker.build_say_argv. |
| `voice_guard.py` | SessionStart, UserPromptSubmit, Stop, SubagentStart, SubagentStop and PreToolUse hooks that gate tool calls during voice turns. Covers arming, voice/typed provenance, voice-started subagents, the Bash allowlist and the confirm-phrase grant. | Stdlib only. Injected via `--settings` and runs as `python -P -m undertone.voice_guard <event> --home <UNDERTONE_HOME> --session <token>`. Events: `sessionstart`, `userpromptsubmit`, `stop`, `subagentstart`, `subagentstop`, `pretooluse`. Session state at `~/.undertone/sessions/<token>.json` (0600, atomic). Log at `~/.undertone/logs/guard.log` (0600, decisions only). Fails closed: `pretooluse` errors exit 2 (plus `|| exit 2` on the command), and the channel refuses voice turns unless SessionStart armed this launch's state. |
| `mcp_channel.py` | Stdlib MCP stdio **channel** server. Claude Code spawns it from the inline `--mcp-config` that `undertone session` passes (`python -P -m undertone.cli channel`). | Requires an armed session token (UNDERTONE_SESSION); refuses voice turns if SessionStart hook hasn't armed state. Connects as a client to `~/.undertone/voice.sock` and says `hello`. Each `utterance` becomes a `notifications/claude/channel` event. Exposes the `reply` tool (`kind` = ack/answer/confirm, ≤4000 chars) and forwards replies to `attach`. Registers confirm phrases (one-shot, 300 s challenge on each request). Rejects the 2026-07-28 `server/discover` probe so Claude Code falls back to `initialize` 2025-11-25. |
| `voice.py` | `VoiceBridge`, the `undertone attach` process. It runs the mic loop (Listener on the main thread) and the unix-socket server at `~/.undertone/voice.sock` (0600). | Sends each utterance to the attached channel and speaks replies sentence by sentence through `SpeechQueue`. Wake during speech = barge-in (stop speech). The follow-up window opens only after an `answer`. Socket rules: 64 KB lines, 16 connections, latest session wins. |
| `paths.py` | UNDERTONE_HOME layout (0700) | Default `~/.undertone`. Includes the voice.sock path and logs directory. (`voice_guard` keeps its state under `sessions/`.) |
| `cli.py` | Commands: `listen`, `chat`, `session`, `attach`, `channel` (internal), `models fetch`. `chat_workdir()` picks the working directory. | `session` generates a token (`secrets.token_hex(16)`) and injects hooks via `--settings` inline JSON; refuses `--settings`, `--bare`, `--safe-mode`. Resolves the cwd (symlinks followed). If it is home or an ancestor of home, any dot-directory under home, `~/Library`, or exactly `~/Documents`/`~/Desktop`/`~/Downloads`, it switches to `~/.undertone/workspace` and refuses a symlinked workspace. |

## State Machine

```
IDLE ── wake word ──► LISTENING
LISTENING
  ├─ no speech within 4 s           → IDLE
  ├─ 0.7 s silence after speech      → STT → utterance → THINKING
  └─ 15 s max utterance              → STT → utterance → THINKING
THINKING (brain turn) → SPEAKING (sentences queued and played)
  └─ reply finished playing          → FOLLOW-UP: LISTENING without the wake word, 6 s
                                        (no speech → IDLE)
THINKING / SPEAKING ── wake word ──► barge-in: stop speech, interrupt turn → LISTENING
```

`listener.py:Listener` covers IDLE and LISTENING, with timings from `ListenerConfig`. `chat.py:ChatSession` adds THINKING, SPEAKING, barge-in and the follow-up window (`Listener.listen_now()`).

## Lock Order

Session lock → player lock. The player's `on_start` callback is invoked without the lock held (avoiding deadlock).

## Latency Budget (M4 Max, measured 2026-10-01)

| Stage | Measured | Notes |
|---|---|---|
| End-of-turn detection | 0.7 s | Silence threshold |
| STT (real voice) | ~87 ms median (68–145 ms) | The first call took 823 ms before warm-up was added; it takes 54 ms after warm-up |
| Claude first token (warm) | 1.0–2.0 s | Persistent child; one-shot `claude -p` is 5.5–13 s |
| `say` first audio | 0.73 s | macOS TTS latency |
| Live end-to-end (speech end → first TTS) | **3.15 s** | First token at 1.95 s |

Target: ≤ 4 s from speech end to reply start.

## Testing Seams

- **Wake word factory:** injected callable; the CLI uses `cli._make_wake_factory()`, which wraps pymicro-wakeword
- **VAD:** injected callable; the CLI uses pysilero-vad's `SileroVoiceActivityDetector`
- **STT:** injected callable; the CLI uses `ParakeetSTT`
- **Clock:** injected callable; the CLI uses `time.monotonic`, and tests use a fake clock they advance by hand
- **Fake Claude:** `tests/fake_claude.py` is a protocol-faithful CLI that does not require Claude Code login
- **Say override:** `UNDERTONE_SAY_CMD` stubs `say` with a test binary
- **Undertone home:** `UNDERTONE_HOME` temp dir for each test (socket, logs, cache)
- **Session isolation:** Each test runs in its own `UNDERTONE_HOME` and fake Claude session

Tests run with `uv run python -m unittest discover -s tests`. No microphone, no speakers, no real `claude`, never touches `~/.undertone`.

## Phase 4 Note: Echo Cancellation for Barge-In

Today, barge-in requires the wake word. Talking over a reply without the wake word is planned for Phase 4 (Kokoro voice + livekit echo cancellation).

The challenge is telling the user's speech apart from undertone's own playback when both reach the microphone. Echo cancellation needs the playback signal as a reference, so Phase 4 will render TTS audio and play it from undertone's own process. That requires:
- A resident TTS model whose audio undertone plays itself (Kokoro or similar)
- An echo canceller fed the playback signal as reference (livekit `AudioProcessingModule`)
- Barge-in detection on the echo-cancelled mic signal

Today `say` plays its audio directly, so undertone never sees the playback signal and has nothing to use as an echo reference.

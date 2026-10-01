# Architecture

## Pipeline Diagram

```
mic → wake ("hey jarvis") → VAD (0.7 s silence) → STT → brain (persistent claude -p)
                                                          │ streamed text
                                                          ▼
speaker ← say ← speech queue (player.py) ← sentence chunker ─┘
```

The wake detector stays live while Claude is thinking or speaking. Saying "hey jarvis" again stops the speech and interrupts the turn (barge-in).

### Brain Adapters

There are two brains:

- **Brain A (Claude Code sessions):** the Stop hook sends a short spoken summary of each reply to the speaker daemon via `~/.undertone/speaker.sock`. A Channel MCP server (Phase 3, planned) will feed transcripts from undertone's mic pipeline into a running session.
- **Brain B (`undertone chat`):** One persistent `claude -p` stream-json child, reading replies sentence by sentence.

## Modules

| Module | Role | Key Details |
|---|---|---|
| `audio.py` | sounddevice RawInputStream, 320-byte (10 ms) int16 frames | Lazy import; never used by light commands like `undertone hook stop` |
| `listener.py` | Pure state machine: IDLE→LISTENING→IDLE over frame stream | VAD in 1024-byte (32 ms) chunks; 0.7 s silence → end of turn; 4 s no-speech timeout; 15 s max utterance; 0.15 s VAD ignore window after wake. Strips leading wake phrase from transcripts. Wake detector is re-created after every turn (Phase 0 found a stale one could double-trigger). |
| `stt.py` | ParakeetSTT: warm-up on load (first call ~823 ms → 54 ms after warm-up, Phase 2a); HF_HUB_OFFLINE forced at import | Model: mlx-community/parakeet-tdt-0.6b-v3. Float32 in-memory, never written to disk. |
| `brain.py` | ClaudeBrain: one persistent child; reader thread → queue; `ask()` yields TextDelta/ToolUse/TurnEnd; `interrupt()` writes control_request JSON (~10 ms response) | 120 s turn timeout; stderr → 0600 log; atomic 0600 session file. Process startup cost eliminated (one-shot `claude -p` is 5.5–13 s; persistent child gives ~1.0–2.0 s to first token, measured on M4 Max). |
| `chunker.py` | SentenceChunker: stream of text deltas → sentences | `.!?` + whitespace, or newline; skips decimals and abbreviations (e.g., "e.g.", "etc."); fenced code → "Code's on screen." once per turn; 240-char soft cap. |
| `player.py` | SpeechQueue: sequential `say` per sentence; `stop()` for barge-in with generation counter | Closes race between dequeue and spawn via generation counter. Worker thread owned by SpeechQueue. |
| `chat.py` | ChatSession: main thread owns mic and Listener; ONE brain worker thread runs turns sequentially | An interrupted turn drains to its end before the next message is written. Generation counter bumped on every utterance and barge-in discards stale output. After a completed reply, worker waits for player idle, then posts to control queue; main thread drains → follow-up window. |
| `speaker.py` | Path A daemon: unix socket `~/.undertone/speaker.sock` (0600), newline JSON ops (say/stop/ping), 1 s deadline per connection, 64 KB cap | build_say_argv is the ONE place `say` argv is built (with `--` to guard against flag injection). |
| `spoken.py` | Reply → short speakable summary (SPOKEN: line or first 2 sentences) | Markdown stripped, code/URLs skipped. |
| `hooks.py` | Stop hook (speaks a summary of the reply) and UserPromptSubmit hook (asks Claude to end replies with a `SPOKEN:` line) | Opted-in sessions only; never speaks on `stop_hook_active` turns. |
| `paths.py` | UNDERTONE_HOME layout (0700) | Default `~/.undertone`. |
| `cli.py` | Commands: `chat`, `listen`, `serve`, `say`, `stop`, `on`, `off`, `status`, `hook`, `hooks-snippet`, `models fetch`. `chat_workdir()` picks the working directory. | Resolves the cwd (symlinks followed). If it is home or an ancestor of home, any dot-directory under home, `~/Library`, or exactly `~/Documents`/`~/Desktop`/`~/Downloads`, it switches to `~/.undertone/workspace` and refuses a symlinked workspace. |

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

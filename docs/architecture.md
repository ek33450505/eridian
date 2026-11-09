# Architecture

## Pipeline Diagram

```
Path A (eridian attach + eridian session):
  mic ──┬─→ AEC ─→ wake ("hey jarvis") → VAD (0.7 s silence) → STT → voice.sock → channel (mcp_channel.py) → Claude Code session
        │                                                                                             │ reply tool
  duplex engine ◄──────────────────────────────────────────────────────────────────────────────────┘
        │     speech queue (RenderedSpeechQueue) ← sentence chunker ← voice.sock ← channel
        └──→ speaker (duplex sink)

Path B (eridian chat):
  mic ──┬─→ AEC ─→ wake ("hey jarvis") → VAD (0.7 s silence) → STT → brain (persistent claude -p)
        │
  duplex engine ◄───────────────────────────────────────────────────────┐
        │     speech queue ← sentence chunker ◄─ streamed text ──────────┘
        └──→ speaker (duplex sink)

Note: AEC (WebRTC echo cancellation) uses the speaker signal as reference.
The wake detector and VAD stay live while Claude is thinking or speaking.
Saying "hey jarvis" again stops the speech and interrupts the turn (barge-in).
Talk-over: sustained speech during playback without wake word opens a turn (gated by voice guard).
```

### Brain Adapters

There are two brains:

- **Brain A (Claude Code sessions):** A Channel MCP server (`mcp_channel.py`) receives transcripts from `eridian attach` (via `~/.eridian/voice.sock`) and injects them as notifications into a running session. Claude calls the `reply` tool to speak answers.
- **Brain B (`eridian chat`):** One persistent `claude -p` stream-json child, reading replies sentence by sentence.

## Modules

| Module | Role | Key Details |
|---|---|---|
| `audio.py` | sounddevice RawInputStream, 320-byte (10 ms) int16 frames | Lazy import; not loaded by light commands like `eridian channel` |
| `listener.py` | Pure state machine: IDLE→LISTENING→IDLE over frame stream | VAD in 1024-byte (32 ms) chunks; 0.7 s silence → end of turn; 4 s no-speech timeout; 15 s max utterance; 0.15 s VAD ignore window after wake. Strips leading wake phrase from transcripts. Wake detector is re-created after every turn (an early test found a stale one could double-trigger). |
| `stt.py` | ParakeetSTT: warm-up on load (first call ~823 ms → 54 ms after warm-up); HF_HUB_OFFLINE defaults to 1 at import (your own setting wins) | Model: mlx-community/parakeet-tdt-0.6b-v3. Float32 in-memory, never written to disk. |
| `brain.py` | ClaudeBrain: one persistent child; reader thread → queue; `ask()` yields TextDelta/ToolUse/TurnEnd; `interrupt()` writes control_request JSON (~10 ms response) | 120 s turn timeout; stderr → 0600 log; atomic 0600 session file. Process startup cost eliminated (one-shot `claude -p` is 5.5–13 s; persistent child gives ~1.0–2.0 s to first token, measured on M4 Max). |
| `chunker.py` | SentenceChunker: stream of text deltas → sentences | `.!?` + whitespace, or newline; skips decimals and abbreviations (e.g., "e.g.", "etc."); fenced code → "Code's on screen." once per turn; 240-char soft cap. |
| `player.py` | SpeechQueue: sequential `say` per sentence; `stop()` for barge-in with generation counter. RenderedSpeechQueue: renders TTS via `say -o` or `KokoroRenderer`, validates 1 channel / 16-bit / 16 kHz, unlinks after reading. Timeout: RENDER_TIMEOUT_S = 20 s. `SayRenderer` and `KokoroRenderer` are interchangeable renderers. | Closes race between dequeue and spawn via generation counter. Worker thread owned by SpeechQueue. Render files in mode-0700 temp directory, deleted in finally. In-memory buffers ~32 KB/s. |
| `duplex.py` | DuplexAudio: full-duplex sounddevice RawStream (16 kHz, mono, int16, 10 ms blocks). Audio callback writes to speaker, queues (mic, exactly-what-was-played). Main thread runs WebRTC AudioProcessingModule (AEC3) with echo_cancellation, noise_suppression, high_pass_filter. Delay hint: the stream's input+output latency, or 40 ms. Implements PlaybackSink. | Lazy import for light commands. Stream delay set via set_stream_delay_ms. Cleaned frames to listener. Tests inject stream_factory and apm_factory. |
| `chat.py` | ChatSession: main thread owns mic and Listener; ONE brain worker thread runs turns sequentially | An interrupted turn drains to its end before the next message is written. Generation counter bumped on every utterance and barge-in discards stale output. After a completed reply, worker waits for player idle, then posts `reply_done` with the turn's origin; main thread opens the follow-up window only for the first reply to a wake-word turn. |
| `speaker.py` | prepare_speech: single scrub point (NUL strip, lone-surrogate strip, NFKC, `spoken.sanitize_for_tts()`, MAX_SPEECH_CHARS = 1000 cap) → exits to build_say_argv and espeak_phonemes. build_say_argv: constructs `say` argv with `--` to guard against flag injection. espeak_phonemes: text → espeak-ng via subprocess with phoneme IPA output for Kokoro. | Single point where TTS text is prepared. Runs `spoken.sanitize_for_tts()` to filter wake word and brackets. |
| `spoken.py` | Reply sanitization for TTS: strips invisible characters, removes `[`/`]`, replaces wake word forms with "the wake word". Also strip_echo: removes talked-over reply words. | Used by speaker.prepare_speech. strip_echo applied in phase 4b. |
| `kokoro.py` | KokoroVoice: resident Kokoro-82M via onnxruntime, phoneme string → 16 kHz mono int16 PCM. Pieces capped at 32 (first) and 200 (later) phoneme tokens, max 510. Leading ~0.4 s silence trimmed. Non-finite output dropped. | ORT_DISABLE_TELEMETRY forced; HF_HUB_OFFLINE defaults to 1 at import. Model: onnx-community/Kokoro-82M-v1.0-ONNX, fp32 `onnx/model.onnx`, pinned revision. Reply voice: default `am_adam`; ERIDIAN_KOKORO_VOICE env overrides. Own voice: default `af_heart`; ERIDIAN_KOKORO_OWN_VOICE env overrides. |
| `screen.py` | ScreenedSink: wraps a PlaybackSink; runs wake detector on last HISTORY_S of audio, modeled silence gap, current chunk, LOOKAHEAD_S silence. Drops chunks if detector fires (SCREEN_CUTOFF = 0.8; the live detector fires at 0.97); fails closed on error; keeps history across clear(). | HISTORY_S = 2.0 s; LOOKAHEAD_S = 1.0 s. Calibrated: 0/200 ordinary sentences blocked, 66/80 homophone attacks caught. Detector fires 50–250 ms after phrase ends. |
| `voice_guard.py` | SessionStart, UserPromptSubmit, Stop, SubagentStart, SubagentStop and PreToolUse hooks that gate tool calls during voice turns. Covers arming, voice/typed provenance, voice-started subagents, the Bash allowlist, the confirm-phrase grant, and read-only MCP tier (`READ_ONLY_MCP_TOOLS`). Blocked calls recorded in state for `read_back()` summary on confirm replies. Optional safe phrase: an scrypt hash in `<home>/safe_phrase.json`, matched in `record_voice`, redacted by `redact_safe_phrase()`. On a voice turn, any read or search of eridian's home or a folder above it is denied even after a confirm (`HOME_DENY_REASON`). | Stdlib only. Injected via `--settings` and runs as `python -P -m eridian.voice_guard <event> --home <ERIDIAN_HOME> --session <token>`. Events: `sessionstart`, `userpromptsubmit`, `stop`, `subagentstart`, `subagentstop`, `pretooluse`. Session state at `~/.eridian/sessions/<token>.json` (0600, atomic) holds gating state, grants, subagent records, and blocked-call descriptions for the read-back (≤160 chars each, no argument values; last 3 kept plus an eviction count, ≤300 s old). Log at `~/.eridian/logs/guard.log` (0600, decisions only). Fails closed: `pretooluse` errors exit 2 (plus `|| exit 2` on the command), and the channel refuses voice turns unless SessionStart armed this launch's state. |
| `mcp_channel.py` | Stdlib MCP stdio **channel** server. Claude Code spawns it from the inline `--mcp-config` that `eridian session` passes (`python -P -m eridian.cli channel`). | Requires an armed session token (ERIDIAN_SESSION); refuses voice turns if SessionStart hook hasn't armed state. Connects as a client to `~/.eridian/voice.sock` and says `hello` (now includes `"session"` token). Each `utterance` becomes a `notifications/claude/channel` event. Exposes the `reply` tool (`kind` = ack/answer/confirm, ≤4000 chars) and forwards replies to `attach`. On a confirm reply, attaches `voice_guard.read_back()` as a `check` field. Registers confirm phrases (one-shot, 300 s challenge on each request). Rejects the 2026-07-28 `server/discover` probe so Claude Code falls back to `initialize` 2025-11-25. |
| `voice.py` | `VoiceBridge`, the `eridian attach` process. It runs the mic loop (Listener on the main thread) and the unix-socket server at `~/.eridian/voice.sock` (0600). | Sends each utterance to the attached channel and speaks replies sentence by sentence through `RenderedSpeechQueue` (or `SpeechQueue` with `--no-aec`). Wake during speech = barge-in (stop speech); talk-over does the same during ack/answer speech, never during a confirm prompt. Tags each utterance with `via`. The follow-up window opens only after the first `answer` to a wake-word turn. On a confirm reply, speaks the `check` read-back (≤600 chars) before Claude's words in eridian's own voice. While a confirm is pending (up to 300 s), a too-early wake turn (`wake_barge_in`) is held, not sent, and `TOO_SOON_SPEECH` is spoken. Transcript prints show the safe phrase as `[safe phrase]`. Socket rules: 64 KB lines, 16 connections (`MAX_CONNECTIONS`); multi-attach routing, see "Multi-attach routing and the control socket" below. |
| `control.py` | Client for the control socket: `eridian voice`, `mute` and `unmute` commands use it. | Refuses a socket that isn't a socket or isn't owned by the caller; checks the server's uid (`LOCAL_PEERCRED` / `SO_PEERCRED`); total deadline per request; responses capped at `MAX_RESPONSE`. |
| `persona.py` | Opt-in speech persona: `ERIDIAN_PERSONA` (`rocky`, or unset/`off` for none) adds a style block to the channel's instructions. Also the typed-chat SessionStart hook, `python -P -m eridian.persona sessionstart --under DIR`. | A bad value fails at startup. The hook prints its context only when the session's cwd is inside `DIR`, and always exits 0 once its arguments parse. |
| `announce.py` | Async Stop / StopFailure / PermissionRequest hook for `eridian session`: maps the payload to an announce event and sends one `announce` line to `voice.sock`. | Stdlib only, lazy `voice_guard` import; runs as `python -P -m eridian.announce <hook> --home <ERIDIAN_HOME> --session <token>`. Always exits 0 with no output; forwards no payload text; requires an absolute home and a socket owned by the same uid; 0.5 s connect timeout. |
| `paths.py` | ERIDIAN_HOME layout (0700) | Default `~/.eridian`. Includes the voice.sock path and logs directory. (`voice_guard` keeps its state under `sessions/`.) |
| `cli.py` | Commands: `listen`, `chat`, `session`, `launch`, `attach`, `stop`, `channel` (internal), `models fetch`, `shell-init zsh`, `safe-phrase`, `migrate`, and the voice controls `voice status|on|off`, `mute`, `unmute` (via `control.py`). `chat_workdir()` picks the working directory. | `session` generates a token (`secrets.token_hex(16)`) and injects hooks via `--settings` inline JSON; refuses `--settings`, `--bare`, `--safe-mode`. Resolves the cwd (symlinks followed). If it is home or an ancestor of home, any dot-directory under home, `~/Library`, or exactly `~/Documents`/`~/Desktop`/`~/Downloads`, it switches to `~/.eridian/workspace` and refuses a symlinked workspace. |

## State Machine

```
IDLE ── wake word ──► LISTENING
LISTENING
  ├─ no speech within 4 s                           → IDLE
  ├─ 0.7 s silence after speech                      → STT → utterance → THINKING
  └─ 15 s max utterance                              → STT → utterance → THINKING

THINKING (brain turn) → SPEAKING (sentences queued and played)
  ├─ wake word during speech                         → barge-in: stop speech, interrupt turn → LISTENING
  ├─ talk-over, mode "turn" (ack/answer; all chat)  → barge-in → LISTENING (talk_over turn, 0.5 s pre-roll)
  ├─ (confirm/unknown kind: talk-over is off, so the prompt plays to the end; answering needs the wake word)
  └─ wake-turn answer finished playing               → FOLLOW-UP: LISTENING without the wake word, 6 s
                                                        (no speech → IDLE)
```

- `Listener.talk_over`: set by owner before each frame: `"off"` (no VAD while IDLE), `"stop"` (emit event, stay IDLE), `"turn"` (open turn).
- Talk-over turn pre-seeded with `talk_over_preroll_s=0.5` s of audio; sustained speech is 10 consecutive 32 ms chunks at ≥ `talk_over_threshold=0.6`.
- Wake word always checked first.
- `listener.py:Listener` covers IDLE and LISTENING. `chat.py:ChatSession` and `voice.py:VoiceBridge` add THINKING, SPEAKING, barge-in, follow-up, and talk-over modes (via `Listener.talk_over`).

## Lock Order

`VoiceBridge` session lock → player lock (`RenderedSpeechQueue`'s condition) → the duplex engine's buffer lock. On the main thread, `VoiceBridge._talk_over_mode()` takes the session lock and then reads `player.busy`, and the result is assigned to `Listener.talk_over` before the frame is processed. The player calls `sink.play` under its own condition; the sink never calls back into the player. The audio callback only takes the buffer lock, briefly, and never waits. The player's `on_start` callback runs with no lock held. No lock is held across `player.stop()`.

## Latency Budget (M4 Max, measured 2026-10-01)

| Stage | Measured | Notes |
|---|---|---|
| End-of-turn detection | 0.7 s | Silence threshold |
| STT (real voice) | ~87 ms median (68–145 ms) | The first call took 823 ms before warm-up was added; it takes 54 ms after warm-up |
| Claude first token (warm) | 1.0–2.0 s | Persistent child; one-shot `claude -p` is 5.5–13 s |
| `say` first audio | 0.54–0.56 s | `say -o` with Evan (Enhanced), 2026-10-02 (earlier: 0.73 s with Zoe Premium) |
| Kokoro first audio | 0.12–0.27 s | onnxruntime + espeak-ng, idle machine, 2026-10-02 |
| Live end-to-end with Kokoro (speech end → first audio) | not yet measured | Live test passed 2026-10-02; no timing was captured |

Target: ≤ 4 s from speech end to reply start.

## Testing Seams

- **Wake word factory:** injected callable; the CLI uses `cli._make_wake_factory()`, which wraps pymicro-wakeword
- **VAD:** injected callable; the CLI uses pysilero-vad's `SileroVoiceActivityDetector`
- **STT:** injected callable; the CLI uses `ParakeetSTT`
- **Clock:** injected callable; the CLI uses `time.monotonic`, and tests use a fake clock they advance by hand
- **Fake Claude:** `tests/fake_claude.py` is a protocol-faithful CLI that does not require Claude Code login
- **Say override:** `ERIDIAN_SAY_CMD` stubs `say` with a test binary
- **DuplexAudio:** `stream_factory` and `apm_factory` injected for fakes; the CLI uses the real sounddevice and livekit
- **RenderedSpeechQueue:** `sink` injected (PlaybackSink) for testing without audio output
- **Eridian home:** `ERIDIAN_HOME` temp dir for each test (socket, logs, cache, render temp files)
- **Session isolation:** Each test runs in its own `ERIDIAN_HOME` and fake Claude session

Tests run with `uv run python -m unittest discover -s tests`. No microphone, no speakers, no real `claude`, never touches `~/.eridian`.

## Echo Cancellation for Talk-Over (2026-10-01)

**Talk-over:** Users can now interrupt an eridian reply by speaking, without saying the wake word. `Listener.talk_over`, set by the owner before each frame, controls the behavior: `"off"` (default; no VAD while idle), `"stop"` (emit `talk_over_stop`, stay IDLE), or `"turn"` (open a turn pre-seeded with 0.5 s of audio).

**Echo Cancellation:** Eridian now renders TTS itself, capturing the playback signal to feed WebRTC's `AudioProcessingModule` (AEC3) as a reference. The audio callback runs in the sounddevice RawStream; the main thread runs the echo canceller on each frame.

### Rendered TTS
- `player.RenderedSpeechQueue` replaces `SpeechQueue` when AEC is on (default; `--no-aec` to disable).
- It renders each sentence with `say -o` (the baseline) or `KokoroRenderer` (the default when the Kokoro voice loads; otherwise `say`) into a private `tempfile.mkdtemp(prefix="eridian-tts-")` directory (0700) as `<counter>.wav`, validates (1 channel, 16-bit, 16 kHz), plays via the duplex sink, and unlinks in a `finally`.
  - **`say`:** argv from `speaker.build_say_argv(text, out_path=...)`, latency 0.54–0.56 s.
  - **Kokoro:** text → `speaker.espeak_phonemes()` (runs espeak-ng subprocess) → `kokoro.KokoroVoice.pieces()` (cap 32/200/510 tokens) → `onnxruntime` → 16 kHz PCM in memory (24 kHz resampled by livekit); first audio 0.12–0.27 s.
- `RENDER_TIMEOUT_S = 20.0`: a hung render is killed and skipped.
- Each reply line (64 KB text max) buffers roughly 10 MB of audio in memory at most, about 32 KB per second of speech.

### Duplex Engine
- `duplex.DuplexAudio` runs one full-duplex sounddevice `RawStream`: 16 kHz, mono, int16, 10 ms blocks (160 samples).
- The audio callback: (1) copies up to one block from the playback buffer (zero-padded), (2) writes it to the speaker, (3) queues (mic frame, exactly-what-was-played).
- The main thread runs `AudioProcessingModule(echo_cancellation=True, noise_suppression=True, high_pass_filter=True)` on each frame:
  - `set_stream_delay_ms`: the stream's input+output latency, or 40 ms
  - `process_reverse_stream`: the speaker signal
  - `process_stream`: the mic signal, edited in-place
- Cleaned frames go to the listener (wake word and VAD).
- The engine is also the `PlaybackSink` (`play`, `clear`, `playing`, `wait_drained`).
- Lazy imports keep light commands free of sounddevice and livekit.

### Talk-Over Detection and Control
- Sustained speech: 10 consecutive 32 ms VAD chunks at ≥ 0.6 (`ListenerConfig.talk_over_threshold=0.6`, `talk_over_min_s=0.3`).
- In `"turn"` mode, opens a `talk_over` turn pre-seeded with `talk_over_preroll_s=0.5` s of audio.
- In `"stop"` mode, emits `talk_over_stop` and stays IDLE, keeping the wake detector running. `VoiceBridge` no longer uses it: during confirm or unknown-kind speech it sets `"off"`, so a confirm prompt is always heard in full.
- The wake word is always checked first.
- With mode `"off"` (default, and whenever the player is idle), no VAD runs while IDLE.

### Hard Control: Voice Provenance
Every utterance now carries `"via": "wake"|"follow_up"|"talk_over"` on the voice socket:
- `mcp_channel` passes `can_grant = (via == "wake")` to the voice guard.
- `voice_guard.record_voice()` consumes the pending challenge (one attempt), but only `can_grant=True` plus the phrase grants.
- **Before this fix:** a talk-over turn could capture eridian's own spoken "confirm <w1> <w2>" through imperfect AEC and grant it. A talk-over turn already listening when a confirm reply was spoken could capture it. A follow-up window could open after a confirm reply followed by an answer.
- **Verified end-to-end** (2026-10-01): via `talk_over`, `follow_up`, or missing → no grant, Write denied; via `wake` → granted.

### Echo Filter (Defence in Depth)
- `spoken.strip_echo(heard, spoken, min_run=2)` uses difflib to find runs of 2+ heard words that match the reply in order, and removes them; single shared words ("the") stay.
- The rest of a talk-over transcript is sent with `[removed my own words: "…" -> "…"]`; if nothing is left it is dropped with `[ignored as my own reply: "…"]`. The pre-roll and the detection delay overlap the reply, so a talk-over transcript often starts with residual echo.
- Not applied to wake or follow-up turns.

### Residual Risks
- Talk-over widens no-wake-word input: it was the 6 s follow-up window after an answer; now it is also the whole playback of every ack or answer.
- Ambient speech during playback can open a turn. That turn is gated by voice_guard and cannot confirm.
- If the wake detector ever fired on eridian's own speech, that turn would carry `via: "wake"`. The TTS scrub prevents the wake phrase from being spoken; that pre-existing control covers this.
- **Imperfect AEC in a real room:** live-tested once (2026-10-02, MacBook Pro speakers and mic); residual echo did reach STT during talk-over, which `strip_echo` handles. Other rooms, external speakers and high volumes are untested. `--no-aec` turns talk-over off.

### Measurement (offline simulation, M4 Max, 2026-10-01)
- **ERLE (Echo Return Loss Enhancement):** ≈ 35 dB
- **VAD on residual chunks:** 0/217 chunks (vs. 189/217 raw)
- **Processing time:** ~60 µs per 10 ms frame

## Multi-attach routing and the control socket (2026-11-06)

Live test: pending. This section describes the code, not a live-tested behaviour.

**Routing:** `voice.py` keeps every session that sent an accepted `hello` attached; one without a valid token is listed but never gets voice. Wake-word turns go to the most recently attached session with voice on (the target). Replies and announcements from a session that is off, or on but not the target, are not spoken. A `{"type":"detached"}` line goes only to an old connection whose session token matches a newer `hello` (a reconnect), and that connection is then closed.

**Voice state:** per-session OFF flags and mute are kept in `<home>/voice-state.json` (version 2; version 1 loads as unmuted). Keys are SHA-256 of the session token. The file is 0600 and written atomically, with at most `MAX_OFF_ENTRIES` entries. Unreadable or corrupt state makes every session off and starts muted.

**Control socket:** `<home>/control.sock` (`paths.control_socket_path()`, 0600) carries newline-delimited JSON, one response per request: `status`, `voice_on`, `voice_off`, `mute`, `unmute`. It can't create utterances, open turns or grant confirms. Its accept loop serves up to `CONTROL_MAX_CONNECTIONS` connections plus one reserved connection (only `mute`, `voice_off`, `status`). After repeated consecutive accept failures (`ACCEPT_MAX_ERRORS`), attach mutes and removes the socket.

**Requester check:** `voice_on` and `unmute` are refused when the peer pid (read at accept) descends from Claude Code. The ancestry walk uses `/bin/ps` and fails closed. See SECURITY.md §6a for the mitigations and residual risks.

## Voice-safety tier for app tools (2026-10-03)

**Path of a blocked call to the read-back:**
```
Claude attempts a gated tool ──► PreToolUse hook: voice_guard denies, then records a description (no argument values)
                                 ├─ state file: last 3 entries + eviction count (entry ≤ 160 chars, ≤ 300 s old)
Claude replies with kind="confirm" ──► channel attaches voice_guard.read_back() as "check" field (≤ 600 chars)
                                        └─ attach speaks it in eridian's own voice, prints `eridian: …`
                                           Tool result tells Claude what was read back
```

**Read-only MCP tier:** five tools from an MCP server named `cast-record`, the maintainer's optional local server (not part of eridian), are listed in `voice_guard.READ_ONLY_MCP_TOOLS` (exact-name allowlist in code, not config) and run ungated on a voice turn. The names do nothing unless a server with that name is registered: `mcp__cast-record__cast_decisions`, `mcp__cast-record__cast_incidents`, `mcp__cast-record__cast_cost`, `mcp__cast-record__cast_sessions`, `mcp__cast-record__cast_ask`.

**Eridian's own voice:** A second TTS voice for words eridian writes itself, which Claude's replies never use:
- Kokoro: `af_heart` (env `ERIDIAN_KOKORO_OWN_VOICE`, constants `OWN_VOICE_ENV`, `DEFAULT_OWN_VOICE` in kokoro.py), name-checked and SHA-256 verified.
- `say`: `Samantha` (env `ERIDIAN_OWN_VOICE`).
- Used only for the read-back. The attach banner names both voices (e.g. `Kokoro (am_adam; eridian's own voice: af_heart)`), and attach refuses to start (exit 1) if the own voice equals the reply voice, or is one of the Kokoro blend components.

**Read-back vocabulary:** fixed words only. Names ≤2 words/20 chars or `a long name`. Location classes: `in this project`, `in the project git or Claude settings`, `in your home folder`, `in your home settings files`, `outside your home folder`, `at a relative path`, `somewhere unknown`. Hosts: last two labels, `a site under X`, `an international address`, or `an IP address`. Never spoken/stored: argument values, MCP keys/values, commit messages, free text. Omissions marked with `with N arguments`. Redirects: `overwriting` vs `appending to`. Complex shell: `a complex shell command starting with <prog>`. Wrappers name the program they run; up to 3 commands are named and the rest counted. URLs and scp/ssh remotes in shell arguments read `to <host>` / `to a remote computer <host>`. The read-back starts `eridian blocked:` and ends `, and N more blocked calls` when entries were evicted. Example: `the shell command rm rf on files in your home folder`.

**Grant unchanged:** a confirm still unlocks every gated call for the rest of the turn, so the read-back informs and does not bind. See SECURITY.md §7a for the residual risks.

**Channel instructions changed:** For destructive, irreversible or outward-facing actions requested by voice, Claude makes the tool call first; after the voice guard refuses it, Claude sends `kind="confirm"`.

## Announcements (2026-10-02)

`eridian attach` tells you when Claude finishes or needs you, with fixed spoken templates (`voice.ANNOUNCE_TEMPLATES`).

### Path of an announcement
```
Claude Code hook (Stop / StopFailure / PermissionRequest, async)
  └─ python -P -m eridian.announce <hook> --home H --session T
       └─ one-shot connection to H/voice.sock, no hello:
          {"type":"announce","session":T,"event":E}
            └─ attach reader thread: E enabled by --announce? T == attached session (hmac.compare_digest)?
                 └─ control queue ─► main thread: skip rules ─► SpeechQueue (behind current speech)
```
- **Hello.** The channel's `hello` now includes `"session"`, its `ERIDIAN_SESSION` token. A hello without a valid token still attaches, but no announcement can match it.
- **Hook mapping.** `stop` → `finished`, except while `background_tasks` lists a `subagent` whose status is `running` (its completion produces a later `Stop`). `stopfailure` → `error`. `permissionrequest` → `question` for `AskUserQuestion`, `plan` for `ExitPlanMode`, `permission` otherwise. `cli.voice_guard_settings` adds the `Stop` handler to the existing `Stop` entry, after the voice guard's, and new `StopFailure` and `PermissionRequest` entries.
- **Hook rules.** The hook always exits 0 and writes no output, because `PermissionRequest` stdout is a decision channel. It sends only the token and the event key, never payload text, and connects with a 0.5 s timeout. Cold start is ~0.03 s.
- **Main-thread rules.** For `finished`, the "answered" flag is read and cleared first. `finished` is skipped if a non-ack reply was spoken since the last `finished`, and while a confirm is pending (set by a confirm or unknown-kind reply, cleared only when the next utterance is sent). Any announcement is dropped while the listener isn't idle, and the same event isn't repeated within `ANNOUNCE_REPEAT_S` (3.0 s). Otherwise the generation is bumped, talk-over is off for it, and the template is queued behind current speech; it never stops the player. No announcement opens the follow-up window; only the first answer to a wake-word turn does. Both reply flags reset when a new session attaches.

### Spike findings (Claude Code 2.1.288, captured live)
- `Stop` fired 43–100 ms after the final message (7 samples). An Esc-interrupted turn fires no `Stop`. The payload also carries `background_tasks` and `session_crons`, which the hooks docs don't list.
- `Notification` never fired: not for a forced permission prompt, inside cmux or with cmux's hook injection off (`CMUX_CLAUDE_HOOKS_DISABLED=1`), and not after 250 s idle inside cmux. `PermissionRequest` fired on every prompt, so it is the trigger, and there is no idle announcement.
- Under cmux, `claude` is a shim that merges its own `--settings` (including its own `PermissionRequest` hook) with ours; `eridian session` goes through it.
- Projected `Stop` to first audio on an idle machine: about 0.4 s (≤ 0.1 s `Stop` lag + 0.03 s hook start + ~0.29 s Kokoro first audio). Not yet measured end to end.

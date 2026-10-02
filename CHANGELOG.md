# Changelog

All notable changes to undertone are documented here, in the style of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). No release has been tagged yet; `pyproject.toml` is at 0.1.0.

## [Unreleased]

### 2026-10-01 — Phase 4a: echo cancellation and talk-over

**Added**
- `DuplexAudio` (duplex.py): full-duplex sounddevice `RawStream` (16 kHz mono int16, 10 ms blocks). Pairs each mic frame with the speaker frame it just wrote. WebRTC AEC3 via livekit's `AudioProcessingModule(echo_cancellation=True, noise_suppression=True, high_pass_filter=True)`: the speaker frame goes to `process_reverse_stream`, then the mic frame to `process_stream`. The delay hint is the stream's input+output latency or 40 ms if unknown. Offline simulation (2026-10-01): ERLE ≈ 35 dB, Silero VAD fired on residual in 0/217 chunks (vs 189/217 without AEC), user voice still detected at max probability 0.9999, ~60 µs per 10 ms frame.
- **Live test (2026-10-02, real speech, MacBook Pro speakers and mic):** undertone did not talk itself into a turn while playing a full reply. Talk-over fired on the user's voice, and a wake-word "confirm lemon canyon" granted a delete (`guard.log`: `pretooluse granted Bash`). The first build lost the user's words (the whole talk-over turn was dropped as echo), let a talked-over confirm prompt cut off its phrase, and sent "A Jarvis." as a turn; all three were fixed and the re-test worked (reported by Ed; per-step output not captured).
- `RenderedSpeechQueue` (player.py): renders each sentence with `say -o <file> --file-format=WAVE --data-format=LEI16@16000` to a private temp dir (prefix `undertone-tts-`, mode 0700; file deleted after reading). Rendering time: 0.62 s for a 4.7 s sentence (measured 2026-10-01). Sentences render ahead so playback is gapless. `SpeechQueue` (direct `say`) remains for `--no-aec`.
- Talk-over (AEC on only): sustained speech stops playback without the wake word — 10 consecutive 32 ms VAD chunks at probability ≥ 0.6 (`ListenerConfig.talk_over_threshold=0.6`, `talk_over_min_s=0.3`). Your speech becomes your next turn; `talk_over_preroll_s=0.5` keeps your first words. Exception: talk-over is off during a confirm prompt (or a reply of unknown kind), so the phrase is always heard in full; the wake word still interrupts it, and you answer with the wake word plus the phrase. (The first version stopped the prompt, and in the 2026-10-02 live test that cut off the phrase.) In `chat`, talk-over also interrupts Claude's in-flight turn.
- Hard control (`via` field): every utterance carries `via` = `wake` | `follow_up` | `talk_over`. `voice_guard.record_voice(..., can_grant=False)` lets ONLY a wake-word turn (`via: "wake"`, exact match) satisfy a confirm challenge. Any voice turn still uses up the one attempt. Enforced end to end through the channel and PreToolUse hook. An old `undertone attach` paired with a new session sends no `via`, so confirmations fail closed.
- Echo filter (defence in depth): on a talk-over turn, words that repeat the reply being spoken (runs of 2+ words matching it in order, `spoken.strip_echo`) are removed and the rest is sent. It prints `[removed my own words: "…" -> "…"]`, or `[ignored as my own reply: "…"]` when nothing is left. Does not apply to wake or follow-up turns. The first version dropped the whole turn when echo dominated the transcript, which lost the user's words in the 2026-10-02 live test.
- CLI: `undertone attach` and `undertone chat` use echo cancellation by default. New flag `--no-aec` (both commands) plays speech with `say` directly: no echo cancellation, no talk-over, wake-word barge-in only. Startup banner: `Echo cancellation on: talk over a reply to stop it.` / `Echo cancellation off: say 'hey jarvis' to stop a reply.` If audio devices can't be opened: `undertone: cannot open the audio devices for echo cancellation: <error>. Try --no-aec.`
- `scripts/verify-offline.sh` gains an "echo cancellation" check (ERLE ≥ 20 dB, and no speech-like run on the residual long enough to trigger talk-over; 43.9 dB and a 3-chunk longest run on 2026-10-01).

**Changed**
- `speaker.build_say_argv(text, *, out_path=None)`, still the single sanitizing argv builder, gains a render-to-file mode for `RenderedSpeechQueue`; every flag stays before `--`.
- `undertone listen` is unchanged (raw mic).

**Security**
- Confirm challenges: only wake-word turns (`via: "wake"`) can satisfy them. Talk-over and follow-up turns cannot grant gated calls: such a turn could be undertone's own speech leaking through imperfect echo cancellation, or ambient audio. Found by the security review of talk-over (CHANGES_REQUESTED, fixed, then APPROVED).

**Dependency**
- livekit 1.1.20 (Apache-2.0; pulls in protobuf, types-protobuf, aiofiles). Only its local `AudioProcessingModule` is used, with no LiveKit server or network.

### 2026-10-01 — Voice guard follow-ups (from the live test)

- **Live-verified** with real speech on Claude Code 2.1.286. See SECURITY.md §7a for the results.
- **Voice-started subagents stay gated.**
  - New `SubagentStart` and `SubagentStop` hooks (events `subagentstart`, `subagentstop`).
  - Agents are recorded only at `SubagentStart`. A subagent started while a voice turn is active, or while any voice-born subagent is still recorded, is voice-born. That includes nested children.
  - Once a session has had a voice turn, an unknown or invalid `agent_id` counts as voice-born; only subagents recorded as typed-origin run ungated.
  - A typed prompt revokes grants that subagents inherited.
  - Without this, a background subagent started by a voice turn ran ungated once the user typed after the turn ended.
- **Plain shell commands during voice turns.** The channel instructions now tell Claude to run shell commands in their plainest form (no file redirects, no `$?`, no globs). Otherwise a spoken "run the tests" asked for a confirm phrase every time.
- **Known, undiagnosed:** the startup banner shows `server:undertone · no MCP server configured with that name` with the inline `--mcp-config`, although channel events and the `reply` tool work.

### 2026-10-01 — Voice-turn hard control (voice_guard)

**Added**
- `voice_guard.py`: hooks that gate tool calls made on a spoken turn. Hooks (`SessionStart`, `UserPromptSubmit`, `Stop`, `PreToolUse`) are injected via `claude --settings '<inline json>'` and run `python -P -m undertone.voice_guard <event> --home <UNDERTONE_HOME> --session <token>`.
- Session state at `~/.undertone/sessions/<token>.json` (0600, atomic writes). Session token: `secrets.token_hex(16)`.
- Session log at `~/.undertone/logs/guard.log` (0600). Records decisions only, never command text or prompt text.
- Token per `undertone session` launch is generated and passed to the channel as `UNDERTONE_SESSION` env var and to hooks as `--session`.
- Confirmation flow now a hard control: gated calls require a confirm phrase (`confirm <word1> <word2>`, one attempt, 300 s valid, case and punctuation ignored). Both words ≥3 letters, uncommon, fresh. Valid words unlock gated calls for the rest of the turn.
- Fail-closed: `SessionStart` must arm state, a failed `UserPromptSubmit` on a voice prompt drops that turn, and an unarmed state or failed recording makes the channel refuse voice turns. `PreToolUse` errors exit 2 (`|| exit 2` on the command). `PreToolUse` timeout is 10 s (documented limitation: Claude Code lets a call through on timeout).
- Gating allowlist includes read (`ls`, `pwd`, `cat`, `head`, `tail`, `wc`, `grep`, `which`, `type`, `whoami`, `uname`, `basename`, `dirname`, `realpath`, `stat`, `du`, `df`), `cd` to plain relative paths, `find` with read-only predicates only, read-only git (`status log diff show blame rev-parse ls-files describe shortlog branch remote tag` listing), and test runners with restricted args (`npm|pnpm|yarn|bun test`, `pytest`, `bats`, `make test|check`, `just test|check`, `cargo test`, `go test`).
- Never gated: `Read Grep Glob LS WebSearch TodoWrite Task Agent AskUserQuestion ToolSearch Skill EnterPlanMode ExitPlanMode BashOutput TaskOutput mcp__undertone__reply`.
- Live test: done; see the follow-up entry above.

### 2026-10-01 — Phase 1 retired (Stop hook and speaker daemon)

**Removed**
- The `Stop` and `UserPromptSubmit` (`SPOKEN:`) hooks (`hooks.py`) and the unix-socket speaker daemon (`speaker.sock`).
- The commands `undertone serve`, `say`, `stop`, `on`, `off`, `status`, `hook` and `hooks-snippet`.
- The `UNDERTONE_SPEAK` opt-in and the `enabled` flag file.
- `paths.socket_path()`, `paths.enabled_flag_path()` and `spoken.spoken_text()`.

**Changed**
- `speaker.py` now holds only `build_say_argv`, the single sanitizing `say` argv builder.
- `undertone session` no longer needs to disable the hook.

**Replaced by:** Phase 3. A voice session speaks Claude's `reply` tool text instead of a Stop-hook summary.

**Note:** if you registered the Phase 1 hooks, remove them from your Claude Code settings. A leftover registration (`… undertone hook stop || true`) fails harmlessly, because the `hook` subcommand is gone.

### 2026-10-01 — Phase 3: voice into a running session (PR #4)

**Added**
- `undertone attach`: the mic process. It runs the wake word, VAD and STT, sends transcripts over `~/.undertone/voice.sock` (0600, newline JSON), and speaks replies sentence by sentence. "Latest session wins" when several sessions attach.
- `mcp_channel.py`: a stdlib MCP stdio server that Claude Code spawns as a **Channel** (`python -P -m undertone.cli channel`). It turns each transcript into a `notifications/claude/channel` event that lands as `← undertone: …`, and exposes a `reply` tool whose `kind` is `ack`, `answer` or `confirm`.
- `undertone session [claude args...]`: runs `claude --mcp-config ~/.undertone/channel.mcp.json --dangerously-load-development-channels server:undertone --allowedTools mcp__undertone__reply`.

**Security**
- Voice turns keep full session power but are unverified input (Ed's decision: confirm risky actions). Before anything destructive, irreversible or outward-facing, Claude sends a `kind=confirm` reply and asks the user to say the wake word, then "confirm <two fresh uncommon words>". The listener strips the wake word from every transcript, so Claude accepts "confirm walnut lantern" with the words immediately after "confirm". "Yes" is a refusal.
- `attach` opens the no-wake-word follow-up window only after an `answer`, never after an `ack` or a `confirm`, so ambient audio can't answer a confirmation.
- undertone never speaks its own wake word. `speaker.build_say_argv` runs `spoken.sanitize_for_tts`, which strips invisible characters, removes every `[`/`]` (`say` inline commands), and replaces any spoken or obfuscated "hey jarvis"/"jarvis" with "the wake word".
- No permission relay is declared. Reply text is capped at 4000 chars, and `<`/`>` are stripped from utterances.

**Verified live (2026-10-01):**
- A stdlib Python server works as a channel. Claude Code probes `server/discover` (protocol 2026-07-28); answering -32601 makes it fall back to `initialize` 2025-11-25.
- The confirm flow passed: "confirm walnut lantern" deleted a test file.

**Commits:** 0a983dc, 807e6f2, 5866265, 8735486, 1dcf551

### 2026-10-01 — Docs unit D

**Added**
- README rewritten around "Local-first by design", with a data-flow table, two ways to verify it yourself, components and licenses, and the status and roadmap.
- `docs/local-first.md`, `docs/architecture.md`, `docs/training.md`, `SECURITY.md`, and this changelog.
- `scripts/verify-offline.sh` and `scripts/offline_check.py`: run wake word → VAD → STT → TTS on a generated clip inside a macOS `sandbox-exec` profile that denies all network access. A canary inside the sandbox must fail to connect and resolve, so the check proves the denial rather than assuming it.
- `LICENSE` (Apache-2.0), also declared in `pyproject.toml`.

### 2026-10-01 — Phase 2b: `undertone chat` (PR #2)

**Added**
- `undertone chat [--no-beep] [--resume] [--follow-up SECONDS]` is a hands-free voice conversation. Say "hey jarvis", ask, and hear Claude's reply spoken sentence by sentence while it streams.
- Barge-in: saying "hey jarvis" while Claude is thinking or speaking stops speech and interrupts the turn.
- A follow-up window (default 6 s) after each reply lets you ask the next question without the wake word.
- Persistent `claude -p` stream-json child (`brain.py`). Turns are interrupted with a raw `control_request`, so no SDK is needed. The session id is saved for `--resume`.
- Sentence chunker (`chunker.py`): it splits streamed text deltas at sentence ends or newlines, skipping decimals and a small set of abbreviations. Fenced code becomes "Code's on screen.", and a 240-char soft cap applies.
- In-process speech queue (`player.py`): it plays sentences strictly in order while Claude is still streaming. The Phase 1 daemon can't do this, because its `say` op interrupts the current speech.
- A latency line per turn: speech end → first `say`, plus Claude's first token.

**Changed**
- `listener.py` gained `state` and `listen_now()` for the follow-up window.
- The `say` argv is now built in one place, `speaker.build_say_argv`, shared by the daemon and the queue.
- The default voice is now Evan (Enhanced), a one-time download in System Settings. `UNDERTONE_VOICE` overrides it.

**Security**
- The voice child runs `--restricted --strict-mcp-config --tools Read,Grep,Glob,WebSearch --permission-prompts none`. User settings and MCP servers are not loaded, and file tools are confined to the working directory. There is no Bash and no WebFetch.
- Fail-closed policy check: every `system/init` must advertise exactly those tools and no MCP servers, and any output before a verified init kills the child.
- `chat` swaps in an empty `~/.undertone/workspace` when started from home or an ancestor of it, a dot-directory or `Library` under home, or the top level of `Documents`, `Desktop` or `Downloads`.
- Terminal control characters are stripped from everything `chat` prints.
- `chat_session` is written atomically at 0600, and `brain.log` is opened 0600 with `O_NOFOLLOW`.

**Commits:** d972747, 0ee198f, c9594d9, d8aef8d, 5e74fc3, 3038ae5

### 2026-09-30 — Phase 2a: listening core (PR #1)

**Added**
- `undertone listen` runs mic → wake word (pymicro-wakeword "Hey Jarvis") → Silero VAD end-of-turn (0.7 s of silence) → on-device STT (parakeet-mlx), and prints one JSON event per line.
- `undertone models fetch` downloads the STT model (~2.5 GB) from Hugging Face, once. The wake-word model ships inside pymicro-wakeword.
- `HF_HUB_OFFLINE` defaults to 1 at import, so loading the model never contacts the Hub.
- STT warm-up: the first call drops from 823 ms to 54 ms.

**Commits:** 1083717

### 2026-09-30 — Phase 1: spoken replies

**Added**
- `undertone serve` is a local speaker daemon on a unix socket (0600) that speaks text with `say`.
- A `Stop` hook speaks a short summary of each Claude Code reply. A `UserPromptSubmit` hook asks Claude to end each reply with a `SPOKEN: …` line, which is used verbatim when present.
- Opt-in only: `undertone on`/`off`/`status` for the machine, or `UNDERTONE_SPEAK=1 claude` for one session.
- `undertone hooks-snippet` prints the hook registration JSON.

**Commits:** 8cef189

### 2026-09-30 — Phase 0: research and measurement

**Added**
- Research plan and component picks (`docs/plan.md`). The wake phrase is "hey jarvis", which ships pretrained, so no training is needed.
- Measurements:
  - wake word: 6/6 detected and 0 false accepts in a 30-minute live run;
  - STT median: 87 ms on a real voice;
  - persistent `claude -p` first token: about 1.2 s.
- Claude Code Channels, the research-preview way to push events into a running session, verified GO with a hello-world.
- An optional Docker image for training a custom wake phrase on 100% synthetic TTS clips.

**Commits:** ca64eb8, 4988819, ca9bc93, 53010e5, 69760f7

# Security Policy

## Reporting

undertone is a personal, single-user macOS tool. Private vulnerability reports can be submitted via GitHub private vulnerability reporting or a Security Advisory on [`ek33450505/undertone`](https://github.com/ek33450505/undertone).

## Threat Model and Controls

undertone is designed for local-first, voice-driven interaction with Claude. The following threat model and mitigations document what is built and verified in the code.

### 1. Open Microphone

**Threat:** While `undertone chat` or `undertone listen` runs, the microphone stream is open and every 10 ms frame goes through the wake-word detector in memory. Any audible sound nearby, such as a video, a call or someone speaking, can trigger the wake word "hey jarvis". After a Claude reply ends, there is a 6-second follow-up window during which no wake word is needed to issue a follow-up command. In Phase 4 (2026-10-01), users can also interrupt by talking over a reply without the wake word (talk-over), widening the no-wake-word input surface to include the entire playback of every ack or answer.

**Mitigations:**
- Microphone access is granted per app by macOS TCC (Transparency, Consent, and Control). undertone uses the permission of the terminal app you run it in.
- Audio is buffered in memory only after the wake word, during the follow-up window, or during talk-over playback. No microphone audio is written to disk (rendered speech uses transient files, see §8), and each turn's buffer is discarded once it has been transcribed.
- On-device STT turns the buffered audio into text. Only that text reaches the `claude` child; the audio is never stored or uploaded.
- Echo cancellation (livekit AEC3, local-only) removes undertone's own speech from the talk-over stream before VAD and STT, reducing false triggers on playback.
- Talk-over turns are **gated by voice_guard** (see control #7a): a talk-over turn cannot confirm or execute gated commands.
- The open microphone, short follow-up window, and talk-over surface are inherent to a voice interface. STT can mishear, so the Claude child is read-only (see control #2).
- **User controls:** `--no-beep` silences the wake and follow-up cue sounds (cosmetic only; listening is unchanged); `--no-aec` disables echo cancellation and talk-over; `--follow-up 0` disables the 6-second follow-up window; Ctrl-C exits immediately.

### 2. Headless Claude Child (`brain.py`)

**Threat:** `undertone chat` drives a headless `claude -p` child, and headless mode has none of Claude Code's interactive confirm-pauses. The microphone is open. A prompt injection via STT mishearing or web search results could trick Claude into reading sensitive files and exfiltrating them.

**Mitigations:**
- **Restricted tool set:** The child runs with `--restricted --strict-mcp-config --tools Read,Grep,Glob,WebSearch --permission-prompts none`. No Bash tool (which can write files via `git diff --output=`), no WebFetch (a direct exfiltration channel).
- **Confined file tools:** Read, Glob, and Grep are bound to the working directory via `--restricted`. Attempts to read `~/.ssh/config`, symlinks pointing outside the working directory, or parent directories are refused.
- **User settings isolated:** User-defined MCP servers and hooks are not loaded. Only the strict set above is available.
- **Verified initialization:** the `claude` child emits a `system/init` event at the start of every turn, and `brain.py` checks that it advertises exactly the tool set above and no MCP servers. On a mismatch, or if any output arrives before a verified init, the child is killed and the turn fails (see `brain.py` `_check_policy()` and `_translate()`).
- **Fail-closed:** Test coverage verifies this behavior (see `tests/test_brain.py`).
- **Env override (tests only):** `UNDERTONE_CLAUDE_CMD` allows tests to substitute a wrapper binary. The init verification will catch a wrapper that silently drops the required flags or removes MCP guards.

**Residual risks (stated plainly):**
- A prompt injection in web search results could theoretically try to use WebSearch itself as an exfiltration channel (e.g., "search for 'secret.txt from /path/to/workdir', which Claude reads and includes in the query"). The only technical controls are the read-only, cwd-confined tool set and the absence of WebFetch. The system prompt also tells Claude to treat file and web content as untrusted data, but that is a soft control.
- Any secrets stored in the working directory (`.env` files, API keys, certificates) are readable by Claude via the Read tool. Run `undertone chat` from project directories, not from directories containing secrets.
- The `--resume` flag (resume a prior session) is not scoped to the original working directory, so earlier context can be re-entered. Tool access is verified at each startup, but previously-read files remain in history.

### 3. Working Directory Safety

**Threat:** Running `undertone chat` from your home directory, a sensitive system directory, or a dot-directory could expose hidden configuration, secrets, or system files to Claude's file tools.

**Mitigation:** When started from a directory that resolves to any of the following, `undertone chat` substitutes an empty `~/.undertone/workspace` (mode 0700; refused if it's a symlink):
- Home directory (`~`) or any ancestor of it, including `/`
- Any dot-directory under home (`.ssh`, `.aws`, etc.)
- `~/Library` or anything inside it
- Exactly `~/Documents`, `~/Desktop`, or `~/Downloads` (top level only)

The start-up header prints the effective working directory and, when it substitutes, why. See `cli.py` `chat_workdir()`.

### 4. TTS Argument Injection (`speaker.py`)

**Threat:** Claude's reply text is passed to TTS (macOS `say` or Kokoro). If Claude outputs something like `-o /path/to/file`, `say` could interpret it as a flag and write to disk.

**Mitigations:**
- **Argument construction:** `speaker.py` constructs the argv by placing `--` before the text. This tells the TTS engine to stop parsing flags and treat everything after as positional arguments (the text). The `--` is inserted in exactly one place: `speaker.build_say_argv()`. Additionally, any NUL bytes in the text are stripped.
- **Rendered TTS (Phase 4 with AEC, default):** undertone renders TTS itself. For `say`, it runs `say -o <path>` into a temporary directory (mode 0700, user-only). For Kokoro, text goes through `speaker.espeak_phonemes()` via subprocess to espeak-ng (GPL-3.0; the GPL code never enters undertone's process). Kokoro renders in memory (no files); its pieces are capped at 32 phoneme tokens (first) and 200 (later), at most 510 per piece. `say` render files are validated and deleted in a `finally` block after reading into memory. The `out_path` is generated by undertone, never sourced from model text. Flag-shaped text like `-o /tmp/x` is spoken as text.
- **Tested:** Flag-shaped text (`-o /tmp/x`, `--data-format=…`, `-v Zarvox`) was probed and each was spoken as text.
- See `speaker.py`, `player.py` (`SayRenderer`, `KokoroRenderer`), and `kokoro.py`.

### 5. Terminal Escape Injection (`chat.py`)

**Threat:** Claude's reply may contain control characters that, when printed to a terminal, could execute terminal sequences, switch color modes, or hide text.

**Mitigation:** `chat.py` strips C0 (0x00–0x1F) and C1 (0x80–0x9F) control characters from all model output before printing, with the exception of newline (`\n`, 0x0A) and tab (`\t`, 0x09). See `spoken.sanitize_terminal()`, which `chat.py` and `voice.py` apply to everything they print.

### 6. Local Voice Socket (`voice.py`)

**Threat:** The `attach` process listens on a Unix domain socket for incoming voice turns. If the socket is world-writable or in a shared directory, any process could send commands.

**Mitigations:**
- The socket is created at `~/.undertone/voice.sock`, owned by the user, with mode 0600 inside a mode 0700 parent directory (`~/.undertone`).
- `attach` refuses to start if something other than a socket sits at the path, or if another `attach` is already answering. A stale socket is unlinked only when nothing answers.
- The socket is bound under `umask 0o177` and then chmod'd 0600, so it is never wider than owner-only.
- Messages are newline-delimited JSON. A line over 64 KB drops that connection, and malformed or unknown lines are ignored. At most 16 connections are kept, and extras are closed.
- Each connection has its own reader thread, so a failure in one never stops the mic loop or other connections.
- "Latest session wins": a newer channel's `hello` sends `{"type":"detached"}` to the older one and closes it.

### 7. Claude Code Channel MCP Server (`mcp_channel.py`)

**Threat:** The channel is spawned as a stdio MCP server by Claude Code and receives voice transcripts from `undertone attach`. It is bidirectional: Claude can also call the `reply` tool to speak answers. All input is unverified spoken text.

**Mitigations:**
- **No permission relay:** the channel does not forward tool approvals to the voice process. Claude decides which tools to call in its running session, using all available context and its instructions.
- **No module shadowing:** Claude Code starts the channel as `python -P -m undertone.cli channel`. `-P` keeps the session's working directory off `sys.path`, so a hostile checkout can't shadow undertone's modules.
- **Input/output limits:** `reply` text over 4000 characters is rejected. Inbound utterances have `<` and `>` stripped before they are sent to the session, so a transcript can't close or forge the `<channel>` tag it arrives in.
- **stdout is JSON-RPC only:** logs go to a 0600 `channel.log` and never include transcripts or reply text.
- **No follow-up window after confirm:** `attach` opens the no-wake-word follow-up window only after an `answer` reply. After an `ack` or a `confirm`, the user must say the wake word again, so ambient audio can't answer a confirmation question.
- **Confirmation is now a hard control:** see "Voice-turn guard (`voice_guard.py`)" below.

### Acoustic Self-Screen (Phase 4b, 2026-10-02)

**Threat:** the wake detector stays live while undertone speaks. If undertone's own speech fires it, the listener opens a `via: "wake"` turn, the only kind that can grant a voice confirmation. A (possibly prompt-injected) reply can carry a homophone the text scrub misses: "hey jarviss", "djarvis", "jarvice", "jarvus" and "charvis" pass it, and spoken by Kokoro they fired the detector on 4/4 voices tested (`say` with Evan did not).

**Mechanism:**
- **Text-level scrub:** `speaker.build_say_argv()` and `speaker.espeak_phonemes()` both start with `speaker.prepare_speech()`: NUL and lone-surrogate strip, NFKC (so fullwidth brackets and letters can't slip past), then `spoken.sanitize_for_tts()` (strips invisible characters, removes every `[` and `]` because both `say` and espeak-ng honor `[[…]]` inline commands, and replaces any form of "hey jarvis" or "jarvis" with "the wake word"), then a 1000-character cap (`MAX_SPEECH_CHARS`). This keeps the literal wake phrase from being spoken; homophones are left to the acoustic screen.
- **Acoustic self-screen:** `screen.ScreenedSink` wraps a `player.PlaybackSink` and runs the detector on the last 2 s of approved audio (`HISTORY_S`), the modeled silence gap, the current chunk, and 1 s of lookahead silence (`LOOKAHEAD_S`). The detector fires 50–250 ms after a phrase ends, hence the lookahead. If it fires, the chunk is dropped and `[skipped speech that sounded like the wake word]` is printed.
- **Cutoff and calibration (screen 0.8 vs live 0.97, 2026-10-02):** 0/200 ordinary sentences blocked; 67/80 homophone attack clips blocked with the phrase at the chunk end; every clip that played peaked below 0.8, so none would trip the live 0.97 detector. The detector fails closed on error and keeps history across a barge-in `clear()`.
- **Security review:** the split attack, 888 cut points and noise-prefix scenarios found 0 bypasses.
- See `screen.py`.

### Undertone Never Speaks Its Own Wake Word

**Threat:** If undertone speaks the phrase "hey jarvis" in a reply, it could trigger the wake word detector and capture unintended speech. With talk-over (Phase 4), undertone's own speech leaking through imperfect echo cancellation could also become a talk-over turn; the provenance control in §7a means such a turn can never confirm.

**Mitigation:** `speaker.build_say_argv()` calls `spoken.sanitize_for_tts()`, which strips invisible characters, removes `[` and `]` (macOS `say` uses `[[…]]` for inline commands), and replaces any text matching "hey jarvis", "jarvis", or obfuscated forms with "the wake word". undertone's instructions also explicitly tell Claude never to speak the wake phrase. The sanitization is still necessary with AEC: it ensures the wake-word detector only fires on user speech, not undertone's own playback, even through imperfect echo cancellation.

### 7a. Voice-turn guard (`voice_guard.py`)

**Threat:** Voice turns are unverified spoken input. A stray wake word or misheard phrase could trigger destructive or outward-facing actions (delete or overwrite files, commit or push, call an MCP tool that changes remote state). In Phase 4, talk-over turns (speech during playback without the wake word) widen the unverified input surface.

**Mechanism:**
- **Hooks:** Six hooks (`SessionStart`, `UserPromptSubmit`, `Stop`, `SubagentStart`, `SubagentStop`, `PreToolUse`) are injected into the session via `--settings` inline JSON. Each runs `python -P -m undertone.voice_guard <event> --home <UNDERTONE_HOME> --session <token>`. Events are: `sessionstart`, `userpromptsubmit`, `stop`, `subagentstart`, `subagentstop`, `pretooluse`. `pretooluse` can block a tool call, and `userpromptsubmit` can drop a voice turn it cannot record. `sessionstart`, `stop`, `subagentstart` and `subagentstop` always exit 0, because exit 2 on `Stop` or `SubagentStop` would keep Claude or the subagent running.
- **Subagents:** Subagents can run in the background, so a voice turn can end (`Stop`) while a subagent it started keeps working. Each subagent is recorded by its `agent_id` at `SubagentStart`, as voice-born if a voice turn is active or any voice-born subagent is still recorded (until its `SubagentStop`). That rule also catches a nested child started after the user typed. A voice-born subagent stays gated until `SubagentStop`, even after the user types. Its gated calls are allowed only if it started while a grant was valid (inherited for 300 s, revoked by a typed prompt) or if a new grant is valid. In a session that has had a voice turn, a subagent whose `agent_id` is unknown or invalid is treated as voice-born; this covers a missed `SubagentStart`, an early `SubagentStop` and an evicted record. Only a subagent positively recorded as typed-origin runs ungated.
- **Arming:** The channel (`mcp_channel.py`) refuses a voice turn in three cases: the session token is missing, the `SessionStart` hook hasn't armed state for this launch, or recording the turn failed. When refused, the channel speaks "Voice turns are off:" instead of delivering the turn. This fails safe: if hooks don't load, voice is refused rather than unguarded.
- **Provenance:** Claude Code fires `UserPromptSubmit` for channel events. Its prompt is `<channel source="undertone" input="voice">…</channel>`, so a prompt containing `<channel` counts as a voice turn. A prompt starting with `<` that is not a channel event (an injected envelope such as a background-task notification) changes nothing. Any other non-empty prompt counts as typed. A voice turn stays gated until its `Stop`, even if the user types mid-turn; after that, a typed prompt ungates. If a voice turn is interrupted (Esc, no `Stop`), the session stays gated until a later turn completes.
- **Gating:** During an active voice turn, gated tool calls require confirmation. Never gated: `Read Grep Glob LS WebSearch TodoWrite Task Agent AskUserQuestion ToolSearch Skill EnterPlanMode ExitPlanMode BashOutput TaskOutput mcp__undertone__reply`. `Bash` goes through a fail-closed allowlist (read commands, `cd` to plain relative paths, `find` with read-only predicates only, read-only git, test runners with restricted args); any command using shell expansion or special characters, a redirect other than to `/dev/null` or an fd dup, a heredoc, an env-var assignment or a non-ASCII character is gated, and so is every other tool.
- **Confirm flow:** Claude's `reply` with `kind="confirm"` must contain `confirm <word1> <word2>`. Both words ≥3 letters, uncommon, different, not used before in the session. Otherwise the reply tool returns an error. The channel records the challenge; one attempt, valid 300 s. If the next voice message contains `confirm <word1> <word2>` (case and punctuation ignored), that creates a grant. Any other message cancels the challenge. On a successful confirm, gated calls are allowed for the rest of that turn (until `Stop`, the next voice message, a typed prompt, or 300 s).
- **Self-wake guard (`SELF_WAKE_GUARD_S = 1.0`):** A wake word heard while undertone is speaking, or within `SELF_WAKE_GUARD_S` s after, gets `via: "wake_barge_in"`. It still interrupts, but never grants (only `via == "wake"` grants). `UNDERTONE_SELF_WAKE_GUARD_S` (floor 1.0, cap 10.0) is for high-latency outputs; AirPlay is about 2 s, so set `3` to cover it. A downgraded turn uses up the confirm attempt; Claude re-asks. Restart both `attach` and `session` after upgrading.

#### Hard Control: Voice Turn Provenance (Phase 4, 2026-10-01)
Every utterance on the voice socket now carries `"via": "wake"|"follow_up"|"talk_over"`, which opened the turn:
- The channel passes `can_grant = (via == "wake")` to `voice_guard.record_voice()`.
- Only `can_grant=True` plus the confirm phrase grant; talk-over and follow-up turns cannot confirm.
- Gated tool calls in a non-grantable turn fail closed.

**Verified end to end through the real code (2026-10-01; not yet with live speech):** a real `ChannelServer` over a real unix socket, the session armed by the real `SessionStart` hook, a confirm challenge recorded through the real `reply` tool, then the real `PreToolUse` hook command run with a `Write` payload after each utterance:
- `via` `"talk_over"`, `"follow_up"`, missing, `["wake"]`, `"WAKE"` or `"wake "` → no grant, challenge consumed, `Write` denied.
- `via` `"wake"` with a fresh challenge → grant, and the guard allows `Write` (your normal permission prompts still apply).
- Any voice turn uses up the challenge's one attempt, so an ambient or echoed talk-over turn can only cancel a pending confirmation, never satisfy it.
- An `undertone attach` started before this change sends no `via`, so confirmations fail closed until both `attach` and `session` are restarted.

**Residual risks (talk-over, stated plainly):**
- **Before the fix (2026-10-01):** a talk-over turn could capture undertone's own spoken "confirm <w1> <w2>" through imperfect AEC and grant it. A talk-over stream already listening when a confirm reply was spoken could capture the reply. A follow-up window could open after a confirm reply. All three are now blocked by the hard control.
- **Imperfect AEC:** ambient speech during playback can still open a talk-over turn, but it is gated (cannot confirm or run Write-class tools). AEC was live-tested once (2026-10-02, MacBook Pro speakers and mic, one room); other rooms, external speakers and high volumes are untested. `--no-aec` turns talk-over off.

**Fail-closed:**
- `pretooluse` errors exit 2, and the hook command ends in `|| exit 2`, so a missing or crashing interpreter blocks the call instead of allowing it.
- `userpromptsubmit` errors on voice prompts exit 2, which drops that voice turn. Typed prompts are never blocked.
- `pretooluse` returns `permissionDecision: "deny"` with a reason starting `undertone voice guard:`. Claude is told to ask with `kind="confirm"`. Allowing a call means exit 0 with no output, so the user's normal permission prompts still apply. The guard never auto-approves.
- `pretooluse` timeout is 10 s. Claude Code lets a call through on timeout (documented limitation).

**Residual risks (stated plainly):**
- `WebSearch` is ungated, so a query could carry data out under prompt injection.
- `Read`/`Grep`/`Glob` and `cat`/`grep` can read secrets in the working directory.
- A grant covers the rest of the turn, not one specific command.
- Test runners execute repo code (`conftest.py`, `Makefile`, `package.json` scripts, `build.rs`).
- Git config programs (`core.fsmonitor`, `diff.external`) need a prior write to `.git/config`.
- `Task`/`Agent` are ungated. That is safe because a subagent's own tool calls pass through the same `PreToolUse` hook (verified live).
- Your shell aliases and functions are trusted, because Claude Code sources your shell snapshot. They can change what an allowlisted name runs: on the development machine `ls` is `eza`, `cat` is `bat`, `find` runs bfs and `grep` runs ugrep. The allowlist was probed against those, and `find` takes only an allowlist of read-only predicates for that reason (bfs adds actions such as `-rm`).
- The subagent rules assume Claude Code delivers `SubagentStart` before the voice turn's `Stop`. The live test was consistent with that, but it is not documented. If `SubagentStart` arrived after a typed prompt with no other voice-born subagent recorded, that subagent would be recorded typed-origin.
- A voice-born subagent that crashes without `SubagentStop` keeps new subagents gated until `undertone session` restarts or the user speaks a confirm phrase. That is the safe direction.
- Narrow timing cases: a voice event queued during a typed turn, and a background-task turn after a voice turn's `Stop` when you had typed. Both require you at the keyboard.

**Live test (2026-10-01, Claude Code 2.1.286, real speech):**
- Arming worked.
- `voice`, `typed` and `other` classification all appeared (`other` was a background-task notification).
- Read-only commands went through and a risky `Bash` call was denied.
- `confirm` then a spoken phrase gave a grant, and the gated test run went through. The grant ended at `Stop`.
- Answering "never mind" cancelled a pending challenge.
- A subagent's `Write`, and its `touch` workaround, were both denied.
- A typed turn after the voice turn ended was ungated.
- The test found that a background subagent could outlive its voice turn. Subagent tracking (above) was added for it.

**Live test, Phase 4a (2026-10-02, real speech, speakers on):**
- A full spoken reply played without opening a talk-over turn on undertone's own voice.
- Talking over a reply stopped it. The first build then dropped the whole turn as echo, losing the user's words; `strip_echo` now removes only the repeated words.
- Talking over a confirm prompt stopped it and the phrase was never heard; talk-over is now off during confirm prompts.
- A wake-word turn "Confirm Lemon Canyon." granted the gated delete (`pretooluse granted Bash`) and the file was removed. An earlier unanswered challenge was used up by the next voice turn, as designed.
- STT turned "hey jarvis" into "A Jarvis.", which reached Claude; the wake strip now handles it.
- Re-test after the fixes: reported working by Ed (per-step output not captured).

### 8. Local Files

**Threat:** Persistent state files could be read by other processes, forged, or used to track behavior. Temporary files could be read or recovered.

**Mitigations:**
- **Chat session state:** `~/.undertone/chat_session` (stores the current session ID) is written atomically, opened with mode 0600 (user-read/write only), and validated on load with UUID format checks.
- **Brain logs:** `~/.undertone/logs/brain.log` is opened with O_NOFOLLOW (refusing symlinks) and mode 0600. Log rotation occurs at 1 MB. See `brain.py` `_open_log()`.
- **Voice-turn session state:** `~/.undertone/sessions/<token>.json` (file 0600, directory 0700) stores per-launch state for the voice guard. Atomic writes, symlinks refused, lock file `<token>.lock`.
- **Voice-turn guard log:** `~/.undertone/logs/guard.log` (0600) records decisions only, never command text or prompt text.
- **Rendered TTS (Phase 4):** When AEC is on, TTS audio is rendered into `tempfile.mkdtemp(prefix="undertone-tts-")` (mode 0700, user-only). This applies to the `say` renderer only (Kokoro renders in memory and writes no files). Files are `.wav`, validated as 1 channel, 16-bit, 16 kHz, and named `<counter>.wav`, unlinked in a `finally` block immediately after reading into the playback buffer. The directory is removed when `RenderedSpeechQueue` closes. No audio is written anywhere else except the transient playback buffer in RAM.
- No user secrets or API keys are stored in these files; Claude Code's login token is used to authenticate (inherited from the user's session).

### 9. Network

**Threat:** If undertone sends audio or other sensitive data over the network, it could be intercepted or logged.

**Mitigation:** Only the headless `claude` child talks to Anthropic's API. Audio never leaves your Mac—it is processed locally by wake-word detection, VAD, STT, and TTS. Only the transcribed text is sent to Claude, along with any working-directory files Claude reads and its WebSearch queries.

**Telemetry (onnxruntime, Phase 4b):** onnxruntime 1.30's macOS wheel embeds Microsoft's 1DS telemetry client (endpoint `mobile.events.data.microsoft.com`). `src/undertone/kokoro.py` FORCES `ORT_DISABLE_TELEMETRY=1` before onnxruntime is imported and calls `onnxruntime.disable_telemetry_events()`. With the variable set, ORT no longer initializes its telemetry device ID.

**Model integrity (Kokoro, Phase 4b):** the model, tokenizer and voice files come from a pinned Hugging Face revision and are loaded from the local cache only (`local_files_only=True`). Each is also checked against a SHA-256 pinned in `src/undertone/kokoro.py` every time it loads; a mismatch, a corrupt model or a malformed tokenizer raises `KokoroUnavailable`, and undertone falls back to `say`.

See [`docs/local-first.md`](docs/local-first.md) for full detail on what leaves your Mac and when. Run `bash scripts/verify-offline.sh` to verify wake word, VAD, STT, TTS, and Kokoro run entirely offline inside a macOS network sandbox.

---

**Last updated:** 2026-10-02

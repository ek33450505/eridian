# Security Policy

Supported versions: only the latest `main`.

## Reporting

eridian is a personal, single-user macOS tool. Private vulnerability reports can be submitted via GitHub private vulnerability reporting or a Security Advisory on [`ek33450505/eridian`](https://github.com/ek33450505/eridian).

## Threat Model and Controls

eridian is designed for local-first, voice-driven interaction with Claude. The following threat model and mitigations document what is built and verified in the code.

### 1. Open Microphone

**Threat:** While `eridian chat` or `eridian listen` runs, the microphone stream is open and every 10 ms frame goes through the wake-word detector in memory. Any audible sound nearby, such as a video, a call or someone speaking, can trigger the wake word "hey jarvis". After Claude's first answer to a wake-word turn, there is a 6-second follow-up window during which no wake word is needed to issue a follow-up command. Talk-over lets users interrupt a reply by talking over it without the wake word, widening the no-wake-word input surface to include the entire playback of every ack or answer.

**Mitigations:**
- Microphone access is granted per app by macOS TCC (Transparency, Consent, and Control). eridian uses the permission of the terminal app you run it in.
- Audio is buffered in memory only after the wake word, during the follow-up window, or during talk-over playback. No microphone audio is written to disk (rendered speech uses transient files, see §8), and each turn's buffer is discarded once it has been transcribed.
- On-device STT turns the buffered audio into text. Only that text reaches the `claude` child; the audio is never stored or uploaded.
- Echo cancellation (livekit AEC3, local-only) removes eridian's own speech from the talk-over stream before VAD and STT, reducing false triggers on playback.
- Talk-over turns are **gated by voice_guard** (see control #7a): a talk-over turn cannot confirm or execute gated commands.
- The open microphone, short follow-up window, and talk-over surface are inherent to a voice interface. STT can mishear, so the Claude child is read-only (see control #2).
- **User controls:** `--no-beep` silences the wake and follow-up cue sounds (cosmetic only; listening is unchanged); `--no-aec` disables echo cancellation and talk-over; `--follow-up 0` disables the 6-second follow-up window; Ctrl-C exits immediately.

### 2. Headless Claude Child (`brain.py`)

**Threat:** `eridian chat` drives a headless `claude -p` child, and headless mode has none of Claude Code's interactive confirm-pauses. The microphone is open. A prompt injection via STT mishearing or web search results could trick Claude into reading sensitive files and exfiltrating them.

**Mitigations:**
- **Restricted tool set:** The child runs with `--restricted --strict-mcp-config --tools Read,Grep,Glob,WebSearch --permission-prompts none`. No Bash tool (which can write files via `git diff --output=`), no WebFetch (a direct exfiltration channel).
- **Confined file tools:** Read, Glob, and Grep are bound to the working directory via `--restricted`. Attempts to read `~/.ssh/config`, symlinks pointing outside the working directory, or parent directories are refused.
- **User settings isolated:** User-defined MCP servers and hooks are not loaded. Only the strict set above is available.
- **Verified initialization:** the `claude` child emits a `system/init` event at the start of every turn, and `brain.py` checks that it advertises exactly the tool set above and no MCP servers. On a mismatch, or if any output arrives before a verified init, the child is killed and the turn fails (see `brain.py` `_check_policy()` and `_translate()`).
- **Fail-closed:** Test coverage verifies this behavior (see `tests/test_brain.py`).
- **Env override (tests only):** `ERIDIAN_CLAUDE_CMD` allows tests to substitute a wrapper binary. The init verification will catch a wrapper that silently drops the required flags or removes MCP guards.

**Residual risks (stated plainly):**
- A prompt injection in web search results could theoretically try to use WebSearch itself as an exfiltration channel (e.g., "search for 'secret.txt from /path/to/workdir', which Claude reads and includes in the query"). The only technical controls are the read-only, cwd-confined tool set and the absence of WebFetch. The system prompt also tells Claude to treat file and web content as untrusted data, but that is a soft control.
- Any secrets stored in the working directory (`.env` files, API keys, certificates) are readable by Claude via the Read tool. Run `eridian chat` from project directories, not from directories containing secrets.
- The `--resume` flag (resume a prior session) is not scoped to the original working directory, so earlier context can be re-entered. Tool access is verified at each startup, but previously-read files remain in history.

### 3. Working Directory Safety

**Threat:** Running `eridian chat` from your home directory, a sensitive system directory, or a dot-directory could expose hidden configuration, secrets, or system files to Claude's file tools.

**Mitigation:** When started from a directory that resolves to any of the following, `eridian chat` substitutes an empty `~/.eridian/workspace` (mode 0700; refused if it's a symlink):
- Home directory (`~`) or any ancestor of it, including `/`
- Any dot-directory under home (`.ssh`, `.aws`, etc.)
- `~/Library` or anything inside it
- Exactly `~/Documents`, `~/Desktop`, or `~/Downloads` (top level only)

The start-up header prints the effective working directory and, when it substitutes, why. See `cli.py` `chat_workdir()`.

### 4. TTS Argument Injection (`speaker.py`)

**Threat:** Claude's reply text is passed to TTS (macOS `say` or Kokoro). If Claude outputs something like `-o /path/to/file`, `say` could interpret it as a flag and write to disk.

**Mitigations:**
- **Argument construction:** `speaker.py` constructs the argv by placing `--` before the text. This tells the TTS engine to stop parsing flags and treat everything after as positional arguments (the text). The `--` is inserted in exactly one place: `speaker.build_say_argv()`. Additionally, any NUL bytes in the text are stripped.
- **Rendered TTS (with AEC, default):** eridian renders TTS itself. For `say`, it runs `say -o <path>` into a temporary directory (mode 0700, user-only). For Kokoro, text goes through `speaker.espeak_phonemes()` via subprocess to espeak-ng (GPL-3.0; the GPL code never enters eridian's process). Kokoro renders in memory (no files); its pieces are capped at 32 phoneme tokens (first) and 200 (later), at most 510 per piece. `say` render files are validated and deleted in a `finally` block after reading into memory. The `out_path` is generated by eridian, never sourced from model text. Flag-shaped text like `-o /tmp/x` is spoken as text.
- **Tested:** Flag-shaped text (`-o /tmp/x`, `--data-format=…`, `-v Zarvox`) was probed and each was spoken as text.
- See `speaker.py`, `player.py` (`SayRenderer`, `KokoroRenderer`), and `kokoro.py`.

### 5. Terminal Escape Injection (`chat.py`)

**Threat:** Claude's reply may contain control characters that, when printed to a terminal, could execute terminal sequences, switch color modes, or hide text.

**Mitigation:** `chat.py` strips C0 (0x00–0x1F) and C1 (0x80–0x9F) control characters from all model output before printing, with the exception of newline (`\n`, 0x0A) and tab (`\t`, 0x09). See `spoken.sanitize_terminal()`, which `chat.py` and `voice.py` apply to everything they print.

### 6. Local Voice Socket (`voice.py`)

**Threat:** The `attach` process listens on a Unix domain socket for incoming voice turns. If the socket is world-writable or in a shared directory, any process could send commands.

**Mitigations:**
- The socket is created at `~/.eridian/voice.sock`, owned by the user, with mode 0600 inside a mode 0700 parent directory (`~/.eridian`).
- `attach` refuses to start if something other than a socket sits at the path, or if another `attach` is already answering. A stale socket is unlinked only when nothing answers.
- The socket is bound under `umask 0o177` and then chmod'd 0600, so it is never wider than owner-only.
- Messages are newline-delimited JSON. A line over 64 KB drops that connection, and malformed or unknown lines are ignored. At most 16 connections are kept, and extras are closed.
- Each connection has its own reader thread, so a failure in one never stops the mic loop or other connections.
- **Multi-attach routing:** every channel whose `hello` is accepted stays connected, and a new `hello` does not detach the others. Wake-word turns go to the most recently attached session with voice on. `{"type":"detached"}` is sent, and the old connection closed, only when a `hello` carries the same session token as a live connection (a reconnect). A `hello` without a valid token is listed but never gets voice.
- **Announcements.** A hook opens a one-shot connection with no `hello`, sends `{"type":"announce","session":"<32-hex token>","event":"<event>"}` and closes. `attach` speaks it only if the token equals (`hmac.compare_digest`) the one the attached channel sent in its `hello`, and only if `--announce` enables the event; anything else is ignored. The token is a routing check, not a secret against the same user: it is on the hook command lines, visible via `ps`, and a same-user process could already send `hello` and `reply` lines. Announcements speak fixed templates only, never Claude's text, and each event is spoken at most once per 3.0 s. No announcement opens the follow-up window, and "Claude finished." is suppressed while a confirm is pending.

#### Announce hook (`announce.py`)

`eridian session` runs `python -P -m eridian.announce <hook> --home <ERIDIAN_HOME> --session <token>` as async hooks: a second handler on the `Stop` entry (after the voice guard's), plus `StopFailure` and `PermissionRequest`. Async hooks can't block Claude or decide a permission.
- It always exits 0 and writes nothing to stdout or stderr, because `PermissionRequest` stdout is a decision channel. Every path runs inside `try/except BaseException`, the argument parser never prints, and the `voice_guard` import is lazy, so a broken install stays silent too.
- It drains stdin first, and it forwards nothing from the payload: the line it sends holds only the token and a fixed event key.
- It sends nothing unless `--home` is absolute, the token is 32 hex characters, and `<home>/voice.sock` is a socket (`lstat`, so not a symlink) owned by the same uid. It connects with a 0.5 s timeout and never reads from the socket.
- It logs at most one line per run to `<home>/logs/guard.log`: `announce <sent|skipped|error> <hook> <event or reason>`, where the reason is `bg-agents`, `no-socket`, `bad-token` or `send-failed`. It never logs the token or the payload.

### 6a. Control socket and mute (`voice.py`, `control.py`)

**Threat:** Anything that can turn voice on or unmute the mic can start listening to the room, or keep the mic from going quiet when the user expects it to. Claude itself is one such actor, on any turn: injected content (a document, a web page, or a spoken turn) could steer it to run `eridian unmute`, or to call the socket directly.

**Mitigations:**
- **Socket:** `<home>/control.sock` (`paths.control_socket_path()`), 0600 in the 0700 home. `attach` refuses to start without it. Requests are newline-delimited JSON with one response each: `status`, `voice_on`, `voice_off`, `mute`, `unmute`.
- **No speech path:** nothing on the control socket can create an utterance, open a turn or grant a confirm.
- **Real mute:** while muted, mic frames are dropped before the wake word, VAD and STT. A turn in progress when mute lands is discarded, and its transcript is never sent or printed (only `[muted: not sent]`). Each turn records a mute epoch and is sent only if no mute change happened since it opened.
- **Mute persistence:** mute is written to `voice-state.json` (version 2, 0600, atomic). Mute applies in memory before any write; unmute needs a successful write first. A corrupt or unreadable state file starts muted and makes every session off.
- **Mic device closed while muted:** `attach` closes the mic so the macOS mic indicator can go off (not yet verified on live hardware). On the echo-cancelled path it swaps the duplex stream for an output-only stream, so replies keep playing. If the mic fails to reopen on unmute, `attach` mutes again and prints `[mic failed to reopen; muted]`.
- **Connection limits:** `CONTROL_MAX_CONNECTIONS` (8) plus one reserved connection that serves only `mute`, `voice_off` and `status`; 4096-byte lines (`CONTROL_MAX_LINE_BYTES`); a 5 s per-line and idle deadline (`CONTROL_IDLE_S`); 30 s lifetime (`CONTROL_LIFETIME_S`); 64 requests per connection (`CONTROL_MAX_REQUESTS`). If the accept loop fails for good, `attach` mutes itself and removes the socket.
- **Requester check (layer 2):** `voice_on` and `unmute` are refused when the requester descends from Claude Code. The requester pid is read from the socket peer at accept (`LOCAL_PEERPID` / `SO_PEERCRED`), never from the request. `attach` re-reads the peer pid at check time, walks its ancestry with `/bin/ps`, and refuses if any process is `claude` or `claude.exe`, or has `@anthropic-ai/claude-code` or `/claude/versions/` in its path or arguments. It fails closed: an unreadable ancestry is refused. Replies: `refused: requested from a Claude session` and `refused: could not verify the requester`.
- **Client checks:** the `eridian` client refuses a control socket that isn't a socket or isn't owned by you, checks the server's uid (`LOCAL_PEERCRED` / `SO_PEERCRED`), and has a total deadline per request.

**Residuals:**
- Layer 2 is best effort. A process deliberately detached and reparented to launchd (pid 1) loses its Claude ancestor and escapes the check.
- The socket is protected by same-user ownership, not by secrecy. Any process running as you can still reach it from a plain terminal.
- `! eridian unmute` typed into a Claude prompt is refused by design. Use a plain terminal, or the menu-bar app once it ships (planned as a separate PR).
- The macOS mic indicator behaviour and the stall and reopen messages have not been tested live. Not yet verified on live hardware.

### 7. Claude Code Channel MCP Server (`mcp_channel.py`)

**Threat:** The channel is spawned as a stdio MCP server by Claude Code and receives voice transcripts from `eridian attach`. It is bidirectional: Claude can also call the `reply` tool to speak answers. All input is unverified spoken text.

**Mitigations:**
- **No permission relay:** the channel does not forward tool approvals to the voice process. Claude decides which tools to call in its running session, using all available context and its instructions.
- **No module shadowing:** Claude Code starts the channel as `python -P -m eridian.cli channel`. `-P` keeps the session's working directory off `sys.path`, so a hostile checkout can't shadow eridian's modules.
- **Input/output limits:** `reply` text over 4000 characters is rejected. Inbound utterances have `<` and `>` stripped before they are sent to the session, so a transcript can't close or forge the `<channel>` tag it arrives in.
- **stdout is JSON-RPC only:** logs go to a 0600 `channel.log` and never include transcripts or reply text.
- **No follow-up window after confirm:** `attach` opens the no-wake-word follow-up window only after the first `answer` reply to a wake-word turn. After an `ack` or a `confirm`, the user must say the wake word again, so ambient audio can't answer a confirmation question.
- **Confirmation is now a hard control:** see "Voice-turn guard (`voice_guard.py`)" below.

### Acoustic Self-Screen

**Threat:** the wake detector stays live while eridian speaks. If eridian's own speech fires it, the listener opens a `via: "wake"` turn, the only kind that can grant a voice confirmation. A (possibly prompt-injected) reply can carry a homophone the text scrub misses: "hey jarviss", "djarvis", "jarvice", "jarvus" and "charvis" pass it, and spoken by Kokoro they fired the detector on 4/4 voices tested (`say` with Evan did not).

**Mechanism:**
- **Text-level scrub:** `speaker.build_say_argv()` and `speaker.espeak_phonemes()` both start with `speaker.prepare_speech()`: NUL and lone-surrogate strip, NFKC (so fullwidth brackets and letters can't slip past), then `spoken.sanitize_for_tts()` (strips invisible characters, removes every `[` and `]` because both `say` and espeak-ng honor `[[…]]` inline commands, and replaces any form of "hey jarvis" or "jarvis" with "the wake word"), then a 1000-character cap (`MAX_SPEECH_CHARS`). This keeps the literal wake phrase from being spoken; homophones are left to the acoustic screen.
- **Acoustic self-screen:** `screen.ScreenedSink` wraps a `player.PlaybackSink` and runs the detector on the last 2 s of approved audio (`HISTORY_S`), the modeled silence gap, the current chunk, and 1 s of lookahead silence (`LOOKAHEAD_S`). The detector fires 50–250 ms after a phrase ends, hence the lookahead. If it fires, the chunk is dropped and `[skipped speech that sounded like the wake word]` is printed.
- **Cutoff and calibration (screen 0.8 vs live 0.97, 2026-10-02):** 0/200 ordinary sentences blocked; 66/80 homophone attack clips blocked with the phrase at the chunk end; every clip that played peaked below 0.8, so none would trip the live 0.97 detector. The detector fails closed on error and keeps history across a barge-in `clear()`.
- **Security review:** the split attack, 888 cut points and noise-prefix scenarios found 0 bypasses.
- See `screen.py`.

### Eridian Never Speaks Its Own Wake Word

**Threat:** If eridian speaks the phrase "hey jarvis" in a reply, it could trigger the wake word detector and capture unintended speech. With talk-over, eridian's own speech leaking through imperfect echo cancellation could also become a talk-over turn; the provenance control in §7a means such a turn can never confirm.

**Mitigation:** `speaker.build_say_argv()` calls `spoken.sanitize_for_tts()`, which strips invisible characters, removes `[` and `]` (macOS `say` uses `[[…]]` for inline commands), and replaces any text matching "hey jarvis", "jarvis", or obfuscated forms with "the wake word". eridian's instructions also explicitly tell Claude never to speak the wake phrase. The sanitization is still necessary with AEC: it ensures the wake-word detector only fires on user speech, not eridian's own playback, even through imperfect echo cancellation.

### 7a. Voice-turn guard (`voice_guard.py`)

**Threat:** Voice turns are unverified spoken input. A stray wake word or misheard phrase could trigger destructive or outward-facing actions (delete or overwrite files, commit or push, call an MCP tool that changes remote state). With talk-over, turns (speech during playback without the wake word) widen the unverified input surface.

**Mechanism:**
- **Hooks:** Six hooks (`SessionStart`, `UserPromptSubmit`, `Stop`, `SubagentStart`, `SubagentStop`, `PreToolUse`) are injected into the session via `--settings` inline JSON. Each runs `python -P -m eridian.voice_guard <event> --home <ERIDIAN_HOME> --session <token>`. Events are: `sessionstart`, `userpromptsubmit`, `stop`, `subagentstart`, `subagentstop`, `pretooluse`. `pretooluse` can block a tool call, and `userpromptsubmit` can drop a voice turn it cannot record. `sessionstart`, `stop`, `subagentstart` and `subagentstop` always exit 0, because exit 2 on `Stop` or `SubagentStop` would keep Claude or the subagent running.
- **Subagents:** Subagents can run in the background, so a voice turn can end (`Stop`) while a subagent it started keeps working. Each subagent is recorded by its `agent_id` at `SubagentStart`, as voice-born if a voice turn is active or any voice-born subagent is still recorded (until its `SubagentStop`). That rule also catches a nested child started after the user typed. A voice-born subagent stays gated until `SubagentStop`, even after the user types. Its gated calls are allowed only if it started while a grant was valid (inherited for 300 s, revoked by a typed prompt) or if a new grant is valid. In a session that has had a voice turn, a subagent whose `agent_id` is unknown or invalid is treated as voice-born; this covers a missed `SubagentStart`, an early `SubagentStop` and an evicted record. Only a subagent positively recorded as typed-origin runs ungated.
- **Arming:** The channel (`mcp_channel.py`) refuses a voice turn in three cases: the session token is missing, the `SessionStart` hook hasn't armed state for this launch, or recording the turn failed. When refused, the channel speaks "Voice turns are off:" instead of delivering the turn. This fails safe: if hooks don't load, voice is refused rather than unguarded.
- **Provenance:** Claude Code fires `UserPromptSubmit` for channel events. Its prompt is `<channel source="eridian" input="voice">…</channel>`, so a prompt containing `<channel` counts as a voice turn. A prompt starting with `<` that is not a channel event (an injected envelope such as a background-task notification) changes nothing. Any other non-empty prompt counts as typed. A voice turn stays gated until its `Stop`, even if the user types mid-turn; after that, a typed prompt ungates. If a voice turn is interrupted (Esc, no `Stop`), the session stays gated until a later turn completes.
- **Gating:** During an active voice turn, gated tool calls require confirmation. Read-only MCP tier: `mcp__cast-record__cast_decisions`, `mcp__cast-record__cast_incidents`, `mcp__cast-record__cast_cost`, `mcp__cast-record__cast_sessions`, `mcp__cast-record__cast_ask` run ungated as an exact-name allowlist. The allowlist is in code (`READ_ONLY_MCP_TOOLS` in `voice_guard.py`), so a typed turn cannot widen the voice gate by editing a config file. It has no effect unless an MCP server named `cast-record` is registered; `cast-record` is the maintainer's own local tooling, not part of eridian. Never gated: `Read Grep Glob LS WebSearch TodoWrite Task Agent AskUserQuestion ToolSearch Skill EnterPlanMode ExitPlanMode BashOutput TaskOutput mcp__eridian__reply`. `Bash` goes through a fail-closed allowlist (read commands, `cd` to plain relative paths, `find` with read-only predicates only, read-only git, test runners with restricted args); any command using shell expansion or special characters, a redirect other than to `/dev/null` or an fd dup, a heredoc, an env-var assignment or a non-ASCII character is gated, and so is every other tool.
- **Voice-control commands (layer 1):** on every turn (typed or spoken, any grant, any subagent), `PreToolUse` refuses Bash that could turn voice on or unmute, including any Bash command whose text names `control.sock` (`CONTROL_DENY_REASON`). Turning voice off stays allowed. This is a string-matching deterrent, not a boundary: indirection can get past it, so the socket's own requester check (SECURITY.md §6a, layer 2) is the backstop. Searching for the socket name with the Grep and Read tools is allowed.
- **Confirm flow with a safe phrase** (when `<home>/safe_phrase.json` exists at session start; see "Safe phrase" below): Claude's confirm reply needs no phrase. The next voice message grants only if it is a wake turn, within 300 s, at most two words longer than the phrase, and contains the phrase. Every voice message still uses up the challenge.
- **Confirm flow without one:** Claude's `reply` with `kind="confirm"` must contain `confirm <word1> <word2>`. Both words ≥3 letters, uncommon, different, not used before in the session. Otherwise the reply tool returns an error. The channel records the challenge; one attempt, valid 300 s. If the next voice message contains `confirm <word1> <word2>` (case and punctuation ignored), that creates a grant. Any other message cancels the challenge. On a successful confirm, gated calls are allowed for the rest of that turn (until `Stop`, the next voice message, a typed prompt, or 300 s).
- **Self-wake guard (`SELF_WAKE_GUARD_S = 1.0`):** A wake word heard while eridian is speaking, or within `SELF_WAKE_GUARD_S` s after, gets `via: "wake_barge_in"`. It still interrupts, but never grants (only `via == "wake"` grants). `ERIDIAN_SELF_WAKE_GUARD_S` (floor 1.0, cap 10.0) is for high-latency outputs; AirPlay is about 2 s, so set `3` to cover it. A downgraded turn uses up the confirm attempt; Claude re-asks. Restart both `attach` and `session` after upgrading.

#### Hard Control: Voice Turn Provenance (2026-10-01)
Every utterance on the voice socket now carries `"via": "wake"|"follow_up"|"talk_over"`, which opened the turn:
- The channel passes `can_grant = (via == "wake")` to `voice_guard.record_voice()`.
- Only `can_grant=True` plus the confirm phrase grant; talk-over and follow-up turns cannot confirm.
- Gated tool calls in a non-grantable turn fail closed.

**Verified end to end through the real code (2026-10-01; not yet with live speech):** a real `ChannelServer` over a real unix socket, the session armed by the real `SessionStart` hook, a confirm challenge recorded through the real `reply` tool, then the real `PreToolUse` hook command run with a `Write` payload after each utterance:
- `via` `"talk_over"`, `"follow_up"`, missing, `["wake"]`, `"WAKE"` or `"wake "` → no grant, challenge consumed, `Write` denied.
- `via` `"wake"` with a fresh challenge → grant, and the guard allows `Write` (your normal permission prompts still apply).
- Any voice turn uses up the challenge's one attempt, so an ambient or echoed talk-over turn can only cancel a pending confirmation, never satisfy it.
- An `eridian attach` started before this change sends no `via`, so confirmations fail closed until both `attach` and `session` are restarted.

**Residual risks (talk-over, stated plainly):**
- **Before the fix (2026-10-01):** a talk-over turn could capture eridian's own spoken "confirm <w1> <w2>" through imperfect AEC and grant it. A talk-over stream already listening when a confirm reply was spoken could capture the reply. A follow-up window could open after a confirm reply. All three are now blocked by the hard control.
- **Imperfect AEC:** ambient speech during playback can still open a talk-over turn, but it is gated (cannot confirm or run Write-class tools). AEC was live-tested once (2026-10-02, MacBook Pro speakers and mic, one room); other rooms, external speakers and high volumes are untested. `--no-aec` turns talk-over off.

**Fail-closed:**
- `pretooluse` errors exit 2, and the hook command ends in `|| exit 2`, so a missing or crashing interpreter blocks the call instead of allowing it.
- `userpromptsubmit` errors on voice prompts exit 2, which drops that voice turn. Typed prompts are never blocked.
- `pretooluse` returns `permissionDecision: "deny"` with a reason starting `eridian voice guard:`. Claude is told to ask with `kind="confirm"`. Allowing a call means exit 0 with no output, so the user's normal permission prompts still apply. The guard never auto-approves.
- `pretooluse` timeout is 10 s. Claude Code lets a call through on timeout (documented limitation).

#### Confirm Read-Back in Eridian's Own Voice

**Threat:** Claude writes the spoken confirm prompt, so content injected from a document or message could make a harmful action sound harmless.

**Mitigation:**
- When `voice_guard` denies a call, it records a short description (tool kind, program and file names, location classes, host names; no argument values or free text) in session state (`~/.eridian/sessions/<token>.json`) with a timestamp, after the deny is written and best-effort, so the deny always stands. The last 3 entries are kept (`MAX_BLOCKED`); each eviction is counted in `blocked_more` and read back as ", and N more blocked calls", so padding the list with harmless calls can't hide an earlier one silently. Both are cleared on each new voice or typed turn, and entries older than 300 s are ignored.
- On a confirm reply, the channel attaches `voice_guard.read_back()` as a `check` field on the reply line. `attach` honours `check` only on a confirm and only up to `MAX_CHECK_CHARS` = 600 chars, speaks it before Claude's words (since the safe-phrase follow-up; it was after them in the first read-back build) in eridian's own voice (a second TTS voice Claude's replies never use: Kokoro `af_heart` or `say` `Samantha`), and prints `eridian: <text>`. Forms: `eridian blocked: <a>; and <b>`, `eridian has not blocked anything yet.`, or `eridian could not read back what it blocked.`
- **Vocabulary:** only fixed words; names at most 2 words and 20 chars, otherwise `a long name`; location classes from the hook payload's `cwd`: `in this project`, `in the project git or Claude settings`, `in your home folder`, `in your home settings files`, `outside your home folder`, `at a relative path`, `somewhere unknown`. Hosts: the last two labels (`a site under X`), `an international address` (punycode or non-ASCII), or `an IP address`. URLs and scp/ssh remotes in arguments read `to …` / `to a remote computer …`. Never spoken or stored: argument values, MCP argument keys or values, commit messages, free text. `with N arguments` marks omissions. Redirects read `overwriting` vs `appending to`. Complex shell commands (backticks, `$(`, heredocs) read `a complex shell command starting with <prog>`. Wrappers (sudo, env, xargs, timeout, `sh -c`, `find -exec/-delete`) name the program they run. Up to 3 commands of a pipeline or list are named; the rest are counted (`, and N more commands`). Examples: `the shell command rm rf on files in your home folder`; `the shell commands cat on files in your home settings files, then curl d to evil.com with other arguments`; `write a file named x.py in this project`; `the neon tool run sql with 2 arguments`; `fetch a web page from a site under python.org`.
- **Residual risks:**
  - **The grant is turn-wide (the maintainer's decision).** A spoken confirm still unlocks every gated call for the rest of the turn (up to 300 s), not only the calls read back. So a prompt-injected Claude can get a harmless-looking call blocked and confirmed, then run other gated calls inside the grant. The read-back reports honestly what was blocked; it does not bind the grant. A possible follow-up is a spoken post-grant audit ("eridian allowed: …").
  - Short names (at most 2 words and 20 chars) are still Claude-chosen, so a script named `harmless_readonly` is read back as "harmless readonly". Longer names become `a long name`.
  - The own voice goes through the same acoustic self-screen as the reply voice. Scratch measurement (2026-10-03; 18 homophone attacks and 20 read-back-style sentences through `ScreenedSink` with the real wake detector): `af_heart` blocked 14/18 homophones and 0/20 ordinary sentences, the same as `am_adam`. The read-back carries no free text, so the exposure is small.
  - An MCP server registered as `cast-record` (e.g. in a project's `.mcp.json`) would receive the read-only ungated tier, because the hook sees only the tool name. Accepted, because an approved MCP server runs arbitrary code at startup anyway. The cast-record tools add a data source (agent output, incidents, sessions) that an ungated `WebSearch` could carry out, but not a new path: `Read` and `WebSearch` were already ungated.

#### Safe phrase

Set with `eridian safe-phrase` (hidden prompt, entered twice, refuses to run without a terminal; 1–3 words, at least 2 words or 8 letters).
- **Storage:** only a salted scrypt hash (n=2^14, r=8, p=1) plus the phrase's word count and length, in `<home>/safe_phrase.json` (0600). It is read with `O_NOFOLLOW`, and a file with another owner, group/other mode bits or any other format never grants.
- **Claude never sees it.** The channel passes each utterance raw to the guard (so it can match) and gives Claude the text with the phrase replaced by `[safe phrase]`. attach shows `[safe phrase]` on screen too. Only exact matches are replaced, so a misheard or partly wrong attempt reaches Claude as spoken.
- **No reads of eridian's folder on a voice turn.** The guard denies `Read`, `Grep`, `Glob` and `LS` on `<home>`, inside it, or on a folder above it, and shell commands that name `.eridian`, `safe_phrase` or the home path, have a path argument there, or run from a folder at or above it. This holds even after a confirm. `eridian session` also adds the Claude Code rule `Read(//<home>/safe_phrase.json)` to `permissions.deny`, which covers typed turns too.
- **Too-early confirms:** while a confirm is pending (up to 300 s), attach doesn't send a wake turn heard during or just after its own speech. Such a turn could never grant, and now it doesn't use up the challenge either.
- **Residual risks:**
  - **Replay.** A fixed phrase works for anyone who has heard it: a bystander, a call, a recording. They still need the wake word in silence, a pending confirm (which a prompt-injected Claude can create) and the 300 s window, and you hear the read-back first. Don't say it on calls or near media, and pick a phrase you never say otherwise.
  - **Offline guessing.** Someone who gets the file can still guess a short phrase offline. scrypt makes each guess cost about 25 ms. The stored length narrows the search a little.
  - Paths are matched by file identity (`os.stat` device and inode), so a symlink, a `//` prefix, a case variant or the `/System/Volumes/Data` alias of `<home>` is still caught. So are renamed symlinks and hard links to files directly in `<home>` or `sessions/` (up to 256 entries each; `safe_phrase.json` always), `..` taken after a symlink, and bare shell arguments. A link planted during the same tool call can't be seen in advance. After a confirm, though, shell access is best-effort: `$` expansion, globs or `python -c` can still reach files. Before a confirm those forms are already gated.

**Residual risks (stated plainly):**
- `WebSearch` is ungated, so a query could carry data out under prompt injection.
- `Read`/`Grep`/`Glob` and `cat`/`grep` can read secrets in the working directory.
- A grant covers the rest of the turn, not one specific command.
- Test runners execute repo code (`conftest.py`, `Makefile`, `package.json` scripts, `build.rs`).
- Git config programs (`core.fsmonitor`, `diff.external`) need a prior write to `.git/config`.
- `Task`/`Agent` are ungated. That is safe because a subagent's own tool calls pass through the same `PreToolUse` hook (verified live).
- Your shell aliases and functions are trusted, because Claude Code sources your shell snapshot. They can change what an allowlisted name runs: a user alias or wrapper can point an allowlisted name such as `find` or `grep` at a different program. The allowlist was probed against such wrappers, and `find` takes only an allowlist of read-only predicates for that reason (some `find` implementations add actions such as `-rm`).
- The subagent rules assume Claude Code delivers `SubagentStart` before the voice turn's `Stop`. The live test was consistent with that, but it is not documented. If `SubagentStart` arrived after a typed prompt with no other voice-born subagent recorded, that subagent would be recorded typed-origin.
- A voice-born subagent that crashes without `SubagentStop` keeps new subagents gated until `eridian session` restarts or the user speaks a confirm phrase. That is the safe direction.
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

**Live test (2026-10-02, real speech, speakers on):**
- A full spoken reply played without opening a talk-over turn on eridian's own voice.
- Talking over a reply stopped it. The first build then dropped the whole turn as echo, losing the user's words; `strip_echo` now removes only the repeated words.
- Talking over a confirm prompt stopped it and the phrase was never heard; talk-over is now off during confirm prompts.
- A wake-word turn "Confirm Lemon Canyon." granted the gated delete (`pretooluse granted Bash`) and the file was removed. An earlier unanswered challenge was used up by the next voice turn, as designed.
- STT turned "hey jarvis" into "A Jarvis.", which reached Claude; the wake strip now handles it.
- Re-test after the fixes: reported working by the maintainer (per-step output not captured).

### 7b. Typed-Chat Persona Hook (`persona.py`)

**Threat:** A SessionStart hook runs at the start of every session it is registered for, and its output is added to Claude's context. Text injected there shapes every such session.

**Mechanism:**
- You register `python -P -m eridian.persona sessionstart --under DIR` in your own Claude Code settings. eridian does not install it.
- When the session's working directory, with symlinks resolved, is inside `DIR` (both paths must be absolute), it prints one JSON envelope whose `additionalContext` is the fixed `TYPED_ROCKY_BLOCK` text. Otherwise it prints nothing.
- It cannot block or grant anything. Once its arguments parse it always exits 0, and it returns no permission decision. Argparse errors exit 2 before that point.

**Residual risks:**
- It adds fixed text to Claude's context. It reads only the `cwd` field of the hook payload.
- It runs whatever `persona.py` is in the checkout the hook command points at. Point the hook only at a checkout you trust, and remember that editing that checkout changes what runs at every session start under `DIR`.

See `persona.py`.

### 8. Local Files

**Threat:** Persistent state files could be read by other processes, forged, or used to track behavior. Temporary files could be read or recovered.

**Mitigations:**
- **Chat session state:** `~/.eridian/chat_session` (stores the current session ID) is written atomically, opened with mode 0600 (user-read/write only), and validated on load with UUID format checks.
- **Attach log:** `~/.eridian/attach.log` (0600, opened with `O_NOFOLLOW`) holds the stdout and stderr of the background `eridian attach` that `eridian launch` starts. It is not decisions-only: it contains `you: <transcript>` lines (`you [<via>]: <transcript>` for non-wake turns), with the safe phrase replaced by `[safe phrase]`. Before the background attach starts, a log of 1 MB (1,000,000 bytes) or more is renamed to `attach.log.1`, replacing the previous `attach.log.1`, so one older generation is kept. Rotation runs only when a new background attach starts, so a long-running attach can grow the log past 1 MB, and a symlinked attach.log is never rotated (`lstat`) or opened (`O_NOFOLLOW`). Treat both files as containing spoken content. See `cli.py` (`_ATTACH_LOG_ROTATE_BYTES`).
- **Brain logs:** `~/.eridian/logs/brain.log` is opened with O_NOFOLLOW (refusing symlinks) and mode 0600. Log rotation occurs at 1 MB. See `brain.py` `_open_log()`.
- **Voice-turn session state:** `~/.eridian/sessions/<token>.json` (file 0600, directory 0700) stores per-launch state for the voice guard, plus blocked-call descriptions for the read-back (tool kinds, program and file names, location classes and host names, but no argument values or free text). Atomic writes, symlinks refused, lock file `<token>.lock`.
- **Safe phrase:** `~/.eridian/safe_phrase.json` (0600) holds a salted scrypt hash of your confirm phrase, its word count and length, never the phrase.
- **Voice-turn guard log:** `~/.eridian/logs/guard.log` (0600) records decisions only, never command text or prompt text.
- **Rendered TTS:** When AEC is on, TTS audio is rendered into `tempfile.mkdtemp(prefix="eridian-tts-")` (mode 0700, user-only). This applies to the `say` renderer only (Kokoro renders in memory and writes no files). Files are `.wav`, validated as 1 channel, 16-bit, 16 kHz, and named `<counter>.wav`, unlinked in a `finally` block immediately after reading into the playback buffer. The directory is removed when `RenderedSpeechQueue` closes. No audio is written anywhere else except the transient playback buffer in RAM.
- No user secrets or API keys are stored in these files; Claude Code's login token is used to authenticate (inherited from the user's session).

### 9. Network

**Threat:** If eridian sends audio or other sensitive data over the network, it could be intercepted or logged.

**Mitigation:** Only the headless `claude` child talks to Anthropic's API. Audio never leaves your Mac—it is processed locally by wake-word detection, VAD, STT, and TTS. Only the transcribed text is sent to Claude, along with any working-directory files Claude reads and its WebSearch queries.

**Telemetry (onnxruntime):** onnxruntime 1.30's macOS wheel embeds Microsoft's 1DS telemetry client (endpoint `mobile.events.data.microsoft.com`). `src/eridian/kokoro.py` FORCES `ORT_DISABLE_TELEMETRY=1` before onnxruntime is imported and calls `onnxruntime.disable_telemetry_events()`. With the variable set, ORT no longer initializes its telemetry device ID.

**Model integrity (Kokoro):** the model, tokenizer and voice files come from a pinned Hugging Face revision and are loaded from the local cache only (`local_files_only=True`). Each is also checked against a SHA-256 pinned in `src/eridian/kokoro.py` every time it loads; a mismatch, a corrupt model or a malformed tokenizer raises `KokoroUnavailable`, and eridian falls back to `say`.

See [`docs/local-first.md`](docs/local-first.md) for full detail on what leaves your Mac and when. Run `bash scripts/verify-offline.sh` to verify wake word, VAD, STT, TTS, and Kokoro run entirely offline inside a macOS network sandbox.

---

**Last updated:** 2026-11-09

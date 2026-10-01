# Security Policy

## Reporting

undertone is a personal, single-user macOS tool. Private vulnerability reports can be submitted via GitHub private vulnerability reporting or a Security Advisory on [`ek33450505/undertone`](https://github.com/ek33450505/undertone).

## Threat Model and Controls

undertone is designed for local-first, voice-driven interaction with Claude. The following threat model and mitigations document what is built and verified in the code.

### 1. Open Microphone

**Threat:** While `undertone chat` or `undertone listen` runs, the microphone stream is open and every 10 ms frame goes through the wake-word detector in memory. Any audible sound nearby, such as a video, a call or someone speaking, can trigger the wake word "hey jarvis". After a Claude reply ends, there is a 6-second follow-up window during which no wake word is needed to issue a follow-up command.

**Mitigations:**
- Microphone access is granted per app by macOS TCC (Transparency, Consent, and Control). undertone uses the permission of the terminal app you run it in.
- Audio is buffered in memory only after the wake word, or during the follow-up window. No audio is written to disk, and each turn's buffer is discarded once it has been transcribed.
- On-device STT turns the buffered audio into text. Only that text reaches the `claude` child; the audio is never stored or uploaded.
- The open microphone and short follow-up window are inherent to a voice interface. STT can mishear, so the Claude child is read-only (see control #2).
- **User controls:** `--no-beep` silences the wake and follow-up cue sounds (cosmetic only; listening is unchanged); `--follow-up 0` disables the 6-second window; Ctrl-C exits immediately.

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

**Threat:** Claude's reply text is passed to the macOS `say` command for text-to-speech. If Claude outputs something like `-o /path/to/file`, `say` could interpret it as a flag and write to disk.

**Mitigation:** `speaker.py` constructs the `say` argv by placing `--` before the text. This tells `say` to stop parsing flags and treat everything after as positional arguments (the text). The `--` is inserted in exactly one place: `speaker.build_say_argv()`. Additionally, any NUL bytes in the text are stripped. See `speaker.py`.

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
- **Confirmation is soft control:** it is enforced by Claude's instructions and choice of the reply tool, not the system. The session's full tools and CAST hooks remain available.

### Undertone Never Speaks Its Own Wake Word

**Threat:** If undertone speaks the phrase "hey jarvis" in a reply, it could trigger the wake word detector and capture unintended speech.

**Mitigation:** `speaker.build_say_argv()` calls `spoken.sanitize_for_tts()`, which strips invisible characters, removes `[` and `]` (macOS `say` uses `[[…]]` for inline commands), and replaces any text matching "hey jarvis", "jarvis", or obfuscated forms with "the wake word". undertone's instructions also explicitly tell Claude never to speak the wake phrase.

### 8. Local Files

**Threat:** Persistent state files could be read by other processes, forged, or used to track behavior.

**Mitigations:**
- **Chat session state:** `~/.undertone/chat_session` (stores the current session ID) is written atomically, opened with mode 0600 (user-read/write only), and validated on load with UUID format checks.
- **Brain logs:** `~/.undertone/logs/brain.log` is opened with O_NOFOLLOW (refusing symlinks) and mode 0600. Log rotation occurs at 1 MB. See `brain.py` `_open_log()`.
- No user secrets or API keys are stored in these files; Claude Code's login token is used to authenticate (inherited from the user's session).

### 9. Network

**Threat:** If undertone sends audio or other sensitive data over the network, it could be intercepted or logged.

**Mitigation:** Only the headless `claude` child talks to Anthropic's API. Audio never leaves your Mac—it is processed locally by wake-word detection, VAD, and STT. Only the transcribed text is sent to Claude, along with any working-directory files Claude reads and its WebSearch queries.

See [`docs/local-first.md`](docs/local-first.md) for full detail on what leaves your Mac and when. Run `bash scripts/verify-offline.sh` to verify wake word, VAD, STT, and TTS run entirely offline inside a macOS network sandbox.

---

**Last updated:** 2026-10-01

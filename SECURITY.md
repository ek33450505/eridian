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

**Mitigation:** `chat.py` strips C0 (0x00–0x1F) and C1 (0x80–0x9F) control characters from all model output before printing, with the exception of newline (`\n`, 0x0A) and tab (`\t`, 0x09). See `chat.py` `_sanitize()`, which is applied to everything `ChatSession` prints.

### 6. Local Speaker Socket (`speaker.py`, Path A only)

**Threat:** The speaker daemon listens on a Unix domain socket. If the socket is world-writable or in a shared directory, any process could send commands to the speaker.

**Mitigations:**
- The socket is created at `~/.undertone/speaker.sock`, owned by the user, with mode 0600 inside a mode 0700 parent directory (`~/.undertone`).
- The daemon refuses to start if a non-socket (e.g., a regular file) already exists at the socket path.
- A stale socket is unlinked only after a failed ping attempt, reducing but not eliminating a small TOCTOU window.
- Each connection has a 1-second overall deadline (guards against slow-loris attacks) and a 64 KB message cap.
- Malformed JSON in a message receives an error response; a crash in one connection never kills the daemon (see `speaker.py` `_handle_connection()` and `serve()`).

**Known low-severity TOCTOU windows:** bind-then-chmod and mkdir-then-chmod operations occur in sequence; an attacker with write access to `~/.undertone` (0700, so in practice the same user) could create a symlink between these steps. This is accepted for a single-user tool.

### 7. Claude Code Hooks (Path A only)

**Threat:** The `Stop` and `UserPromptSubmit` hooks run on every turn of every Claude Code session they are registered in. A hook that blocked or spoke everywhere would be disruptive.

**Mitigations:**
- The Stop hook is registered as async, so it never blocks the session. It fails silently if the speaker daemon is unreachable.
- It speaks only for opted-in sessions (`UNDERTONE_SPEAK=1`, or every session after `undertone on`); otherwise it exits without speaking.
- It never speaks on `stop_hook_active` turns (hook-forced continuations).
- Hooks only send text to the local speaker daemon; they never act on your code.
- User hooks remain isolated from the `undertone chat` voice child (see control #2; the child does not load CAST/user MCP servers or hooks).

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

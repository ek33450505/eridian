# Changelog

All notable changes to eridian (named undertone before 2026-11-05) are documented here, in the style of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). No release has been tagged yet; `pyproject.toml` is at 0.1.0.

## [Unreleased]

### 2026-11-11 — Work-repo persona and journal voice

**Added**
- `--work-under DIR`: sessions under that directory get the persona plus a fixed `WORK_CLAUSE`, which keeps the persona name and speech out of every work artifact (commits, PRs, tickets, messages, docs, handoffs, journal entries). `--under` wins when both match.
- The persona may write Claude's journal in its own voice and add one line at the top or bottom of a handoff file. Everything else on disk stays plain, and both exceptions are off in work repositories.

**Changed**
- SECURITY.md no longer points to GitHub private vulnerability reporting, which is not enabled.

### 2026-11-11 — Voice archived; refocus on typed Rocky

**Removed**
- The voice interface (wake word, speech in and out, `voice_guard`), wake-word training, the offline-check scripts and the speech persona. All of it is preserved at tag `voice-archive-v1`.

**Changed**
- The package now has no runtime dependencies.
- There is no `eridian` console script; the SessionStart hook runs as `python -m eridian.persona`.

### 2026-11-10 — No open mic while a confirm is pending

**Fixed**
- An `answer` still playing when Claude sent a `confirm` could open the no-wake-word follow-up window right after the confirm prompt; anything heard there was sent as a `follow_up` turn and used up the confirm challenge. It could never grant. The window now never opens while a confirm is pending (checked when the reply is credited and again when the window would open).

**Changed**
- While a confirm is pending, `follow_up` and `talk_over` turns are not sent; eridian says "A confirmation is waiting. Say the wake word, then the phrase." in its own voice. Plain wake-word turns are sent as before. Previously a non-echo talk-over turn was sent during a pending confirm.

### 2026-11-09 — Pre-public cleanup

**Changed**
- `~/.eridian/attach.log` (which holds spoken transcripts) is rotated to `attach.log.1` when it is 1 MB or more at the next background launch, so at most one older generation is kept. A failed rotation prints a warning and the launch continues.
- Docs no longer contain personal paths, private project names or internal planning notes. The phase plans and research notes moved out of the repo; README has a short Roadmap instead. README gained install-from-scratch steps, and SECURITY.md now covers the attach log and the persona hook.
- `pyproject.toml` gained `readme`, classifiers and the repository URL.
- The training-model equivalence test reads its model directory from `MWW_ROOT` and skips when it is unset.

### 2026-11-09 — Rocky persona for the typed chat

**Added**
- `python -P -m eridian.persona sessionstart --under DIR`: a Claude Code SessionStart hook that adds `TYPED_ROCKY_BLOCK` to the session's context when the session's cwd is inside `DIR`, and prints nothing otherwise. It always exits 0 once its arguments parse, so it can't block a session. Register it in your Claude Code settings yourself; eridian doesn't install it.
- `TYPED_ROCKY_BLOCK`: full Rocky speech in conversational turns, the same character with standard grammar in dense technical turns. Code, paths, numbers, commands, concerns, approval questions and anything written to disk or sent outside the chat stay plain English. The spoken `ROCKY_BLOCK` and `ERIDIAN_PERSONA` are unchanged.

### 2026-11-06 — Per-session voice on/off and real mute

Live test: pending. Nothing in this entry has been run live yet.

**Breaking**
- A newer session no longer detaches the older one; every session stays attached, and wake turns go to the newest one with voice on. Use `eridian voice off` to silence the others.
- `eridian attach` refuses to start if it can't create `<home>/control.sock`.
- `voice-state.json` is new (version 2). Older builds never read it; there's nothing to migrate.

**Added**
- `eridian voice on|off [--session ID]` turns voice on or off for one Claude session; `eridian voice status` lists sessions with a `*` on the current wake-turn target. `--session` takes the 12-hex id or the pid from `voice status`. Without it, the session the command runs from is found by process ancestry, and an ambiguous match asks for `--session`.
- `eridian mute` and `eridian unmute`. Unmute confirms that the mic really reopened.
- `<home>/control.sock` (0600): a newline-delimited JSON socket for `status`, `voice_on`, `voice_off`, `mute` and `unmute`. `attach` refuses to start without it. Nothing on it can create an utterance, open a turn or grant a confirm.
- While muted, `attach` closes the mic device so the macOS mic indicator can go off (not yet verified live). Replies keep playing. `attach` starts with the mic closed when the saved state is muted.

**Changed**
- Multi-attach: every session that sends `hello` stays connected. Wake-word turns go to the most recently attached session with voice on. A new `hello` no longer detaches the others; `{"type":"detached"}` is sent only when a `hello` carries the same session token as a live connection (a reconnect).
- Sessions start with voice on. An off session gets no utterances, its replies aren't spoken, and its announcements are ignored. Turning off the session whose speech is playing stops that speech.
- Per-session on/off is saved in `<home>/voice-state.json` (keys are SHA-256 of the session token, never the raw token; mode 0600; at most `MAX_OFF_ENTRIES` entries). Version 2 of that file also stores mute; version 1 loads as unmuted. An unreadable or corrupt state file makes every session off, and starts muted.
- Mute is real: while muted, mic frames are dropped before the wake word, VAD and STT. A turn in progress when mute lands is discarded, and its transcript is never sent or printed (only `[muted: not sent]`).
- When sessions exist but none is on, `attach` says `ALL_OFF_SPEECH`. A connection that hasn't sent a valid `hello` within `NO_HELLO_IDLE_S` (5 s) is closed. A `hello` without a valid token is listed but can't get voice.
- If the mic stalls, `attach` prints `[mic stalled; no input]` once and `[mic input back]` when input returns. If the mic fails to reopen on unmute, `attach` mutes again and prints `[mic failed to reopen; muted]`.

**Security**
- The control socket has limits: `CONTROL_MAX_CONNECTIONS` (8) plus one reserved connection that serves only `mute`, `voice_off` and `status`; 4096-byte lines; a 5 s per-line and idle deadline; 30 s lifetime; 64 requests per connection. If its accept loop fails for good, `attach` mutes itself and removes the socket.
- The `eridian` client refuses a socket that isn't owned by you, checks the server's uid, and has a total deadline per request.
- `voice_guard` (layer 1) refuses, on every turn, any Bash command that could turn voice on or unmute, including any command whose text names `control.sock`. It is a string-matching deterrent, not a hard boundary. Turning voice off stays allowed.
- `attach` (layer 2) refuses `voice_on` and `unmute` on the control socket when the requesting process descends from Claude Code (`refused: requested from a Claude session`). If the requester can't be verified, it fails closed (`refused: could not verify the requester`). A process deliberately reparented to launchd escapes this check.
- Side effect: `! eridian unmute` typed into a Claude prompt is refused. Use a plain terminal. The menu-bar app is planned as a separate PR.

### 2026-11-05 — Renamed from undertone to eridian; follow-up window fix

Entries below this one are kept as released, under the old name.

**Changed**
- The project was renamed from undertone to eridian: package, CLI, MCP server and docs. The `UNDERTONE_*` environment variables and the `~/.undertone` state folder are no longer read. `eridian migrate` moves the old state folder (refuses while attach runs; `--dry-run` to preview).

**Fixed**
- `eridian attach` no longer opens the no-wake-word follow-up window after an announcement. "Claude finished." fires after every session turn, so room conversation could chain turns into a session without the wake word (seen live 2026-11-05). The window now opens only after the first answer to a turn started by the wake word.
- `eridian chat` had the same chain: every reply opened the window, so a reply to a no-wake turn opened another. It now follows the same rule.

### 2026-11-04 — Echo text guard (live test: own voice heard as `you:`)

Live test (MacBook speakers, echo cancellation on): after Claude read out the weather, attach printed `you: Humidity is ninety-four percent.` and `you: Humidity's ninety three percent right now.`, which the maintainer never said. Claude then asked whether the microphone was hearing it. **Live test of the fix: pending.**

**Added**
- Echo text guard: a talk-over or follow-up turn (never a wake-word turn) whose transcript mostly repeats what undertone itself just said is dropped and printed as `you [talk_over]: … [dropped: echo of own speech]`. It covers replies, read-backs and announcements, compares numbers as digits ("ninety three" matches "93") and ignores a swapped word. It is checked only if the turn opened while undertone was speaking or within 3 s after, against speech from the last 60 s. It only drops turns; confirm rules are unchanged. A talk-over that had already stopped playback leaves the reply stopped.
- attach prints how each turn started, except a plain wake-word turn: `you [talk_over]: …`, `you [follow_up]: …` or `you [wake_barge_in]: …`.

**Fixed**
- The older talk-over echo filter compared words literally and only against the current reply, so a number heard as words (or a second talk-over after the first stopped playback) got through.

### 2026-10-03 — Confirm with your own safe phrase (Phase 8a follow-up)

Changes made after the Phase 8a live test. **Live test: pending.**

**Added**
- `undertone safe-phrase` sets your own fixed confirm phrase (1–3 words, at least 2 words or 8 letters). It reads the phrase twice with hidden input and refuses to run without an interactive terminal. `--clear` goes back to fresh two-word confirms, and `--status` says whether a phrase is set. Restart `undertone attach` and `undertone session` after changing it.
- With a phrase set, a voice confirm is the wake word in silence followed by your phrase. Claude asks with "say the wake word, then your safe phrase", and never knows the phrase: the channel passes the raw words to the guard but replaces them with `[safe phrase]` before Claude sees them, and attach shows `[safe phrase]` on screen.
- The phrase is stored only as an scrypt hash (n=2^14, r=8, p=1, salted) in `<home>/safe_phrase.json` (0600). A file with the wrong owner, wrong mode or any other format never grants.
- While a confirm is pending, a wake word heard during or within 1 s after undertone's own speech no longer reaches Claude and no longer uses up the confirm. attach prints `[too soon after I spoke, so not sent: …]` and says "That was too soon after I spoke. Wait a moment, then say the wake word again." in its own voice. The pending confirm expires with the guard's 300 s challenge.

**Changed**
- The read-back now plays **before** Claude's confirm prompt, so the last thing you hear is your cue to answer.
- The blocked-call list survives a voice turn that doesn't grant. It is cleared on a grant or a typed turn, so a re-asked confirm still reads back what was blocked.
- The channel instructions name the five read-only tools of the optional `cast-record` MCP server and tell Claude to prefer them over shell commands. In the live test, Claude had reached for `just` recipes, which the guard blocks.

**Security**
- On a voice turn the guard now refuses any read or search of undertone's own folder (`<home>`) or a folder above it, even after a confirm: `Read`, `Grep`, `Glob`, `LS`, and shell commands that name it or run from a folder at or above it. `undertone session` also adds a Claude Code deny rule for reading `safe_phrase.json`.
- Paths are matched by file identity (`os.stat` device and inode), not text. A re-check had bypassed a string-only first version with a leading `//`, a case variant (APFS is case-insensitive), the `/System/Volumes/Data` alias and a symlink. The gate also catches renamed symlinks and hard links to files in `<home>`, `..` taken after a symlink, and bare shell arguments such as `grep -r . somelink`. A project file that is itself named `safe_phrase.json` is denied on voice turns too.
- Security review: PASS WITH FINDINGS. Fixed: M1 (the hash file was readable on a voice turn, and plain SHA-256 is fast to guess), L2 (no owner/mode check), L3 (echoed prompt without a terminal), L4 (pending flag never expired). Accepted: M2, a fixed phrase can be replayed by anyone who has heard it. It still needs the wake word in silence, a pending confirm and the 300 s window, and you hear the read-back first. Don't say the phrase on calls or near recording devices.

### 2026-10-03 — Phase 8a: voice-safety tier for app tools

**Live test (2026-10-03):** the read-back was heard in the second voice and read accurately; a file-create confirm granted. Confirms said while undertone was still speaking, or within 1 s after, were refused without telling the user why; Claude answered a cost question with shell recipes instead of the cast-record tools. Both are addressed in the follow-up above. Full suite on `bcdcb9d`: 633 OK, 1 skipped.

**Added**
- Read-only MCP tier: five tools of the optional `cast-record` MCP server (`mcp__cast-record__cast_decisions`, `mcp__cast-record__cast_incidents`, `mcp__cast-record__cast_cost`, `mcp__cast-record__cast_sessions`, `mcp__cast-record__cast_ask`) run ungated on a voice turn. Listed in `voice_guard.READ_ONLY_MCP_TOOLS` as an exact-name allowlist in code (not config), because a typed turn could edit a config file to widen the voice gate. Vetted read-only: the maintainer checked that the cast-record server opens its database with `mode=ro`, uses parameterized queries, and has no SQL tool.
- Undertone's own voice: a second TTS voice for words undertone writes itself, which Claude's replies never use. Kokoro `af_heart` (env `UNDERTONE_KOKORO_OWN_VOICE`; constants `OWN_VOICE_ENV`, `DEFAULT_OWN_VOICE` in kokoro.py), or `say` `Samantha` (env `UNDERTONE_OWN_VOICE`). Name-checked and SHA-256 verified like the reply voice. The maintainer chose a distinct voice because Claude can imitate any words, but not a voice.
- Blocked-call record: when `voice_guard` denies a call on a voice turn, a short description is stored in the session state file (tool kind, program and file names, location classes, host names, but no argument values). It is written after the deny, best-effort, so the deny always stands. Keeps the last `MAX_BLOCKED` = 3 entries (each ≤ `MAX_WHAT_CHARS` = 160 chars) and counts evictions in `blocked_more`. Cleared on each new voice or typed turn; entries older than 300 s are ignored.
- Read-back in undertone's own voice: on a confirm reply, `voice_guard.read_back()` is attached as a `check` field on the reply line. `attach` speaks it after Claude's words, in undertone's own voice, and prints `undertone: <text>`. The `check` field is honoured only on a confirm, up to `MAX_CHECK_CHARS` = 600. Forms: `undertone blocked: <a>; and <b>`, then `, and N more blocked call(s)` when entries were evicted; or `undertone has not blocked anything yet.`; or `undertone could not read back what it blocked.` if state is unreadable. Vocabulary: fixed words only; names (at most 2 words, ≤20 chars) or `a long name`; location classes (`in this project`, `in your home folder`, `in the project git or Claude settings`, etc.); hosts as `a site under X` or `an international address` or `an IP address`. Never spoken or stored: argument values, MCP argument keys or values, commit messages, free text. `with N arguments` marks omissions. `overwriting` vs `appending to` for redirects. Complex shell commands read `a complex shell command starting with <prog>`; wrappers (sudo, env, xargs, timeout, `sh -c`, `find -exec`) name the program they run; up to 3 commands are named and the rest counted (`, and N more commands`); URLs and scp/ssh remotes in arguments read `to <host>` / `to a remote computer <host>`. The tool result tells Claude what was read back.
- Channel instructions changed: for destructive, irreversible or outward-facing actions requested by voice, Claude now makes the tool call first; after the voice guard refuses it, Claude sends `kind="confirm"`.

**Changed**
- Session state file (`~/.undertone/sessions/<token>.json`) now holds blocked-call descriptions: tool kinds, program and file names, location classes and host names.

**Security**
- Two security review rounds, 2026-10-03. Fixed: H1 (evicted entries vanished silently, and the first wording, "This will run: …", overclaimed for a turn-wide grant), M1 (Claude-chosen free words spoken in the own voice), M2 (unflagged omissions), M3 (secrets spoken and stored), N1 (URLs read as project files), L1 (`>` and `>>` sounded the same), L3 (a homograph host). Accepted residuals: short names (at most 2 words) are still Claude-chosen; an MCP server registered under the name `cast-record` (e.g. in a project `.mcp.json`) would get the ungated tier, because the hook sees only the tool name (accepted: an approved MCP server already runs arbitrary code at startup).
- Self-screen measurement (18 homophones and 20 ordinary sentences through `ScreenedSink` with the real wake detector, 2026-10-03): `am_adam` 14/18 blocked, 0/20 ordinary blocked; `af_heart` 14/18 blocked, 0/20 ordinary blocked. Own voice screened no worse than reply voice.
- Grant unchanged: a spoken confirm still unlocks every gated call for the rest of the turn (up to 300 s). The maintainer decided this on 2026-10-03 and declined binding the grant to the read-back calls. So the read-back reports what was blocked but does not limit what the confirm allows; a prompt-injected Claude can still get a harmless call confirmed and then run others inside the grant.

**Status**
- Built and reviewed 2026-10-03.
- Still to do: full test suite (`uv run python -m unittest discover -s tests`), because socket tests can't run in Claude's sandbox; a live test: a voice question using a cast-record tool runs with no confirm, and a voice request for a gated action produces Claude's confirm prompt followed by the read-back in undertone's own voice, and the two voices are easy to tell apart.
- Phase 8a's plan item "list the exact tool names the 8b candidates expose" (che-ical-mcp) is NOT done; it moves to 8b.

### 2026-10-02 — Phase 6: Jarvis speaks first

**Live test: passed (2026-10-03).**

**Added**
- `undertone attach` now tells you when Claude finishes or needs you, using fixed spoken templates: "Claude finished." (a turn ended: `Stop`), "Claude needs your permission." (a permission dialog), "Claude has a question for you." (`AskUserQuestion`), "Claude's plan is ready." (`ExitPlanMode`) and "Claude stopped with an error." (a turn ended on an API error: `StopFailure`).
- `--announce {all,permission,off}` on `undertone attach` (default `all`). `permission` speaks everything except "finished"; `off` speaks nothing.
- `python -P -m undertone.announce {stop,stopfailure,permissionrequest} --home … --session …`, the hook that sends them. `undertone session` installs it as async hooks: a second handler on the existing `Stop` entry (after the voice guard's), plus new `StopFailure` and `PermissionRequest` (matcher `*`) entries. It maps the hook payload to an event key and sends one line to `voice.sock`; nothing from the payload is forwarded. `Stop` sends nothing while `background_tasks` lists a `subagent` whose status is `running`; that agent's completion produces a later `Stop`, which announces. `PermissionRequest` replaced the planned `Notification` hook: in the Phase 6 spike (Claude Code 2.1.288), `Notification` never fired, while `PermissionRequest` fired on every permission prompt. There is no idle announcement.
- Protocol: the channel's `hello` now carries `"session": "<token>"` (its `UNDERTONE_SESSION`), and a one-shot connection with no `hello` can send `{"type":"announce","session":"<token>","event":"<event>"}`. `attach` speaks it only if the token equals (`hmac.compare_digest`) the one the attached channel sent and `--announce` enables the event. Nothing from the message is spoken or printed except the validated event key.
- Speaking rules (main thread): "finished" is skipped if a non-ack reply was spoken since the last "finished", and while a confirm is pending (set by a confirm reply, cleared when the next utterance is sent), so a "finished" follow-up window can never burn a pending confirm challenge. Any announcement is dropped while the listener is not idle, and the same event isn't repeated within 3.0 s (`ANNOUNCE_REPEAT_S`). Both reply flags reset when a new session attaches. Announcements queue behind current speech (never interrupt), play with talk-over off (the wake word still barges in), and go through the same speech path as replies (one scrub, plus the acoustic self-screen when Kokoro is used). Only "finished" opens the 6 s no-wake-word follow-up window, and a follow-up turn can never grant a voice confirmation. The window also opens after a typed turn's "finished", so you can walk away, hear that Claude finished, and ask a follow-up.
- Terminal output: `[announce: <event>]`, or `[announce skipped: <event> (<reason>)]` with reason `already answered`, `confirm pending`, `listening` or `repeat`.

**Fixed**
- A line on `voice.sock` nested too deeply to parse (`RecursionError`) is now ignored instead of dropping the connection.

**Security**
- The announce token is a routing check, not a secret against the same user: it is on the hook command lines, visible via `ps`. Announcements speak fixed templates only, never Claude's text. A "finished" window can only produce a follow-up turn, which never grants a confirmation, and "finished" is suppressed while a confirm is pending. Each event is spoken at most once per 3.0 s.
- `announce.py` always exits 0 and writes nothing to stdout or stderr, because `PermissionRequest` stdout is a decision channel. It drains stdin first, imports lazily, and needs an absolute `--home`, a 32-hex token and a `voice.sock` that is a socket owned by the same uid. It connects with a 0.5 s timeout and never reads from the socket. It logs at most one line per run to `<home>/logs/guard.log` (`announce <sent|skipped|error> <hook> <event or reason>`) and never logs the token or the payload.

### 2026-10-02 — Phase 4b: Kokoro voice

**Live test (2026-10-02):** with both `undertone session` and `undertone attach` restarted, The maintainer ran the checklist: banner `Voice: Kokoro (am_adam).`, Kokoro replies, talk-over, a confirm answered after the prompt finished, no self-trigger, and `--tts say` / `--no-aec` — "nothing odd, works great" (reported by the maintainer; per-step output not captured). The full suite ran 499 tests OK and `scripts/verify-offline.sh` passed 10/10 with the network denied.

**Added**
- Kokoro-82M ONNX resident TTS engine (phoneme-based; 0.05 s + 0.085 s/second of audio on idle M4 Max). Default voice: `am_adam`, the maintainer's pick from the samples; through the full playback path the self-screen blocked 0/200 ordinary sentences with it, against 2/200 with `am_michael`. Override with `UNDERTONE_KOKORO_VOICE` (e.g., `am_michael`, `bm_george`). Download via `undertone models fetch` (57 files, model + tokenizer + voice bins). Model pinned to `onnx-community/Kokoro-82M-v1.0-ONNX` revision `1939ad2a8e416c0acfeecc08a694d14ef25f2231`, fp32 `onnx/model.onnx` (~325 MB). Model weights: Apache-2.0. Runtime: onnxruntime 1.30 (CPU-only; CoreML was no faster and used 2× memory). MIT license.
- G2P (grapheme-to-phoneme) via espeak-ng, run as a separate subprocess (text on stdin), never linked. GPL-3.0-or-later; installed via Homebrew.
- Telemetry defense: `ORT_DISABLE_TELEMETRY=1` forced before onnxruntime import (Microsoft's 1DS telemetry client in the wheel is blocked).
- `UNDERTONE_ESPEAK_CMD` env var for tests; `UNDERTONE_TEST_REAL_KOKORO` (tests only; opt-in real-model smoke test).
- **Speech sanitizer unification:** `speaker.prepare_speech` is the single scrub point for all text before any TTS engine. One place: NUL strip, lone-surrogate strip, NFKC normalize, `spoken.sanitize_for_tts` (wake-word replace, bracket strip, zero-width chars), then `MAX_SPEECH_CHARS` cap (1000 chars). Both `say` and Kokoro exit via this scrub.
- **Acoustic self-screen:** `screen.ScreenedSink` wraps the echo-cancellation playback path (never `--no-aec` mode). Before each chunk plays, a fresh wake detector (cutoff 0.8, live detector fires at 0.97) runs on the last 2 s of played audio plus lookahead silence. Blocks homophones ("hey jarviss", "djarvis", etc.) that pass text scrub but fire the detector on Kokoro: 0/200 ordinary sentences blocked; 67/80 homophone attack clips blocked with the phrase at the very end of a chunk, and every clip that played peaked below 0.8.
- **Wake-word barge-in (no confirm):** when undertone is speaking, or within `UNDERTONE_SELF_WAKE_GUARD_S` (default 1.0 s, floor 1.0, cap 10.0; override for AirPlay latency), a wake word still interrupts but lands as `via: "wake_barge_in"`. This `via` never grants a confirmation, fail-closed. A barge-in turn is subject to the same gating rules as `follow_up` (unverified input). The confirm hint is now `[confirmation needed: when I finish, say "hey jarvis" and the phrase]`.
- `--tts {kokoro,say}` flag on `undertone chat` and `undertone attach` (default `kokoro`). Fallback: if espeak-ng is missing or the model isn't fetched, undertone prints a reason and uses `say`. Startup banner appends ` Voice: Kokoro (am_adam).` or ` Voice: say (Evan (Enhanced)).`.
- Measured latencies (warm, idle M4 Max): `say` first audio 0.54–0.56 s (process startup dominates); Kokoro first audio 0.12–0.27 s, with the first piece of a sentence capped at 32 phoneme tokens (~5–6 words; 47 tokens took 0.35 s), after trimming Kokoro's ~0.4 s of leading silence. Busy CPUs don't slow it; other onnxruntime processes do (two of them: ~3.6×).
- `scripts/verify-offline.sh` now proves Kokoro telemetry off, render, and self-screen work locally. Final line: `OFFLINE CHECK: PASS (wake, VAD, STT, TTS, Kokoro, self-screen, echo cancellation ran with network denied)`. Sample output block updated in README.

**Changed**
- `undertone models fetch` now also downloads Kokoro (model, tokenizer, voices; 57 files) and prints a reminder if espeak-ng is missing.
- `undertone chat` and `undertone attach` default to `--tts kokoro`; pass `--no-aec` to use `say` with no acoustic screen.
- `speaker.build_say_argv` unchanged; `speaker.espeak_phonemes` new (text → IPA phonemes via espeak-ng subprocess).

**Security**
- One scrub: `speaker.prepare_speech` is the single text-cleaning step for every TTS engine, before any phoneme or voice model sees the text: NUL and lone-surrogate strip, NFKC, wake-word replacement, every `[`/`]` removed (espeak-ng honors `[[...]]` phoneme input like `say` does), zero-width characters, and a 1000-character cap.
- Acoustic self-screen: a wake detector (cutoff 0.8) runs on our own audio before each chunk plays, to catch homophones that pass the text scrub but trigger the live wake detector when Kokoro speaks them. It blocked 0/200 ordinary sentences and 67/80 homophone attack clips. If the detector itself fails, speech stays muted and undertone prints `undertone: the wake-word screen failed (…); speech stays muted until restart` once.
- `wake_barge_in`: a wake word heard while undertone is speaking, or within 1 s after (`UNDERTONE_SELF_WAKE_GUARD_S` raises this to at most 10 s), still interrupts but can't grant a gated call. Only a `via: "wake"` turn, heard after that window of silence, satisfies a confirm challenge.
- Model integrity: the Kokoro model, tokenizer and all 55 voice files are checked against SHA-256 values pinned in `kokoro.py` (taken from the Hugging Face LFS ids of the pinned revision) every time they load from the cache. A mismatch, a corrupt model, a bad tokenizer or a failed warm-up falls back to `say` with `undertone: Kokoro voice unavailable (<reason>); using say`.
- Restart both `undertone attach` and `undertone session` after upgrading: an old `attach` tags barge-in wakes as `wake`, which can still grant.

**Dependency**
- onnxruntime 1.30.0 (MIT; Python package)
- flatbuffers (transitive, via onnxruntime; Apache-2.0)
- espeak-ng (GPL-3.0-or-later; installed via Homebrew, run as subprocess, never bundled or linked)

### 2026-10-01 — Phase 4a: echo cancellation and talk-over

**Added**
- `DuplexAudio` (duplex.py): full-duplex sounddevice `RawStream` (16 kHz mono int16, 10 ms blocks). Pairs each mic frame with the speaker frame it just wrote. WebRTC AEC3 via livekit's `AudioProcessingModule(echo_cancellation=True, noise_suppression=True, high_pass_filter=True)`: the speaker frame goes to `process_reverse_stream`, then the mic frame to `process_stream`. The delay hint is the stream's input+output latency or 40 ms if unknown. Offline simulation (2026-10-01): ERLE ≈ 35 dB, Silero VAD fired on residual in 0/217 chunks (vs 189/217 without AEC), user voice still detected at max probability 0.9999, ~60 µs per 10 ms frame.
- **Live test (2026-10-02, real speech, MacBook Pro speakers and mic):** undertone did not talk itself into a turn while playing a full reply. Talk-over fired on the user's voice, and a wake-word "confirm lemon canyon" granted a delete (`guard.log`: `pretooluse granted Bash`). The first build lost the user's words (the whole talk-over turn was dropped as echo), let a talked-over confirm prompt cut off its phrase, and sent "A Jarvis." as a turn; all three were fixed and the re-test worked (reported by the maintainer; per-step output not captured).
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
- Voice turns keep full session power but are unverified input (the maintainer's decision: confirm risky actions). Before anything destructive, irreversible or outward-facing, Claude sends a `kind=confirm` reply and asks the user to say the wake word, then "confirm <two fresh uncommon words>". The listener strips the wake word from every transcript, so Claude accepts "confirm walnut lantern" with the words immediately after "confirm". "Yes" is a refusal.
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
- Research plan and component picks (`docs/plan.md`, since moved out of the repo). The wake phrase is "hey jarvis", which ships pretrained, so no training is needed.
- Measurements:
  - wake word: 6/6 detected and 0 false accepts in a 30-minute live run;
  - STT median: 87 ms on a real voice;
  - persistent `claude -p` first token: about 1.2 s.
- Claude Code Channels, the research-preview way to push events into a running session, verified GO with a hello-world.
- An optional Docker image for training a custom wake phrase on 100% synthetic TTS clips.

**Commits:** ca64eb8, 4988819, ca9bc93, 53010e5, 69760f7

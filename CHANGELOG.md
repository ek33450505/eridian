# Changelog

All notable changes to eridian (named undertone before 2026-11-05) are documented here, in the style of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.2.0] - 2026-11-11

### 2026-11-11 — Memory note dates

**Changed**
- The memory save instruction says the date is recorded automatically and must not go in the note text.

### 2026-11-11 — Status line mood

**Added**
- `python -P -m eridian.statusline`: a status line segment that prints a short persona mood from the session directory's git state (worry, work, push, pull, good). It prints nothing outside a repo, on any error or timeout, or when any git clean or process filter driver is configured, so the status line never makes git run a filter program.

### 2026-11-11 — Memory injection

**Added**
- Personal sessions get a save instruction and the newest notes (at most 1500 characters, framed as background data, with JSON-quoted text and escaped `<` and `>`). Work sessions, and personal directories inside a work directory, get neither. Any memory error leaves the persona intact.

**Changed**
- The memory CLI also refuses a differently-cased path to the work directory (it compares by file identity as well as spelling).
- `cwd_under` moved to `eridian.paths`, with new `inside_dir` (file identity) and `within` (spelling or identity), which every work-directory check now uses.

### 2026-11-11 — Memory store

**Added**
- `python -P -m eridian.memory add|list|forget`: a personal-only store of short persona notes in `~/.eridian/memory.jsonl` (60 entries, 200 characters each, kinds `win`, `fail`, `joke`, `fact`, `mood`). It writes atomically under a lock, refuses symlinks, hard links and non-regular files, validates every line on read, and refuses to run inside the work directory.

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

### 2026-11-09 — Rocky persona for the typed chat

**Added**
- `python -P -m eridian.persona sessionstart --under DIR`: a Claude Code SessionStart hook that adds `TYPED_ROCKY_BLOCK` to the session's context when the session's cwd is inside `DIR`, and prints nothing otherwise. It always exits 0 once its arguments parse, so it can't block a session. Register it in your Claude Code settings yourself; eridian doesn't install it.
- `TYPED_ROCKY_BLOCK`: full Rocky speech in conversational turns, the same character with standard grammar in dense technical turns. Code, paths, numbers, commands, concerns, approval questions and anything written to disk or sent outside the chat stay plain English. The spoken `ROCKY_BLOCK` and `ERIDIAN_PERSONA` are unchanged.

### 2026-11-09 — Pre-public cleanup

**Changed**
- Docs no longer contain personal paths, private project names or internal planning notes. The phase plans and research notes moved out of the repo; README has a short Roadmap instead. README gained install-from-scratch steps, and SECURITY.md now covers the attach log and the persona hook.
- `pyproject.toml` gained `readme`, classifiers and the repository URL.

## Voice era (archived)

From 2026-09-30 to 2026-11-10 eridian was a voice interface, first under the name undertone. It listened for a wake word, spoke Claude's replies, and could send a spoken turn into a running Claude Code session. Its guard required a spoken confirm phrase before gated tool calls. It was archived on 2026-11-11 to focus on the typed persona.

- A wake word (`hey jarvis` by default; a custom-trained model could replace it via `ERIDIAN_WAKE_MODEL`) starts each turn, with Silero VAD end-of-turn detection and on-device speech-to-text.
- Claude's replies are spoken sentence by sentence, first with macOS `say`, then with Kokoro-82M, default voice `am_adam`.
- `eridian attach` (`undertone attach` before the rename) sends voice into a running Claude Code session through a channel and a `reply` tool.
- The voice-turn guard (`voice_guard`) gates risky tool calls behind a spoken confirm phrase, which talk-over and follow-up turns can never grant. An optional safe phrase was added later.
- Echo cancellation and talk-over: sustained speech stops a reply, and an echo text guard drops turns that repeat its own speech.
- Announcements such as "Claude finished." and "Claude needs your permission.", as fixed phrases.
- Per-session voice on and off, real mute, and a control socket for status, mute and voice on/off.
- On 2026-11-05 the project was renamed from undertone to eridian; `eridian migrate` moves the old state folder.

The code and the full history of this era are preserved at tag `voice-archive-v1`.

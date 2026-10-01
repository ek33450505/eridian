# Changelog

All notable changes to undertone are documented here, in the style of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). No release has been tagged yet; `pyproject.toml` is at 0.1.0.

## [Unreleased]

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

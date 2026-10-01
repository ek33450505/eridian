# undertone — research + plan

> **Date:** 2026-09-30 · **Status:** PLAN (nothing built) · **Machine:** M4 Max, 64 GB, macOS 26.6.2, Claude Code 2.1.286
> **Prior art:** `claude-agent-team/research/v8-phase-0-tauri-architecture-decision.md:65` (push-to-talk Voice MVP, never built)

## 1. Requirements (Ed, 2026-09-30)

| Axis | Choice |
|---|---|
| Where | Research **both**: (A) voice inside real Claude Code sessions, (B) standalone voice REPL driving Claude |
| Turn-taking | **Wake word + VAD auto end-of-turn + barge-in** (interrupt the reply) |
| Free means | **Fully local**: on-device STT/TTS, no API keys, no quotas, no audio leaves the Mac |

Scope note: the *text* of each turn still goes to Anthropic — that's Claude. "Local" applies to audio.

## 2. Findings that change the design

1. **Claude Code's built-in `/voice` is cloud STT.** "Voice dictation streams your recorded audio to Anthropic's servers for transcription. Audio is not processed locally." Push-to-talk / tap only; no continuous mode. → Cannot be the input for a fully-local Jarvis. ([voice-dictation](https://code.claude.com/docs/en/voice-dictation.md))
2. **VoiceMode (the leading Claude Code voice MCP) is ~80% there but misses exactly the Jarvis-defining parts.**
   - Local whisper.cpp + Kokoro supported, but **defaults leak to OpenAI**: `VOICEMODE_STT_BASE_URLS` default = `https://api.openai.com/v1` only; TTS default = local Kokoro *with silent OpenAI fallback*.
   - **No wake word** (issue #485 OPEN) and **no talk-over barge-in** (#211, #312 OPEN). Barge-in today = key / media key / control command.
   - End-of-turn = WebRTC VAD with `VOICEMODE_SILENCE_THRESHOLD` default **3.0 s** (sluggish).
   - Its loop is *Claude-driven* (Claude calls the `converse` MCP tool to speak-then-listen). The control socket binds **only while speaking**, so a wake word can stop playback but cannot start a conversation from idle.
3. **"Hey Jarvis" is free; bare "Jarvis" is not.** `pymicro-wakeword` and `openWakeWord` both ship a pretrained **"hey jarvis"**. The only off-the-shelf bare "jarvis" is Porcupine (`jarvis_mac.ppn`), which **requires a Picovoice Console AccessKey** → violates "no API keys". Bare "jarvis" = train a custom model. **Ed first chose "hey claude", then switched to "hey jarvis" (§7.2): it's pretrained, needs no training, and doesn't depend on which model is behind it.**
4. **Barge-in without headphones needs echo cancellation with a reference signal** — the canceller must see the audio being played. So TTS must be **rendered and played by our own process**, not fire-and-forget `say`.
5. **Claude Code already exposes the hooks both paths need** (all VERIFIED in official docs):
   - Stop hook payload carries `last_assistant_message` (full reply text) and `stop_hook_active`; hooks support `"async": true` so playback never blocks the session.
   - **Channels** (research preview): "an MCP server that pushes events into your running Claude Code session" — the only official way for an external daemon to inject a turn. Needs claude.ai auth; custom channel via `channels-reference`.
   - Headless: one long-lived `claude -p --input-format stream-json --output-format stream-json --include-partial-messages --verbose` accepts many turns over stdin and streams `text_delta` tokens. Agent SDK `interrupt()` cancels an in-flight turn (streaming mode only).
6. **Headless voice turns bypass CAST's interactive safety.** Per `working-conventions.md` → Irreversibility Interrupts, hook guards and confirm-pauses are bypassed in headless runs. A mis-heard phrase must not be able to run destructive tools → Path B needs a restrictive tool allowlist.

## 3. Component picks (verified 2026-09-30)

| Layer | Pick | Fallback | License | Maintained | Key gotcha |
|---|---|---|---|---|---|
| Wake word | **pymicro-wakeword** 2.5.0, built-in `HEY_JARVIS` (see §7.2) | openWakeWord `hey_jarvis`; any custom phrase via the optional `training/` image | Apache-2.0 (oWW pretrained models and anything trained with its data: **CC BY-NC-SA 4.0**, fine for personal use) | pymicro pushed 2026-09-17; oWW last release v0.6.0 (2024-02) | pymicro wheel is `macosx_15_0_universal2`; 16 kHz mono, 10 ms frames |
| VAD / end-of-turn | **Silero VAD** | WebRTC VAD | MIT | pushed 2026-09-29 | Skip **TEN VAD** — license `NOASSERTION` (custom terms). Lead (unverified): pykeio `earshot` on PyPI ("fast & accurate VAD", v1.2.2, 2026-08-19) |
| STT | **parakeet-mlx** (`parakeet-tdt-0.6b-v3`, `transcribe_stream`) | **whisper.cpp** 1.9.4 (`brew install whisper-cpp`, Metal, built-in Silero `--vad`) | Apache-2.0 / MIT | 2026-08-29 / 2026-09-28 | No latency numbers verified from primary sources → measure in Phase 0 |
| STT (future) | Apple SpeechAnalyzer (macOS 26, on-device) | — | — | — | `yap` (CC0, `brew install yap`) does files + **system audio** only, not the mic → would need a small Swift helper |
| TTS | **macOS `say`** + Zoe (Premium) (installed) → **Kokoro-82M** via mlx-audio (`--stream`) or kokoro-onnx (~300 MB / ~80 MB int8) | — | Kokoro weights Apache-2.0; mlx-audio/kokoro-onnx MIT | active | Kokoro must stay resident (cold CLI start is seconds). Piper: `rhasspy/piper` **archived**, successor `piper1-gpl` is **GPL-3.0** — not needed |
| Echo cancel | **livekit** 1.1.20 `AudioProcessingModule(echo_cancellation=True)` (`process_stream` mic / `process_reverse_stream` playback) | Apple VoiceProcessingIO via Swift helper | Apache-2.0 | active | Needs the playback PCM as reference (finding 4). Apple docs only say "voice processing features"; AEC detail comes from WWDC, not verified in docs |
| Porcupine | **Excluded** | — | — | — | AccessKey required |

## 4. Architecture — one shared core, two "brains"

Build the audio pipeline **once** as a small resident daemon (`undertone serve`, Python via `uv`). Paths A and B are thin adapters.

```
 mic ──► [echo cancel*] ──► wake word ("hey jarvis") ──► Silero VAD ──► STT (parakeet-mlx)
  ▲                              │ (also active during playback = barge-in)         │ text
  │                              ▼                                                  ▼
 speaker ◄── player ◄── TTS queue (sentence chunks) ◄─────────── BRAIN ADAPTER ◄────┘
                                                        A: Channels in / Stop hook out
                                                        B: persistent claude -p stream-json
 * Phase 4
```

State machine: `IDLE` (wake word only) → `LISTENING` (VAD, end-of-turn at ~0.7 s silence) → `THINKING` → `SPEAKING` (wake word stays live; later AEC + VAD for talk-over) → back to `IDLE`, with a short **follow-up window** (e.g. 6 s of VAD-armed listening after a reply) so back-and-forth doesn't need "hey jarvis" every turn.

### Path A — voice in real Claude Code sessions

| Direction | Mechanism | Status |
|---|---|---|
| Out (speak replies) | `Stop` hook, `async: true` → reads `last_assistant_message` → posts text to `undertone` over a unix socket (hook never plays audio itself) | VERIFIED primitives |
| Spoken summary | Opted-in sessions get an instruction (via `UserPromptSubmit` `additionalContext`) to end replies with `SPOKEN: <≤2 sentences, no code/paths>`; hook speaks that line, else falls back to stripping code blocks and speaking the first 2 sentences | Design inference |
| In (voice → session) | Custom **Channel** MCP server: `undertone` transcript → channel event → running session | VERIFIED exists; research preview; availability on Ed's account UNTESTED |
| Barge-in | `undertone` stops playback locally. Interrupting Claude's in-flight turn from outside: **NOT FOUND** in docs | Gap |

Must-haves: speak only for **opted-in sessions** (flag file per `session_id`, toggled by a `/jarvis` command) or every open session talks at once; skip when `stop_hook_active` is true (the CAST journal Stop hook blocks and causes a continuation turn).

### Path B — standalone voice REPL

- `undertone` owns one persistent `claude -p --input-format stream-json --output-format stream-json --include-partial-messages --verbose` process (or Python `claude-agent-sdk` `ClaudeSDKClient` for a documented `interrupt()`).
- Stream `text_delta` → sentence chunker → TTS, so the first sentence plays while the rest generates. Code blocks are not read aloud ("code's on screen").
- Barge-in: stop playback + `interrupt()`.
- Continuity: persist the session id; `--resume <id>` after restart, and Ed can open the same conversation in a terminal with `claude --resume <id>`.
- Safety: `--allowedTools` read-only by default (Read, Grep, Glob, WebSearch/WebFetch); anything else needs a spoken confirmation.
- Hooks: every CAST hook fires on every voice turn (memory recall adds latency; the journal Stop hook can block and trigger an extra turn). Options: run the child with `CLAUDE_SUBPROCESS=1` (CAST hooks exit early — loses cast.db records) or a trimmed `--settings`. Decide in Phase 2.

### A vs B

| | A: inside Claude Code | B: standalone REPL |
|---|---|---|
| Agent power | Full (your real session, tools, CAST) | Full CLI, but tools deliberately restricted |
| Latency to first word | Reply must **finish** before the Stop hook fires | **Streams** — first sentence plays early |
| Barge-in | Stop playback only | Stop playback + cancel the turn |
| Hands-free input | Depends on Channels (research preview) | Fully under our control |
| Risk | Research-preview dependency | Headless bypasses CAST confirm-pauses |
| Best for | "Read me what you did" while coding | Conversational Jarvis |

## 5. Latency budget (Phase 0 measurements, 2026-09-30, M4 Max)

| Stage | Target | Measured | Note |
|---|---|---|---|
| Wake word fires | ≤ 0.3 s | — | Phase 0 item for Ed: needs mic permission and 30 min of real use |
| End-of-turn silence | 0.6–0.8 s | — | Biggest local lever; VoiceMode's 3.0 s is why it feels slow |
| STT, 1.8–3.8 s utterances | ≤ 0.3 s | **parakeet-mlx: median 44 ms** (34–127 ms), 5/5 correct (one comma dropped), 1.2 GB RSS. mlx-whisper large-v3-turbo: median 217 ms, 5/5 exact, 1.9 GB | Input was clean synthetic `say` speech. Real voice through the built-in mic is still untested. Feed **in-memory arrays**: both libraries shell out to `ffmpeg` for file input, and ffmpeg isn't needed for mic audio. parakeet needs float32 input; a bf16 cast breaks its STFT |
| Claude first token | 1–3 s | **Persistent `claude -p` stream-json: 0.9–3.1 s per turn, median ~1.2 s**, turn 1 including spawn 1.6–1.9 s. Cold one-shot `claude -p` per turn: **5.5–13 s** | One-shot is ruled out: Path B must keep one process alive. `--strict-mcp-config` / `--setting-sources project` gave no clear gain. `--bare` is unusable because it accepts only `ANTHROPIC_API_KEY` and never reads the OAuth login. Why one-shot is slower is not isolated (stdin was ruled out) |
| TTS first audio | ≤ 0.3 s | **`say` + Zoe Premium: 0.73 s** to render one sentence to a file (includes process start). Kokoro via mlx-audio: **BLOCKED** | Kokoro: `espeakng-loader` 0.2.4 (macOS arm64) `exit(1)`s on a data path compiled in on its CI machine and ignores `ESPEAK_DATA_PATH` (mlx-audio 0.5.7, misaki 0.9.4). Try kokoro-onnx or Homebrew espeak-ng in Phase 4. A resident `NSSpeechSynthesizer`/AVSpeech process should beat `say`'s per-call process start |
| **Speech end → first spoken word** | **~2–4 s** (Path B) | ≈ 0.7 s end-of-turn + 0.05 s STT + ~1.2 s Claude + ≤ 0.7 s TTS ≈ **2.7 s** estimated from parts | Not yet measured end to end. Path A adds the full reply-generation time |

**Channels (Path A input), checked against `channels-reference`:** a custom channel needs `claude --dangerously-load-development-channels server:<name>`, which shows a full-screen warning **that must be accepted interactively**, so testing can't be automated. The documented runtime is Node/Bun/Deno with `@modelcontextprotocol/sdk`; Node v26 is installed, and Python is unverified. Two-way channels also expose a **reply tool**, which could replace the Stop hook as Path A output, and *permission relay* forwards tool approvals to the channel. The hello-world test is a Phase 0 item for Ed.

## 6. Phased plan

| Phase | Deliverable | Size | Exit criteria |
|---|---|---|---|
| **0. Spike** (throwaway, scratchpad) | Measure `hey jarvis` (pymicro-wakeword) false accepts during 30 min of normal work (incl. a meeting/video) + CPU %; parakeet-mlx vs whisper.cpp on 10 recorded utterances; `say` vs Kokoro first audio; `claude -p` stream-json time to first token; **hello-world Channel** into a live session | 1 session | Numbers in this doc; Channels GO/NO-GO |
| **1. Speak replies** (Path A out) | `undertone` speaker (unix socket + `say`) + async Stop hook + `/undertone on|off` + `SPOKEN:` extraction | 1 session | Opted-in session speaks a ≤2-sentence summary; other sessions stay silent; no speech on `stop_hook_active` turns |
| **2. Listening core + Path B** | mic → wake → Silero VAD → STT → persistent `claude -p` → sentence-streamed TTS; wake-word barge-in; follow-up window; read-only tool policy | 1–2 sessions | 5-turn spoken conversation, hands-free, speech-end → first word ≤ 4 s |
| **3. Path A in** | Channel MCP server fed by `undertone` | 1 session | "Hey Jarvis, run the tests" lands as a turn in a running session (only if Phase 0 = GO) |
| **4. Polish** | Kokoro resident voice; livekit AEC talk-over barge-in (TTS rendered + played by `undertone` for the reference signal); optional custom verifier model if "hey jarvis" false-accepts | 1–2 sessions | Talking over a reply stops it without the wake word, speakers on, no self-trigger |
| **5. Offline brain (opt-in)** | `--offline`: Path B drives Ollama (`gpt-oss:20b`) directly through its streaming chat API instead of `claude -p`; same audio core, same sentence-streamed TTS | 1 session | A 5-turn spoken conversation with Wi-Fi off; README states the quality trade-off |
| **D. Docs to flagship standard** (Ed, 2026-09-30) | README at the `claude-agent-team` bar, led by a **"Local-first by design (the point of this repo)"** section in the style of warden's "Safety model". It includes a data-flow table with three columns: stays on the Mac (mic audio, wake word, VAD, STT, TTS, all models), goes to Anthropic (the transcribed text of each turn, nothing else), never exists (API keys, cloud fallbacks — contrast VoiceMode's OpenAI defaults). Also: how to verify it yourself; a component and model license table (note: oWW-trained models carry **non-commercial** data terms); quick start; architecture diagram; status/roadmap; testing. Plus `docs/local-first.md`, `docs/architecture.md`, `docs/training.md`, `LICENSE`, `SECURITY.md` (mic access, local socket permissions), `CHANGELOG.md` | 1 session, `docs` agent; first pass after Phase 0 lands, then refreshed at every phase end | Every local-first claim is **demonstrable**: an offline test runs wake → VAD → STT → TTS in a `--network none` container, plus a documented `nettop`/`lsof -i` check that the daemon's only remote connection is Claude's |

Ceremony: each phase is single-session → native plan mode + one implementer + `code-reviewer`; the Stop hook and anything under `scripts/` also gets `security` if it lands in CAST.

## 7. Decisions

1. **Where it lives and its name — DECIDED (Ed, 2026-09-30): new repo `undertone`** → `~/Projects/personal/undertone`, private GitHub `ek33450505/undertone`. It was created as `hey-claude` and renamed the same day, before any code landed. `jarvis` was taken by the unrelated JARVIS pa-agents repo, which Ed says to ignore as over-engineered. `earshot` and `sotto` were rejected because PyPI has active projects with those names in the same field. The plan moved here from `claude-agent-team/research/`.
2. **Wake phrase — DECIDED (Ed, 2026-09-30): "hey jarvis".** pymicro-wakeword ships it pretrained (Apache-2.0), so **no training run is needed**, and the phrase doesn't depend on which model answers. An earlier choice, "hey claude", was dropped before training started. It had no public model and a high false-accept risk, since "Claude" comes up all the time at work. The `training/` Docker image stays as an **optional "bring your own wake phrase"** tool. It trains openWakeWord on 100% synthetic TTS clips, locally on CPU, with no recordings of the user. The negative features (17 GB) are at `~/Datasets/openwakeword/` and the work dir is `~/Datasets/undertone-training/`. A second-stage custom verifier model can be trained if false accepts show up.
3. **VoiceMode — recommendation: skip.** It can't wake from idle and its defaults leak to OpenAI.
4. **Path B tool policy — ADVISED: read-only, no spoken confirmation in v1.** Allow Read/Grep/Glob/WebSearch/WebFetch plus a short read-only Bash allowlist (`git status/log/diff`); deny everything else. Why not spoken confirmation: STT can mishear "yes", voice confirms get rubber-stamped, and CAST's confirm-pauses don't run in headless. Changes go through Path A, where the real session's guards run and the diff is on screen. Revisit after a week of use.
5. **`/voice` — ADVISED: keep as a stopgap, turn it off when Phase 3 lands.** It only records while space is held (no ambient capture), and Anthropic already receives the transcript text. The raw audio (voiceprint, background) is the real difference. If "no audio off the Mac" is absolute rather than a build requirement, turn it off now.
6. **Model behind it (the "brain") — DECIDED (Ed, 2026-09-30): Claude via Anthropic by default, for quality.** "I would prefer better quality. Offline off by default and we go through anthropic." Offline mode is an **opt-in** for Path B (`--offline`): Ollama `gpt-oss:20b` (already installed) driven directly, which makes the whole loop local and lets it work with Wi-Fi off. Path A can only go fully local through `ccr`, which is not recommended for agentic work. The brain adapter stays pluggable either way.

## 8. Sources

Claude Code: [hooks](https://code.claude.com/docs/en/hooks.md) · [voice-dictation](https://code.claude.com/docs/en/voice-dictation.md) · [headless](https://code.claude.com/docs/en/headless.md) · [cli-reference](https://code.claude.com/docs/en/cli-reference.md) · [channels](https://code.claude.com/docs/en/channels.md) · [agent-sdk/typescript](https://code.claude.com/docs/en/agent-sdk/typescript.md) · [streaming-vs-single-mode](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode.md)
Wake word: [OHF-Voice/pymicro-wakeword](https://github.com/OHF-Voice/pymicro-wakeword) (`const.py` `Model.HEY_JARVIS`; PyPI 2.5.0 wheels) · [dscripka/openWakeWord](https://github.com/dscripka/openWakeWord) (README §License) · [Picovoice/porcupine](https://github.com/Picovoice/porcupine) (`resources/keyword_files/mac/jarvis_mac.ppn`; AccessKey in every demo) · [Home Assistant wake word](https://www.home-assistant.io/voice_control/about_wake_word/)
VAD/STT: [snakers4/silero-vad](https://github.com/snakers4/silero-vad) · [senstella/parakeet-mlx](https://github.com/senstella/parakeet-mlx) · [ggml-org/whisper.cpp](https://github.com/ggml-org/whisper.cpp) · [finnvoor/yap](https://github.com/finnvoor/yap)
TTS/AEC: [hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) · [Blaizzy/mlx-audio](https://github.com/Blaizzy/mlx-audio) · [thewh1teagle/kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) · [OHF-Voice/piper1-gpl](https://github.com/OHF-Voice/piper1-gpl) · [livekit/python-sdks `apm.py`](https://github.com/livekit/python-sdks) · Apple [setVoiceProcessingEnabled](https://developer.apple.com/documentation/avfaudio/avaudioionode/setvoiceprocessingenabled(_:))
Existing projects: [mbailey/voicemode](https://github.com/mbailey/voicemode) (`docs/reference/environment.md`; issues #485, #211, #312; v8.12.0) · [johnmatthewtennant/mcp-voice-hooks](https://github.com/johnmatthewtennant/mcp-voice-hooks) (browser Web Speech; no license)

## 9. How this was researched

The `/deep-research` harness (run `wf_2edae485-817`, 70 agents) extracted 127 claims but verified 12 and confirmed only 2 — its verifiers had no web access and default-refute, so they killed compound claims over minor overreach. Everything in §2–§3 was therefore re-verified directly against primary sources (GitHub via `gh api`, PyPI JSON, official Claude Code / Apple / Hugging Face docs), plus a `claude-code-guide` pass on Claude Code mechanics and a researcher pass on VoiceMode (spot-checked). Items marked unverified / NOT FOUND / estimate are exactly that.

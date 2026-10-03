# Q3b: Comparable voice assistants - features undertone lacks
Date: 2026-10-02. Method: WebSearch/WebFetch only; no installs. Many fetches returned thin README summaries, see section 5.

## 1. Project survey

| Project | Repo / URL | License | Notable features relevant to undertone |
|---|---|---|---|
| VoiceMode (mbailey) | https://github.com/mbailey/voicemode | MIT | Claude Code plugin + MCP server; local Whisper + Kokoro; silence detection; LiveKit transport; search snippets also list: soundfonts (tones on tool use), TTS/audio hook notifications ("spoken feedback when Claude writes files, runs commands"), voice handoff between Claude Code agents. v8.12.0p0 (2026-07-21, per aggregator). Pull-style: Claude calls a converse tool, so it speaks only when the agent decides to. |
| Claude Code /voice | https://code.claude.com/docs/en/voice-dictation | Proprietary (built in) | Dictation only, no spoken replies. Hold-space push-to-talk or tap-to-send; rebindable `voice:pushToTalk`; project name and git branch fed as recognition hints; `autoSubmit`; Esc/Ctrl+C cancels; dictates into agent-view peek replies to background sessions. Cloud STT, claude.ai account only, no SSH. |
| Claude Code hooks + agent view | https://code.claude.com/docs/en/hooks , https://code.claude.com/docs/en/agent-view | Proprietary | Notification hook types: permission_prompt, idle_prompt, agent_needs_input, agent_completed (verified in docs); payload has session_id, cwd, message. Agent view = dashboard of background sessions with peek and reply. |
| souliane/teatree TTS plugin | https://github.com/souliane/teatree (article: https://dev.to/souliane/text-to-speech-for-claude-code-hear-what-the-agent-is-doing-3mom) | not stated | Stop hook speaks final reply (600 char cap); Notification hook speaks permission prompts; per-session voice and mode `dm`/`all`/`off`; uses `say`/espeak. |
| claude-code-tts (ybouhjira) | https://github.com/ybouhjira/claude-code-tts | not checked | OpenAI-TTS MCP plugin with Stop hook speaking first sentence. |
| Superwhisper (+ superwhisper-claude-code) | https://superwhisper.com/claude-code , https://github.com/superultrainc/superwhisper-claude-code | Superwhisper commercial; plugin repo license not checked | "Agent done, answer by voice": overlay appears when Claude Code finishes or needs permission, you reply by voice; works for Claude Code, opencode, Codex. macOS only. |
| Handy | not fetched [unverified URL] | open source per aggregator | Push-to-talk dictation, Mac/Win/Linux. |
| Home Assistant Assist / Wyoming | https://www.home-assistant.io/voice_control/ , https://github.com/rhasspy/wyoming-satellite | Assist: Apache-2.0 (HA core) [license not fetched]; wyoming-satellite not checked | Satellites (ESP32, Voice PE, Wyoming); timers on satellites; broadcast/announce to satellites; continued conversation when the LLM asks a question; wake words toggled on the fly (e.g. "stop" active only while a timer rings or an announcement plays); proactive follow-ups; critical confirmations for locks; multi-room media. https://www.home-assistant.io/blog/2025/06/25/voice-chapter-10/ , https://www.home-assistant.io/blog/2024/06/26/voice-chapter-7/ |
| OpenVoiceOS / Mycroft | https://github.com/OpenVoiceOS/ovos-core | Apache-2.0 | Skill plugins (Mycroft-compatible), persona/LLM plugin, GUI on distributions. Timers/wake word not confirmed from README. |
| Rhasspy | https://github.com/rhasspy | MIT | Offline toolkit; now superseded by Wyoming/HA stack; intent grammar approach. |
| Willow | https://github.com/HeyWillow/willow | Apache-2.0 | ESP32 satellite, wake word, Whisper via inference server, HA integration. Status stated "active" by fetch; not independently confirmed. |
| Talon Voice | https://talonvoice.com/ , https://github.com/talonhub/community | Talon proprietary (free alpha, Patreon beta); community commands MIT | Voice + noise ("pop", "hiss") + eye tracking; Python-scripted command grammar; spoken-letter alphabet for code. Command-grammar, not conversational. |
| Serenade | https://github.com/serenadeai/serenade | Apache-2.0 (main), MIT (VS Code ext) | Speech-to-code engine; original stalled ~3 yrs, community fork https://github.com/sombrafam/serenade. |
| macOS Voice Control | https://support.apple.com/guide/mac-help/use-voice-control-commands-mh40719/mac | Proprietary | Dictation mode vs command mode; "show numbers"/grid overlays; custom commands ("make this speakable"). Eyes-free UI control, no agent awareness. |
| JARVIS (adewaskar) | https://github.com/adewaskar/jarvis | MIT | "Hey Jarvis" + Claude Agent SDK brain, Three.js holographic UI, MCP tools, browser STT/TTS or ElevenLabs. |
| Local Python Jarvises | https://github.com/atuldhull/jarvis , https://github.com/yasir24s/jarvis , https://github.com/MichelleBudri/my-jarvis | not checked | atuldhull: Silero VAD + LiveKit barge-in, Whisper/Piper/Ollama. yasir24s: speaker recognition, arc-reactor HUD. my-jarvis: timers/weather roadmap. All pursue the offline-LLM brain (on hold for undertone). |
| localvoxtral issue #735 | https://github.com/T0mSIlver/localvoxtral/issues/735 | n/a (issue) | Survey (2026-09-26) stating: no existing tool routes spoken names to multiple live agent sessions; Amp "Puck" acts on the viewed thread; Claude Code /voice limited to peeked session. |

## 2. Candidate features for undertone

Effort in sessions (1 session = one focused coding session incl. tests/review). Local-first: Y/N.

| # | Feature | Who has it | Value | Why | Effort | Local-first |
|---|---|---|---|---|---|---|
| 1 | Proactive speech on Stop / Notification (agent_completed, permission_prompt, idle_prompt, agent_needs_input) | Superwhisper plugin, teatree, claude-code-tts, VoiceMode hooks, HA announce | High | Biggest hands-free gap: undertone is reactive; user must look to learn Claude finished or is blocked. Hook payload carries session_id/message so it plugs into the existing MCP channel and TTS. Pair with the existing wake-word-only confirm path. | 1-2 | Y (hooks + local Kokoro) |
| 2 | Spoken short summary of last result (Stop payload last_assistant_message, truncated, scrubbed via existing speech choke point) | teatree (600 chars), claude-code-tts | High | "Read me what it did" without screen; reuses scrub + sentence streaming. Needs a summarizer or head-truncation policy. | 1 | Y if truncation; a cheap local summary needs an LLM (on hold) so use truncation or ask Claude to emit a spoken summary line |
| 3 | Push-to-talk hotkey (global, works while terminal unfocused) | Claude Code /voice, Handy, Superwhisper | High | Noisy room, open office, or a false-wake lockout; deterministic start with no wake word and no self-trigger risk. Global hotkey on macOS needs Accessibility/Input Monitoring permission. | 1-2 | Y |
| 4 | Multi-session routing by voice ("tell the api session ...", "which sessions are waiting?") | none ships it per localvoxtral #735; Claude Code agent view is the manual analog; VoiceMode has agent handoff | Med-High | undertone is "latest session wins"; with several sessions a wrong target is a real risk. Needs session registry + name aliases (cwd/branch) + disambiguation. | 2-3 | Y |
| 5 | Status/cost spoken summaries ("how many sessions are working?", "what did that cost?") | none found doing this by voice; agent view shows state visually | Med | Nice but depends on a stable cost source; session state is derivable from hook events (feature 1). Cost data source not verified. | 1-2 | Y |
| 6 | Headphones/AirPods mode (route in/out to BT device, relax AEC/self-screen when headset detected, handle HFP 16 kHz sample-rate drop) | none of the surveyed projects documents it | Med | Headsets remove echo so talk-over is cleaner, but AirPods switching to HFP degrades output quality and may break device selection; real gotcha on macOS. Needs hardware testing. | 1-2 | Y |
| 7 | Transcript/conversation history (searchable log of heard + spoken turns, replay "what did you say?") | VoiceMode (audio saving/logging), HA conversation history | Med | Recover a missed spoken answer; debug STT. Privacy: store locally with retention limit; undertone SECURITY posture needs a decision on retention. | 1 | Y |
| 8 | Repeat last / "say that again" and stop/silence-all command | HA ("stop" wake word only while speaking/timer), Alexa-style | Med | Cheap and frequently needed; likely partly covered by barge-in. | 0.5 | Y |
| 9 | Dictation-into-editor / free-form dictation mode with coding vocabulary hints (project and branch names) | Claude Code /voice, Wispr Flow, Superwhisper, Talon | Low-Med | Claude Code already injects project/branch hints for its own /voice; undertone injects into the same prompt so marginal gain is custom vocabulary biasing for Parakeet (feasibility not verified). | 1-2 | Y |
| 10 | Menu-bar status presence (listening/thinking/speaking/muted, mute toggle) | Superwhisper overlay, HA companion, Jarvis HUDs | Med | Trust signal for an always-listening mic; also a manual mute/PTT surface. Needs a macOS menu-bar shim (rumps or Swift). | 2 | Y |
| 11 | Soundfont earcons (short tones for tool use/done/needs input) | VoiceMode soundfonts | Low-Med | Quieter than speech for frequent events; trivial once feature 1 exists. | 0.5 | Y |
| 12 | Timers/reminders | HA, OVOS, my-jarvis | Low | Off-mission for a Claude Code voice layer. | 1 | Y |
| 13 | Speaker identification | yasir24s/jarvis (claims), covered by Q3a elsewhere | Low-Med | Handled by researcher__jarvis-q3a-speaker-verify; not duplicated here. | n/a | Y |
| 14 | Satellite/remote mic (phone as mic, LiveKit) | HA/Wyoming, Willow, VoiceMode LiveKit | Low | Single-user desk use. | 3+ | Mixed |
| 15 | "Read me the diff" flow | none found | Low-Med | Spoken diffs are poor audio; better as a summary (feature 2) plus "open it" action. | 1-2 | Y |

## 3. Top 5 picks

1. Proactive speech from Notification/Stop hooks (feature 1). Highest value, and every comparable that targets Claude Code ships some form (Superwhisper, teatree, VoiceMode). Notification matcher types are confirmed in the hooks docs. Route through the existing speech choke point so scrub and self-screen apply. Open decision: how a hook (separate process) reaches the running undertone daemon (unix socket or the MCP channel).
2. Push-to-talk hotkey (feature 3). Table stakes in Claude Code /voice and Handy; solves noisy-room and false-wake cases without the wake-word path, and gives a clean "I am addressing you" signal.
3. Hook-driven answer loop: when feature 1 announces "needs permission/answer", open the follow-up window immediately and keep voice-confirm wake-word-only (Superwhisper's "agent done, answer by voice" loop, HA continued conversation). Small increment on pick 1.
4. Multi-session routing and "what is waiting?" status (features 4+5). Differentiator: localvoxtral #735 reports no tool does spoken-name routing. Builds on session_id carried by hooks; start with "status of sessions" plus "switch to <name>" before arbitrary dictation by name.
5. Menu-bar status + mute (feature 10), with transcript history (feature 7) as the companion. Trust/visibility for an always-on mic; history lets the user recover missed speech. Headphone mode (feature 6) is the runner-up pending hardware testing.

## 4. Sources
Verified by fetch this session:
- https://code.claude.com/docs/en/voice-dictation - /voice hold/tap, cloud STT, agent-view dictation
- https://code.claude.com/docs/en/hooks - Notification matcher values and payload
- https://github.com/mbailey/voicemode - MIT, local Whisper/Kokoro (README summary)
- https://blog.digicuratoragency.com/jarvis-voice-assistant-claude-code/ - adewaskar/jarvis, MIT
- https://zentor.ai/blog/claude-code-voice-mode - /voice has no spoken replies
- https://dev.to/souliane/text-to-speech-for-claude-code-hear-what-the-agent-is-doing-3mom - teatree TTS hooks
- https://github.com/T0mSIlver/localvoxtral/issues/735 - survey of agent voice tools
- https://www.home-assistant.io/blog/2025/06/25/voice-chapter-10/ - HA voice features
- https://github.com/OpenVoiceOS/ovos-core - Apache-2.0
- https://github.com/HeyWillow/willow - Apache-2.0
From search results only (snippets, not fetched):
- https://www.home-assistant.io/blog/2025/02/13/voice-chapter-9-speech-to-phrase/ , https://www.home-assistant.io/blog/2024/06/26/voice-chapter-7/ , https://github.com/rhasspy/wyoming-satellite
- https://superwhisper.com/claude-code , https://github.com/superultrainc/superwhisper-claude-code , https://news.ycombinator.com/item?id=47936169
- https://github.com/rhasspy , https://talonvoice.com/ , https://github.com/talonhub/community , https://github.com/serenadeai/serenade , https://support.apple.com/guide/mac-help/use-voice-control-commands-mh40719/mac
- https://github.com/ybouhjira/claude-code-tts , https://github.com/atuldhull/jarvis , https://github.com/yasir24s/jarvis , https://github.com/MichelleBudri/my-jarvis
- https://code.claude.com/docs/en/agent-view

## 5. Not verified
- VoiceMode soundfonts/hooks/agent-handoff: from search-result summaries (agentskills/claudepluginhub aggregators); the README and voicemode.dev fetches did not mention them. Version 8.12.0p0 date also aggregator-sourced.
- Superwhisper plugin behaviour and macOS-only/v2.13 claim: search snippets only; plugin repo license not checked.
- Licenses not checked: Home Assistant Assist, wyoming-satellite, claude-code-tts, teatree, superwhisper-claude-code, atuldhull/yasir24s/my-jarvis, Handy.
- OVOS timers/wake word, Willow multi-device/announce, Rhasspy current maintenance status: not confirmed.
- Talon licensing nuance (proprietary core, MIT community set) from a search summary and EULA link only.
- Speaker identification coverage in yasir24s/jarvis: README claim only; see Q3a.
- Cost-data source for feature 5, Parakeet vocabulary biasing for feature 9, AirPods HFP behavior on macOS for feature 6, and global-hotkey permission needs: not researched.
- No project's undertone-comparable echo cancellation or self-screen was found; absence is "not found", not "proven absent".
- Not covered: Amp Puck, opencode, herdr, cmux voice details beyond the localvoxtral issue summary.

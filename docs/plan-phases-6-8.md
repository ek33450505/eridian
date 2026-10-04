# undertone — Phases 6–8 plan (weekend of 2026-10-03)

> **Approved by Ed, 2026-10-02.** Order: finish Phase 4b, then 6 → 7 → 8, over several sessions. The research behind it is in [`docs/research/2026-10-02-jarvis/`](research/2026-10-02-jarvis/). Each file there ends with a "Not verified" list, so treat anything sourced from search snippets as a hypothesis until a spike confirms it.
> On hold (Ed, 2026-10-02): "hey claude" (custom wake word) and Phase 5 (offline brain). Both stay in v1; don't start them.

## The short version

- **No new hub.** Claude Code + MCP already is the hub; undertone is the voice front end into it. "Connecting my apps" means vetted MCP servers or Shortcuts, plus a voice-safety tier for them (Phase 8).
- **No full desktop app.** A small signed Swift menu-bar helper owns the microphone permission, keeps `undertone attach` running, shows state, and adds mute and push-to-talk (Phase 7). Bundling Python + MLX + onnxruntime into a notarized app isn't worth it. cast-desktop may get a read-only voice status panel later.
- **First, Jarvis speaks first** (Phase 6). It announces when Claude finishes or needs you, then opens the no-wake-word follow-up window. That's the clearest gap against VoiceMode and Superwhisper, and it needs no new permissions.

## Ground rules (every session)

- Native plan mode at the start of each session. One `backend-writer` per unit (Swift work too). `code-reviewer` after every unit. `security` on anything touching speech output, voice turns, `voice_guard`, the channel, or a socket.
- Hash a unit's files before review and re-check after. Read `DONE_WITH_CONCERNS` notes in full.
- Mutation-test every new control: break it, see its test fail, restore.
- Verify every commit with `git cat-file -t` / `git log` (the commit agent has reported commits that didn't exist).
- Agent-written docs: grep every code span, then **read the prose** — on 2026-10-02 all 120 spans checked out and the prose still had 15 errors.
- Claude's sandbox blocks unix-socket binds, `/tmp` writes and MLX, so `tests.test_voice`, `tests.test_mcp_channel` and `tests.test_stt` run only on Ed's side (`! uv run python -m unittest discover -s tests`). Live tests are always Ed's.
- After any protocol, hook or instruction change, restart **both** `undertone attach` and `undertone session`.

## Session map

| Session | Goal | Exit criterion |
|---|---|---|
| S0 | Finish Phase 4b | PR merged after Ed's live test |
| S1 | Phase 6: Jarvis speaks first | Ed types a long task, walks away, hears "Claude finished", asks a follow-up without the wake word |
| S2 | Phase 7a: helper spike + core | Spike go; the helper supervises `attach` and shows its state |
| S3 | Phase 7b: push-to-talk, mute, launch at login | Live: hotkey turn works, mute really stops the mic, helper survives re-login |
| S4 | Phase 8a: voice-safety tier for app tools | Read-only tools run on a voice turn; side-effect tools need a confirm whose prompt undertone, not Claude, describes |
| S5 | Phase 8b: first integrations | Live: calendar question, reminder (with confirm), music, "what did I spend today?" |

Sessions can spill; the order matters more than the boundaries.

## S0 — finish Phase 4b

**✓ Done 2026-10-02:** contention test run (default session kept; first piece cut to 32 tokens, 0.286 s first audio in the offline check), full suite 499 OK, offline check 10/10 PASS, live test passed with `am_adam` (Ed). Kept below as a record.

At the time of writing (2026-10-02 evening), on `feature/phase4b-kokoro`:
- Committed: Kokoro engine (`f0a4c36`), single scrub + espeak-ng (`c1be949`), acoustic self-screen (`978476c`), `wake_barge_in` (`f8c3bc8`), offline check (`b521b47`).
- In review / to commit: Unit 4 (Kokoro in the speech queue, `--tts`, `models fetch`), Kokoro hardening (SHA-256 pins, load-error fallback, vocab check), docs, this plan.
- To do:
  1. **CPU-contention latency test.** First audio was 0.29 s on an idle machine but 2.1–2.2 s while other agents loaded the CPU. Measure default vs `intra_op_num_threads=4` vs spinning off (`session.intra_op.allow_spinning=0`) under synthetic load; change `kokoro._default_session` if one holds ~0.3 s. Then refresh the README sample output with a real run.
  2. Ed: full suite, then a live test — Kokoro voice heard and natural; talk-over still works; `--tts say` and `--no-aec` still work; a confirm flow ("when I finish, say hey jarvis …") grants; nothing self-triggers.
  3. Push, PR, merge (standing approval; no force-push).

## Phase 6 — Jarvis speaks first (S1)

**Why.** Hands-free use breaks down when you can't hear that Claude is done or blocked. Comparable tools announce these moments; undertone doesn't, and typed turns are silent today.

**Design (decide the open points in the spike):**
- **Hooks.** `cli.voice_guard_settings` already installs `SessionStart`, `UserPromptSubmit`, `SubagentStart`/`SubagentStop`, `Stop` and `PreToolUse` hooks for `undertone session`. Add `Stop` and `Notification` entries that run a new `python -m undertone.announce <event> --home … --session …`.
- **Transport.** Recommended: a one-shot `{"type":"announce","session":"<token>","event":"…"}` line on `voice.sock` with **no** `hello`, so it never takes over the attached channel ("latest session wins" is driven by `hello`). `attach` speaks it only if the token matches the attached session. Alternative: route through the channel server. Decide in the spike.
- **Content.** Fixed templates written by undertone, never Claude's text: "Claude finished.", "Claude needs your permission.", "Claude is waiting for you." Everything still goes through `speaker.prepare_speech` and the acoustic screen.
- **Behaviour.**
  - Queue behind current speech; never interrupt.
  - Skip "finished" for a voice turn whose answer was already spoken.
  - After "finished", open the follow-up window (follow-up turns can't grant confirmations).
  - After "needs permission", no window: the answer is at the keyboard. Voice can't approve Claude Code's own permission dialog, by design.
  - A flag on `attach`, e.g. `--announce {all,permission,off}`; pick the default in the spike.

**Spike (≤ 1 h, go/no-go).** Dump real `Stop` and `Notification` hook payloads from the installed Claude Code. The research lists Notification types `permission_prompt`, `idle_prompt`, `agent_needs_input` and `agent_completed`, with `session_id` — confirm them. Go if a typed turn in an `undertone session` produces an announcement within ~1 s of `Stop`.

**Units.** 6.1 `announce` message handling in `voice.py` + tests (token check, dedupe, follow-up window). 6.2 `undertone.announce` hook command + hook wiring in `voice_guard_settings` + tests. 6.3 docs.

**Security review focus.** Who can write to `voice.sock` (same user, 0600) and what an announce message can make undertone say (templates only); that announce turns and follow-ups never grant.

**Spike result (2026-10-02): GO.** Captured live by Ed, Claude Code 2.1.288 (see `docs/architecture.md` → Phase 6):
- `Stop` fires 43–100 ms after the final message (7 samples); an Esc-interrupted turn fires none. Its payload also carries `background_tasks` and `session_crons`, which the hooks docs don't list.
- `Notification` never fired: not for a forced permission prompt (inside cmux, and with cmux's hook injection off), and not after 250 s idle inside cmux. `PermissionRequest` fired on every prompt.
- Projected `Stop` to first audio ≈ 0.4 s on an idle machine (≤ 0.1 s lag + 0.03 s hook start + ~0.29 s Kokoro first audio); the live test measures it for real.

Design changes from the spike and review: `PermissionRequest` (→ permission / question / plan) and `StopFailure` (→ error) replace `Notification`, so there's no idle announcement; the hooks are async and output-free; `Stop` stays silent while a background subagent is running; the transport is the one-shot announce line on `voice.sock`, with the session token added to `hello`; announcements are dropped while the listener isn't idle; "finished" is skipped after a spoken non-ack reply and while a confirm is pending.

**Units:**
- ✓ 6.1 (`5f0ff15`): announce handling in `voice.py`, `session` in the channel's `hello`, `attach --announce`, tests.
- ✓ 6.2 (`3a7a6eb`): the `undertone.announce` hook and its wiring in `voice_guard_settings`, tests.
- 6.3: docs (README, CHANGELOG, SECURITY, architecture, this plan).
- ✓ Live test by Ed: passed 2026-10-03.

## Phase 7 — menu-bar helper (S2–S3)

**Why.** Today the terminal app holds the microphone permission, nothing restarts `attach` after a crash or re-login, and there's no visual state, mute or push-to-talk.

**Design.**
- `macos/UndertoneHelper/`: a SwiftUI `MenuBarExtra` app, `LSUIElement`, with `NSMicrophoneUsageDescription` in its Info.plist. **Without that key, macOS denies a child's mic request silently** (the child's request is charged to the parent app).
- The helper spawns and supervises `undertone attach` (restart with backoff) and shows listening / speaking / muted / stopped; menu items Start/Stop, Mute, Voice (Kokoro / say), Quit.
- A control socket in `attach`, `<home>/control.sock` (0600), JSON lines: `status`, `mute`, `unmute`, `ptt_down`, `ptt_up`, with state events pushed back. **Mute is real:** mic frames are dropped before the wake word, VAD and STT, not merely ignored.
- Push-to-talk: a global hotkey (Carbon `RegisterEventHotKey`; verify it needs no Accessibility or Input Monitoring permission). `ptt_down` opens a turn via `listen_now`; the turn's `via` is `"ptt"`, which **does not grant confirmations** unless Ed decides otherwise after security review (any same-user process could write to the control socket).
- A LaunchAgent starts the **helper** at login, not Python (a launchd-started Python has no prompt path for the mic).
- Signing: ad-hoc for local use; Developer ID + notarization only if it's ever distributed (open decision 1).

**Spike (first hour of S2, go/no-go, from the research).** A ~60-line Swift app that runs `uv run undertone attach` with Start/Stop and alive/dead in the menu bar. Go if: exactly one mic prompt, naming the helper; System Settings lists the helper, not Terminal; the mic still works with Terminal quit; no new prompt after re-login; a `kill -9` of the child is restarted in under 5 s.

**Units.** 7.1 control socket in `attach` (Python) + tests. 7.2 Swift helper: supervise, menu, status. 7.3 push-to-talk + mute end to end. 7.4 `undertone helper install|uninstall` (LaunchAgent) + docs.

## Phase 8 — app hub v1 (S4–S5)

### 8a — voice-safety tier for app tools (S4)

Today `voice_guard.UNGATED_TOOLS` lets only built-in read tools and `mcp__undertone__reply` through on a voice turn; **every** other MCP tool is denied until a spoken confirm, even read-only ones like `cast_cost` or a calendar read.

- **Read-only MCP tier.** An explicit, in-code allowlist of exact tool names that run ungated on a voice turn (for example the `cast-record` read tools and the calendar/reminder read tools chosen in 8b). It lives in code, not a user config file, because a typed turn could edit a config file to widen the voice gate.
- **Undertone-written confirm prompts.** Today Claude writes the spoken confirm prompt, so content injected from a document or message could make a harmful action sound harmless. When `voice_guard` denies a call, it records the pending tool and a short, sanitized summary (tool name, the first arguments or file path) in session state; when the confirm prompt is spoken, undertone appends "This will run: <summary>." from that state, through `prepare_speech` and the screen.
- **Spike.** List the exact tool names `cast-record` and the 8b candidates expose; classify each read-only vs side-effect.

### 8b — first integrations (S5)

Starter set from the research (low risk, most value):
1. **Calendar + Reminders** via `PsychQuant/che-ical-mcp` (MIT, EventKit). Vet it locally first: read the code, the permissions it asks for, its read-only mode. Reads ungated; writes need a confirm.
2. **Music** via pre-authored Shortcuts ("Undertone Play", "Pause", "Next") run with `shortcuts run "<name>"`, allowlisted by exact name in `voice_guard`'s Bash rules. Decide ungated vs confirm (open decision 4).
3. **Home scenes** via pre-authored Shortcuts, **never locks or the garage**; confirm required.
4. **CAST by voice:** "Jarvis, what did I spend today?" through the `cast-record` read tools.

Deferred: **Mail and Messages.** Third-party text enters the model context (high injection exposure) and sends are irreversible. If ever added: read-only or draft-only, behind the confirm gate.

Note: Automation and Full Disk Access grants land on the process hosting Claude Code (the terminal), not on undertone or the helper.

## Backlog (after the weekend)

| Item | Notes |
|---|---|
| Speaker verification as a third confirm factor | WeSpeaker CAM++ ONNX (~28 MB, 0.80% EER on VoxCeleb1-O) on the existing onnxruntime; weak on 1–3 s audio; not anti-spoofing. Spike: owner vs other voices vs Kokoro/`say`/replay; go if FAR ≤ 0.1% at FRR ≤ 15% on 2 s owner audio and p95 < 100 ms. Licence decision first (open decision 2). |
| Multi-session routing by voice | "Jarvis, switch to the undertone session." The research found no tool that ships it. |
| Transcript history | Local, 0600, opt-in. |
| Headphones / AirPods mode | Bluetooth latency vs `SELF_WAKE_GUARD_S`; HFP mic quality. Needs hardware testing. |
| Follow-up window after a confirm | An `answer` reply sent while a confirm is pending still opens the follow-up window (pre-Phase 6 behaviour); anything heard there uses up the challenge. It can't grant. Fix: gate that window on the confirm-pending flag. Found in the Phase 6 security review. |
| cast-desktop voice status panel | Read-only, over the Phase 7 control socket. |

## Open decisions for Ed

1. Helper signing: ad-hoc (local only) or Developer ID + notarization?
2. Speaker-verification weights: WeSpeaker/TitaNet are CC-BY-4.0 (attribution, commercial OK, not MIT/Apache); SpeechBrain ECAPA is Apache-2.0 but needs a one-off PyTorch export to ONNX.
3. Push-to-talk turns: non-granting (default) or allowed to confirm?
4. Music control by voice: ungated or confirm?
5. ✓ Decided (Ed, 2026-10-02): Announcement defaults — `--announce all` (default), follow-up window after "finished" on, silent while `background_tasks` lists a `subagent` with status `running`.

## Kickoff blocks

Paste one per session into a fresh session in `~/Projects/personal/undertone`.

**S0 (finish 4b, if not done):**
```
Resume undertone Phase 4b close-out. Read docs/plan-phases-6-8.md (S0). PRE-FLIGHT: git fetch && git status on feature/phase4b-kokoro; uv run python -m unittest discover -s tests (Ed's side for socket tests).
THEN: CPU-contention latency test for Kokoro first audio (default vs 4 threads vs no spinning, under synthetic load); fix kokoro._default_session if a config holds ~0.3 s; refresh the README sample block from a real run; Ed's full suite + live test; PR + merge.
```

**S1 (Phase 6):**
```
Start undertone Phase 6 (Jarvis speaks first). Read docs/plan-phases-6-8.md (Phase 6) and docs/research/2026-10-02-jarvis/q3b-comparables.md. PRE-FLIGHT: main up to date and clean; tests green.
THEN: 1 h spike — dump real Stop/Notification hook payloads; decide the announce transport (one-shot announce line on voice.sock vs via the channel). Bring Ed the design before building. Ceremony: native plan mode, backend-writer per unit, code-reviewer + security, mutation-test controls, verify commits with git.
```

**S2 (Phase 7a):**
```
Start undertone Phase 7 (menu-bar helper). Read docs/plan-phases-6-8.md (Phase 7) and docs/research/2026-10-02-jarvis/q1-desktop-app.md. PRE-FLIGHT as usual.
THEN: the 1 h Swift spike and its go/no-go list (mic prompt names the helper; works with Terminal quit; survives re-login; kill -9 restart < 5 s). Ed runs the GUI steps. If go: units 7.1 (control socket) and 7.2 (helper core).
```

**S3 (Phase 7b):**
```
Continue undertone Phase 7: push-to-talk (via "ptt", non-granting unless Ed decided otherwise), real mute, LaunchAgent install/uninstall, docs. Security review on the control socket and ptt turns. Live test by Ed.
```

**S4 (Phase 8a):**
```
Start undertone Phase 8a. Read docs/plan-phases-6-8.md (Phase 8) and docs/research/2026-10-02-jarvis/q2-app-bridges.md. Spike: list exact tool names of cast-record and the 8b candidates; classify. Build the in-code read-only MCP tier and undertone-written confirm prompts in voice_guard. Security review end to end through the real channel and hook commands.
```

**S5 (Phase 8b):**
```
Continue undertone Phase 8b: vet che-ical-mcp locally, wire Calendar/Reminders (reads ungated, writes confirmed), Music and Home-scene Shortcuts (allowlisted by exact name), CAST-by-voice. Mail/Messages stay deferred. Live test by Ed.
```

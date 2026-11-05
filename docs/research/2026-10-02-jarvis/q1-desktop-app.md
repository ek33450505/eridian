# Q1: Desktop app for eridian? (2026-10-02)

## Recommendation
- Do (b) first, scoped small: a signed menu-bar helper .app (Swift, LSUIElement) that owns the mic TCC grant, spawns/supervises `eridian attach` as a child, shows state, mute, start/stop. A stable bundle identity is the real win: TCC is granted once to the bundle and survives `uv` / Python upgrades, instead of to "Terminal" or a bare python binary.
- Reject (a) alone: a launchd-started bare Python has no UI to host the mic prompt, gets /dev/null as stdin, and is its own TCC subject (a per-binary row that changes whenever the interpreter path changes). It works only if the grant already exists.
- Reject (d) for now: bundling Python + MLX + onnxruntime + Metal shaders means signing every nested .so/.dylib, hardened-runtime entitlements (disable-library-validation, allow-unsigned-executable-memory), a large bundle and an update story. Cost far exceeds benefit for a single-user tool.
- (c) is viable but second choice: cast-desktop is an observability app with a different lifecycle. Coupling an always-on mic daemon to a dev dashboard that gets quit/rebuilt is a poor fit. Keep it as a later read-only status panel fed by the same socket.
- Always-on (a launchd LaunchAgent) can still be layered: launchd starts the helper app (not Python), and the helper supervises Python.

## Comparison table
| | (a) CLI + LaunchAgent | (b) Swift menu-bar helper | (c) cast-desktop Voice panel | (d) Full bundle |
|---|---|---|---|---|
| Mic TCC holder | python binary itself (launchd = its own responsible process); no prompt UI | helper bundle (responsible process of child, if the Info.plist has NSMicrophoneUsageDescription) | cast-desktop bundle (needs NSMicrophoneUsageDescription + audio-input entitlement added; Tauri re-signs sidecars with app entitlements) | the .app |
| First-grant prompt | none/ silent denial likely unless pre-granted via terminal run | standard system prompt attributed to helper | prompt attributed to cast-desktop | prompt attributed to app |
| Signing/notarize | none | trivial (Swift, ad-hoc or Developer ID, local use needs no notarization) | already has Tauri signing path | hard: nested native libs, hardened runtime entitlements, notarytool failures common |
| Size | 0 | ~1-3 MB | already shipped | hundreds of MB (est., not measured) |
| Crash restart | launchd KeepAlive (+ThrottleInterval) | helper restarts child; LaunchAgent restarts helper | app must be running; Tauri process supervision to write | helper-in-app |
| Sleep/wake, AirPods | no UI; relies on PortAudio rebuilding stream | same pipeline; helper can restart child on wake / default-device change notifications (CoreAudio) | same, in Rust | same |
| New IPC/security surface | none | one new local control socket (0600, same user) or reuse | socket + a Tauri capability/IPC allowlist; an app that holds mic and renders web content | socket |
| Effort | 0.25 session | 1-1.5 sessions | 1.5-2 sessions | 3+ sessions |

## TCC attribution findings
- Child of a signed app: macOS charges TCC requests to the responsible process (the app), not the child; if the app's Info.plist lacks NSMicrophoneUsageDescription, access is silently denied with no dialog (VS Code issue). Fix: usage key in the helper's plist; `responsibility_spawnattrs_setdisclaim` is the opt-out when the child should be its own subject.
- launchd-started Python: becomes its own responsible process; stdin is /dev/null so any interactive prompt path crashes (whisper-local issue). Their working fix is exactly option (b): a small LSUIElement bundle with NSMicrophoneUsageDescription, spawning the real process, launched by the LaunchAgent.
- Tauri: needs Info.plist NSMicrophoneUsageDescription merged in + entitlements.plist with com.apple.security.device.audio-input wired in tauri.conf.json; externalBin sidecars are re-signed with the app's entitlements (several projects' PRs below). cast-desktop currently has no mic key (grep of tauri.conf.json found none).

## First spike for (b), ~1 h
Build: ~60-line Swift LSUIElement app, Info.plist with NSMicrophoneUsageDescription, ad-hoc signed. It does `Process()` launch of `uv run eridian attach` (absolute paths, own process group) and shows a menu-bar item with Start/Stop and the child's alive/dead state. Skip UI states for now.
Go/no-go measurements (all must pass):
1. Launch helper from Finder (not terminal); `eridian attach` triggers exactly one mic prompt naming the helper; after Allow, `tccutil`/System Settings lists the helper, not Terminal/python.
2. Quit Terminal entirely; mic still captures (RMS of frames > noise floor).
3. Reboot/relogin via LaunchAgent for the helper: mic works with no new prompt.
4. kill -9 the child: restarted <5 s. `uv` upgrade of Python does not re-prompt.
Fail -> fall back to disclaim approach or (a) with pre-granted terminal.
Note: eridian's existing unix socket (voice.py:236-246, 0600) is the Claude MCP channel; I found no mute/status control surface in voice.py/cli.py (grep), so a state/mute API is new work (small JSON line protocol on a second 0600 socket). Push-to-talk global hotkey needs Input Monitoring or Accessibility permission, another TCC row for the helper; defer past spike.

## Sources
- https://github.com/microsoft/vscode/issues/307364 (responsible process, silent denial without usage key; disclaim) - fetched
- https://github.com/drajb/whisper-local/issues/19 (launchd Python, /dev/null stdin, wrapper bundle fix) - fetched
- https://github.com/manaflow-ai/cmux/issues/1325 (audio-input entitlement for child processes) - search snippet only
- https://github.com/felixfoertsch/macos-permission-helper - search snippet only
- https://v2.tauri.app/distribute/macos-application-bundle/ (Info.plist merge) - search snippet only
- https://github.com/tauri-apps/tauri/issues/9928 (mic from Rust on macOS) - snippet only
- https://github.com/SolenesInc/attn/pull/319 and https://github.com/dsh-tauri-desk/deepseek-harness-desktop/pull/536 (Tauri mic Info.plist + entitlements, sidecar re-sign) - snippets only
- https://developer.apple.com/forums/thread/744471 (py2app signing/notarization) - snippet only
- https://stupidtech.io/2025/03/15/how-to-make-a-python-script-into-a-macos-app-with-py2app-code-sign-notarize/ - snippet only
- https://github.com/r3dbars/transcripted/pull/1771 (AirPods wake mic restart) - snippet only
- https://github.com/omnesis-dev/Omnesis/pull/215 , https://github.com/griffinwork40/agent-afk/pull/1666 (launchd KeepAlive/ThrottleInterval) - snippets only
- Local: /Users/edkubiak/Projects/personal/cast-desktop/CLAUDE.md; eridian src/eridian/voice.py:236-246

## Not verified
- No Apple primary doc fetched on responsible-process rules (only third-party issues); no hands-on test of any option; no audio run.
- MLX/onnxruntime bundle size, metallib bundling and notarization behavior: searches returned nothing specific; size estimate is a guess.
- Whether `sounddevice`/PortAudio survives AirPods connect/sleep-wake without stream rebuild in eridian (not tested; not read in audio.py/duplex.py).
- Whether a bare `uv run` child inherits attribution under a non-notarized ad-hoc-signed helper on macOS 26 (spike item 1).
- cast-desktop's tauri.conf.json mic setup only grep-checked; src-tauri plist not examined in depth.
- Global-hotkey permission requirement (Input Monitoring vs Accessibility) not researched.
- No check that eridian lacks an existing status/mute API beyond a keyword grep.

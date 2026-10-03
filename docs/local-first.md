# Local-First: Where Your Data Goes

This is the deep version of the README's lead section. undertone's promise is: **no audio leaves the Mac, and there are no API keys and no cloud STT/TTS fallbacks.**

## Data-Flow Table

| Stays on your Mac | Goes to Anthropic | Never exists |
|---|---|---|
| Mic audio; wake word (pymicro-wakeword); VAD (Silero, 32 ms chunks); STT (parakeet-mlx, float32 in memory); TTS (Kokoro-82M via onnxruntime, rendered in memory; or macOS `say`, rendered to a private temp file that is deleted after reading); G2P for Kokoro (espeak-ng CLI, GPL-3.0, subprocess only); AEC (livekit WebRTC AudioProcessingModule, local-only); all models | The transcribed text of each turn, plus normal Claude Code traffic: the contents of files Claude reads (working directory only) and its WebSearch queries, which run server-side | API keys of undertone's own (it uses however your `claude` is logged in); audio uploads; cloud STT/TTS fallbacks |

## Every Network Touchpoint, Listed Honestly

### 1. **`undertone models fetch`**
A one-time Hugging Face download includes:
- `mlx-community/parakeet-tdt-0.6b-v3` (STT, ~2.5 GB): model file 2,508,288,736 bytes (`model.safetensors`)
- `onnx-community/Kokoro-82M-v1.0-ONNX` (TTS, pinned revision `1939ad2a8e416c0acfeecc08a694d14ef25f2231`, Phase 4b): fp32 `onnx/model.onnx`, tokenizer, and voice files. Each is checked against a SHA-256 pinned in `src/undertone/kokoro.py` whenever it loads from the cache

In normal operation, `stt.py` does `os.environ.setdefault("HF_HUB_OFFLINE", "1")` at import time, before parakeet is imported. `kokoro.py` does the same before importing onnxruntime. That keeps the Hub offline on every start, unless you set `HF_HUB_OFFLINE` yourself. Without that flag, `from_pretrained` contacts the Hub on every startup even with a warm local cache (discovered in Phase 0).

The `fetch_model()` function temporarily lifts the offline flag ONLY for the one-time download; loading never contacts the Hub again. At runtime, the model is loaded with `local_files_only=True`.

### 2. **The `claude` Child** (Path B) and **Claude Code Session** (Path A)
In Path B, the persistent `claude -p` process in `undertone chat` connects to api.anthropic.com. In Path A, a running Claude Code session receives voice transcripts via the MCP channel. Both authenticate however your `claude` already does; undertone adds no key of its own. Transcribed text and anything Claude reads from your files leave here.

### 2b. **onnxruntime Telemetry (Phase 4b)**
onnxruntime 1.30's macOS wheel embeds Microsoft's 1DS telemetry client (`mobile.events.data.microsoft.com`). `src/undertone/kokoro.py` FORCES `ORT_DISABLE_TELEMETRY=1` before onnxruntime is imported and calls `onnxruntime.disable_telemetry_events()`. With the variable set, ORT no longer even initializes its telemetry device ID. The force-disable is verified in the offline check.

### 3. **Optional: `training/` Docker Image**
The Docker image `training/Dockerfile` (optional — the default "hey jarvis" is pretrained):
- At **build time:** git-clones openWakeWord v0.6.0 and piper-sample-generator v2.0.0 from GitHub
- At **build time:** wgets checkpoints for embedding and melspectrogram models from GitHub
- During **training:** downloads datasets and phonemizer data

This only happens if you build or run the image. undertone's core (`undertone chat`) does not download any training data.

### 4. **Nothing Else**
undertone itself has no telemetry, no update checks and no fallback services. Traffic from the `claude` child is Claude Code's own (including any telemetry it sends), and Claude Code's settings govern it.

## Verify It Yourself

### (a) Offline Pipeline Check
Run `bash scripts/verify-offline.sh`.

**What it does:**
1. Generates a "Hey Jarvis. [[slnc 600]] What is the capital of France? [[slnc 1500]]" clip with `say -o … --data-format=LEI16@16000` (16 kHz mono PCM, 16-bit)
2. Runs `.venv/bin/python scripts/offline_check.py <clip> <out.aiff>` inside a macOS `sandbox-exec` profile that **denies all network access** with the policy `(version 1)(allow default)(deny network*)`
3. Inside that sandbox, feeds the real wake word detector, VAD, and STT through `Listener` with a simulated clock
4. Calls `say -o` to render a reply to an AIFF file
5. **Echo cancellation check (Phase 4):** Simulates room echo on the rendered reply and runs it through the real `AudioProcessingModule` (WebRTC AEC3). Asserts ERLE ≥ 20 dB and that residual speech is too short to trigger talk-over.
6. **Kokoro check (Phase 4b):** Asserts that importing `undertone.kokoro` forced `ORT_DISABLE_TELEMETRY=1`, then renders a fixed sentence from the local model cache (espeak-ng → onnxruntime), reports the time to first audio, and writes it to `<out>-kokoro.wav` (16 kHz mono 16-bit).
7. **Kokoro self-screen check (Phase 4b):** Renders "hey jarvice" (a homophone that passes the text scrub) and an ordinary sentence with Kokoro. A fresh `ScreenedSink` with the real wake detector must block the first and play the second.
8. Asserts:
   - Network canary fails (TCP to 1.1.1.1 and DNS lookup both blocked)
   - Wake word fired exactly once
   - Transcript contains "capital of france" (case-insensitive, punctuation-ignored)
   - All renders exit code 0 and wrote bytes to output files

**Verified 2026-10-02 (M4 Max):**
- Wake fired: ✓
- Transcript matched: ✓
- `say` render rc 0; Kokoro rendered 2 pieces: ✓
- Inside the sandbox, TCP/DNS blocked: ✓
- Echo cancellation: ERLE 43.9 dB, talk-over check passed: ✓
- Kokoro telemetry off: ✓
- Kokoro self-screen: homophone blocked, ordinary sentence played: ✓
- **Final:** `OFFLINE CHECK: PASS (wake, VAD, STT, TTS, Kokoro, self-screen, echo cancellation ran with network denied)`

**Why not Docker `--network none`?**
MLX (the ML framework) and macOS `say` are macOS-only; Docker can't run them.

**Platform note:**
Apple has deprecated `sandbox-exec`. It is still present and working on macOS 26.6, verified 2026-10-01; older versions are untested.

### (b) Live Connection Check During `undertone chat`
While running `undertone chat`:

```bash
# Find the PIDs. `uv run` starts a uv process and then undertone's Python
# process, and both match. -n picks the newest one, which is the Python process.
undertone_pid=$(pgrep -nf 'undertone chat')
children=$(pgrep -d, -P "$undertone_pid")   # the claude child, plus any say/afplay in flight

# Show open sockets for each
lsof -a -i -n -P -p "$undertone_pid"   # undertone Python process: expect no output
lsof -a -i -n -P -p "$children"        # children: expect only :443 to Anthropic
nettop -p "$undertone_pid"             # optional: live per-process traffic
```

**Measured on M4 Max (2026-10-01):**
- undertone Python process: **zero sockets**
- `claude` child: TCP :443 to 160.79.104.10 and 2607:6bc0::10 only

**Verify the IP owner:**
```bash
whois 160.79.104.10          # → Organization: Anthropic, PBC (NetRange 160.79.104.0/21)
whois 2607:6bc0::10          # → Organization: Anthropic, PBC (NetName ANTHROPIC-V6)

dig +short api.anthropic.com # confirms these IPs
```

This is a snapshot of one session, not a proof for all sessions. Run it yourself on your machine.

## What "Local-First" Does NOT Cover

"Local-first" here covers the audio. The **text** of each turn still goes to Anthropic, because the brain is Claude. A fully local loop is Phase 5 (`--offline`, planned), an opt-in local brain (Ollama `gpt-oss:20b`).

## Privacy Notes

### Microphone Access
Your terminal app needs microphone permission (macOS → System Settings → Privacy & Security → Microphone).

### Audio Buffering and Storage
- Audio is buffered in RAM only after the wake word fires, during the 6-second follow-up window, or during talk-over playback
- TTS audio is rendered into a transient temp directory (mode 0700) created with `tempfile.mkdtemp(prefix="undertone-tts-")`, validated for format (1 channel, 16-bit, 16 kHz), read into the playback buffer, and immediately unlinked. The directory is deleted when `RenderedSpeechQueue` closes.
- Playback audio buffers in RAM (~32 KB/s of speech); replies up to 64 KB of text can buffer ~10 MB in memory at most
- Nothing else is recorded to disk
- The session ID is stored in `~/.undertone/chat_session` (mode 0600) for `--resume`

### Claude Code Logs
The `claude` child's stderr goes to `~/.undertone/logs/brain.log` (mode 0600). Claude Code keeps its own transcripts under `~/.claude/projects/` as usual.

### File Reads
When Claude uses Read/Glob/Grep, it sees only files under the working directory (enforced by `--restricted` and `--tools Read,Grep,Glob,WebSearch`).

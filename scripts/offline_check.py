"""Prove undertone's audio pipeline runs with the network denied.

Run inside a network-denying sandbox (see verify-offline.sh):

    offline_check.py <clip.wav> <out.aiff> [--expect "text"]

Feeds a recorded clip through wake word -> VAD -> STT, then TTS (`say -o` to a
file, never the speakers), then echo cancellation (a rendered sentence plus a
simulated room echo through the real WebRTC AEC). A network canary runs first so the check proves its
own premise: if the network is reachable, the result is meaningless.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import socket
import subprocess
import sys
import wave

FRAME_SECONDS = 0.01
TRAILING_SILENCE_SECONDS = 2


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.casefold())).strip()


def network_canary() -> list[str]:
    """Return a list of ways the network is reachable (empty == denied)."""
    reachable = []
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=2).close()
        reachable.append("TCP connect to 1.1.1.1:443 succeeded")
    except OSError:
        pass
    try:
        socket.getaddrinfo("example.com", 443)
        reachable.append("DNS lookup of example.com succeeded")
    except OSError:
        pass
    return reachable


def check_echo_cancellation(out: str, check, vad_cls) -> None:
    """Prove WebRTC AEC (livekit) runs locally: ERLE >= 20 dB and the VAD stays quiet."""
    import numpy as np

    from undertone import duplex, speaker
    from undertone.listener import ListenerConfig

    text = (
        "The quick brown fox jumps over the lazy dog while the morning sun rises "
        "slowly over the quiet hills and the river runs on."
    )
    wav_path = os.path.splitext(out)[0] + "-aec.wav"
    argv = speaker.build_say_argv(text, out_path=wav_path)
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(wav_path):
        check("echo cancellation", False, f"say rc={proc.returncode} {proc.stderr.strip()}".strip())
        return
    with wave.open(wav_path) as w:
        far = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)

    rng = np.random.default_rng(0)
    rir = np.zeros(int(0.045 * 16000) + int(0.15 * 16000))
    rir[int(0.045 * 16000)] = 0.5
    tail_n = np.arange(int(0.15 * 16000))
    rir[int(0.045 * 16000) :] += 0.08 * rng.standard_normal(len(tail_n)) * np.exp(-tail_n / (0.04 * 16000))
    echo = np.convolve(far, rir)[: len(far)] + 30 * rng.standard_normal(len(far))
    far_i = far.astype(np.int16)
    near_i = np.clip(echo, -32768, 32767).astype(np.int16)

    from livekit.rtc import AudioFrame

    apm = duplex._default_apm_factory(True)
    step = 160
    residual = bytearray()
    for i in range(0, len(far_i) - step + 1, step):
        apm.set_stream_delay_ms(45)
        apm.process_reverse_stream(AudioFrame(far_i[i : i + step].tobytes(), 16000, 1, step))
        frame = AudioFrame(bytearray(near_i[i : i + step].tobytes()), 16000, 1, step)
        apm.process_stream(frame)
        residual += bytes(frame.data)
    del apm  # livekit complains if it outlives the interpreter

    skip = 16000  # let AEC3 converge for the first second
    n = len(residual) // 2
    res = np.frombuffer(bytes(residual), dtype=np.int16).astype(np.float64)
    raw = near_i[:n].astype(np.float64)
    erle = 10 * np.log10((np.mean(raw[skip:] ** 2) + 1e-9) / (np.mean(res[skip:] ** 2) + 1e-9))

    vad = vad_cls()
    chunk = 1024
    pcm = bytes(residual)[skip * 2 :]
    chunks = [pcm[i : i + chunk] for i in range(0, len(pcm) - chunk + 1, chunk)]
    probs = [vad(c) for c in chunks]
    fired = sum(1 for p in probs if p >= 0.5)
    cfg = ListenerConfig()
    needed = math.ceil(cfg.talk_over_min_s / (1024 / 2 / 16000) - 1e-9)  # as in listener.py
    longest_run = run = 0
    for p in probs:
        run = run + 1 if p >= cfg.talk_over_threshold else 0
        longest_run = max(longest_run, run)
    check(
        "echo cancellation",
        erle >= 20 and longest_run < needed,
        f"ERLE {erle:.1f} dB (need >= 20); longest speech-like run {longest_run} chunks "
        f"(talk-over needs {needed}); VAD>=0.5 on {fired}/{len(chunks)} chunks",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("clip")
    parser.add_argument("out")
    parser.add_argument("--expect", default="capital of france")
    args = parser.parse_args()

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
        return ok

    print("offline check")
    leaks = network_canary()
    if leaks:
        check("network canary", False, "network is reachable - not a valid offline test (" + "; ".join(leaks) + ")")
        print("RESULT: FAIL")
        return 1
    check("network canary", True, "TCP and DNS both blocked")

    try:
        from undertone.cli import _make_wake_factory
        from undertone.listener import FRAME_BYTES, Listener
        from undertone.stt import ModelNotCachedError, ParakeetSTT
        from pysilero_vad import SileroVoiceActivityDetector
    except ImportError as exc:
        print(f"  [FAIL] imports - {exc} (run `uv sync`)")
        print("RESULT: FAIL")
        return 1

    with wave.open(args.clip) as w:
        fmt_ok = w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        if not check("clip format", fmt_ok, "16 kHz mono 16-bit" if fmt_ok else "need 16 kHz mono 16-bit PCM"):
            print("RESULT: FAIL")
            return 1
        pcm = w.readframes(w.getnframes())
    pcm += b"\x00" * (16000 * 2 * TRAILING_SILENCE_SECONDS)  # lets end-of-turn fire

    now = [0.0]
    events = []
    vad = SileroVoiceActivityDetector()
    try:
        listener = Listener(
            wake_factory=_make_wake_factory(),
            vad=lambda chunk: vad(chunk),
            stt=ParakeetSTT(),
            clock=lambda: now[0],
            on_event=events.append,
            vad_reset=vad.reset,
        )
        for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
            listener.process_frame(pcm[i : i + FRAME_BYTES])
            now[0] += FRAME_SECONDS
    except ModelNotCachedError as exc:
        print(f"  [FAIL] models - {exc}")
        print("  Run `uv run undertone models fetch` first, OUTSIDE the sandbox.")
        print("RESULT: FAIL")
        return 1

    wakes = [e for e in events if e.kind == "wake"]
    utts = [e.text for e in events if e.kind == "utterance"]
    check("wake word", len(wakes) == 1, f"{len(wakes)} wake event(s)")
    expect = _normalize(args.expect)
    heard = next((u for u in utts if expect in _normalize(u or "")), None)
    check("VAD + STT", heard is not None, f"heard {utts!r}, expected to contain {args.expect!r}")

    say_text = "You asked: " + (heard or utts[0] if utts else "nothing")
    proc = subprocess.run(["say", "-o", args.out, "--", say_text], capture_output=True, text=True)
    check("TTS exit", proc.returncode == 0, f"say rc={proc.returncode} {proc.stderr.strip()}".strip())
    size = os.path.getsize(args.out) if os.path.exists(args.out) else 0
    check("TTS output", size > 0, f"{size} bytes")

    check_echo_cancellation(args.out, check, SileroVoiceActivityDetector)

    ok = all(r[1] for r in results)
    print("RESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

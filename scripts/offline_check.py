"""Prove undertone's audio pipeline runs with the network denied.

Run inside a network-denying sandbox (see verify-offline.sh):

    offline_check.py <clip.wav> <out.aiff> [--expect "text"]

Feeds a recorded clip through wake word -> VAD -> STT, then TTS (`say -o` to a
file, never the speakers). A network canary runs first so the check proves its
own premise: if the network is reachable, the result is meaningless.
"""

from __future__ import annotations

import argparse
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

    ok = all(r[1] for r in results)
    print("RESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

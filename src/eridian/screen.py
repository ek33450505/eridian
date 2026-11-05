"""Acoustic self-screen: never play audio our own wake detector would fire on.

Threat: eridian's wake detector stays live while replies play. If our own
speech fires it, the listener opens a `via: "wake"` turn, and only "wake" turns
can grant a voice confirmation -- so a self-trigger is a trusted turn the user
never asked for.

Why acoustics beats spelling: scrubbing the reply text for the wake phrase
misses homophones. With the Kokoro voice, "hey jarviss", "djarvis", "jarvice",
"jarvus" and "charvis" fire the detector on 4/4 voices. What matters is how the
audio sounds, so we run the real detector over our own rendered PCM before it
reaches the speaker (about 0.9 ms of compute per second of audio).

Why history + lookahead: the detector fires 50-250 ms AFTER the speech ends,
and fed the speech with no trailing audio it often does not fire at all (the
default voice needs >= 0.1 s more, am_adam >= 0.3 s). So each chunk is screened
by a fresh detector over [recent approved audio + modeled gap silence + chunk +
LOOKAHEAD_S of silence]. History catches a phrase split across chunks; the
lookahead catches one that ends a chunk. A fresh detector costs about 1 ms.

Calibration (2026-10-02 spike): the live detector fires at its model cutoff,
0.97. The screen uses a stricter 0.8: 0 of 200 ordinary sentences would be
blocked while 66 of 80 homophone attack clips are caught.

`ScreenedSink` wraps a `player.PlaybackSink`; blocked audio never reaches the
inner sink.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

SCREEN_CUTOFF = 0.8  # live cutoff is 0.97; 0.8 blocks 0/200 ordinary sentences, catches 66/80 attacks
HISTORY_S = 2.0  # context of previously approved audio
LOOKAHEAD_S = 1.0  # silence appended when screening; max observed detector lag is 254 ms

_FRAME_BYTES = 320  # 10 ms of 16 kHz mono int16
_BYTES_PER_S = 32000
_HISTORY_BYTES = int(HISTORY_S * _BYTES_PER_S)
_LOOKAHEAD_BYTES = int(LOOKAHEAD_S * _BYTES_PER_S)


def _silence(seconds: float) -> bytes:
    n = int(seconds * _BYTES_PER_S) // _FRAME_BYTES * _FRAME_BYTES
    return bytes(min(n, _HISTORY_BYTES))


def _fires(detector: Callable[[bytes], bool], data: bytes) -> bool:
    for i in range(0, len(data), _FRAME_BYTES):
        if detector(data[i : i + _FRAME_BYTES]):
            return True
    return False


class ScreenedSink:
    def __init__(
        self,
        inner,
        *,
        detector_factory: Callable[[], Callable[[bytes], bool]],
        clock: Callable[[], float] = time.monotonic,
        on_blocked: Callable[[], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._inner = inner
        self._factory = detector_factory
        self._clock = clock
        self._on_blocked = on_blocked
        self._on_error = on_error
        self._error_reported = False
        self._lock = threading.Lock()
        self._history = b""
        self._est_end = 0.0

    def play(self, pcm: bytes) -> bool:
        """Screen and play pcm. False means the chunk was screened out and never
        reached the speaker; callers must not treat it as spoken."""
        if len(pcm) % 2:
            pcm = pcm[:-1]
        if not pcm:
            return True
        padded = pcm + bytes(-len(pcm) % _FRAME_BYTES)
        failure: Exception | None = None
        report_error = False
        with self._lock:
            try:
                now = self._clock()
                history = self._history
                gap = max(0.0, now - self._est_end) if history else 0.0
                if gap >= HISTORY_S:
                    history, gap = b"", 0.0
                context = history + _silence(gap)
                blocked = _fires(
                    self._factory(), context + padded + bytes(_LOOKAHEAD_BYTES)
                )
            except Exception as exc:
                blocked = True  # fail closed: never play audio we could not screen
                failure = exc
            if not blocked:
                self._history = (context + padded)[-_HISTORY_BYTES:]
                self._est_end = max(now, self._est_end) + len(pcm) / _BYTES_PER_S
            if failure is not None and not self._error_reported:
                self._error_reported = report_error = True
        if blocked:
            # A broken screen is not "sounded like the wake word": report it
            # separately, once, and stay silent (but closed) afterwards.
            callback = self._on_blocked if failure is None else self._on_error
            arg = () if failure is None else (failure,)
            if callback is not None and (failure is None or report_error):
                try:
                    callback(*arg)
                except Exception:
                    pass
            return False
        self._inner.play(pcm)
        return True

    def clear(self) -> None:
        # Keep the history: a phrase split around a barge-in must still be caught.
        # Queued-but-unplayed audio is dropped, so the gap counts from now.
        with self._lock:
            self._est_end = min(self._est_end, self._clock())
        self._inner.clear()

    @property
    def playing(self) -> bool:
        return self._inner.playing

    def wait_drained(self, timeout: float | None = None) -> bool:
        return self._inner.wait_drained(timeout)

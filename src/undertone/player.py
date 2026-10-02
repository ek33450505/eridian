"""In-process, strictly sequential queues of speech.

SpeechQueue plays sentences one after another through `say`, so the first
sentence can start while the rest of a reply is still streaming. It is the
`--no-aec` fallback. RenderedSpeechQueue renders each sentence with
`say -o` to 16 kHz mono int16 WAV and hands the PCM to a PlaybackSink, so
undertone plays the audio itself and an echo canceller sees the reference
signal; it renders ahead (the sink buffers) to keep sentences gapless. Each
`say` argv comes from speaker.build_say_argv, the single sanitizing argv
builder, and stop() is there for barge-in.

Test seams: UNDERTONE_SAY_CMD / UNDERTONE_VOICE (via build_say_argv); tests
must point UNDERTONE_SAY_CMD at a stub (see tests/test_player.py).
RenderedSpeechQueue takes its sink as a constructor argument, so tests pass a
fake sink and never open an audio device.
"""

from __future__ import annotations

import collections
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Protocol

from . import speaker

RENDER_TIMEOUT_S = 20.0  # bound on one `say -o` render; a hung render is killed and skipped
_PCM_RATE = 16000  # what build_say_argv(out_path=...) asks `say` to render


class PlaybackSink(Protocol):
    def play(self, pcm: bytes) -> None:
        """Append 16 kHz mono int16 PCM (even length)."""

    def clear(self) -> None:
        """Drop everything not yet played, immediately."""

    @property
    def playing(self) -> bool:
        """True while any appended PCM is unplayed."""

    def wait_drained(self, timeout: float | None = None) -> bool:
        """True once nothing is unplayed."""


class SpeechQueue:
    def __init__(self, on_start: Callable[[str], None] | None = None) -> None:
        self._on_start = on_start
        self._cond = threading.Condition()
        self._items: collections.deque[str] = collections.deque()
        # stop() bumps the generation; the worker re-checks it under the lock
        # right before spawning, so a stop() landing between dequeue and Popen
        # still prevents that item from playing.
        self._generation = 0
        self._proc: subprocess.Popen | None = None
        self._playing = False  # an item is dequeued and not yet finished
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="undertone-speech", daemon=True)
        self._thread.start()

    def enqueue(self, text: str) -> None:
        """Play `text` after everything already queued."""
        with self._cond:
            if self._closed:
                return
            self._items.append(text)
            self._cond.notify_all()

    def stop(self) -> None:
        """Clear pending items and terminate current speech; returns promptly."""
        with self._cond:
            self._generation += 1
            self._items.clear()
            proc, self._proc = self._proc, None
            self._cond.notify_all()
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)

    @property
    def busy(self) -> bool:
        with self._cond:
            return bool(self._items) or self._playing

    def wait_idle(self, timeout: float | None = None) -> bool:
        """True once the queue is empty and nothing is playing."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._items and not self._playing, timeout)

    def close(self) -> None:
        """Stop everything and end the worker thread."""
        self.stop()
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self._thread.join(timeout=3.0)

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._items and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                text = self._items.popleft()
                generation = self._generation
                self._playing = True  # set under the same lock as the pop: no false idle

            proc = self._spawn(text, generation)
            if proc is not None:
                if self._on_start is not None:
                    try:
                        self._on_start(text)
                    except Exception:  # noqa: BLE001 - a bad callback must not kill playback
                        pass
                proc.wait()

            with self._cond:
                self._playing = False
                self._cond.notify_all()

    def _spawn(self, text: str, generation: int) -> subprocess.Popen | None:
        argv = speaker.build_say_argv(text)
        if argv is None:
            return None
        with self._cond:
            if generation != self._generation or self._closed:
                return None  # stop() arrived after we dequeued this item
            try:
                proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                return None
            self._proc = proc
            return proc


class RenderedSpeechQueue:
    def __init__(self, on_start: Callable[[str], None] | None = None, *, sink: PlaybackSink) -> None:
        self._on_start = on_start
        self._sink = sink
        self._cond = threading.Condition()
        self._items: collections.deque[str] = collections.deque()
        # Same generation scheme as SpeechQueue: stop() bumps it, and the worker
        # re-checks it under the lock right before spawning AND right before
        # handing PCM to the sink, so a stop() at either gap prevents playback.
        self._generation = 0
        self._proc: subprocess.Popen | None = None
        self._rendering = False  # an item is dequeued and not yet handed to the sink
        self._closed = False
        self._counter = 0
        # Private (0700) dir for render files; names are ours, never model text.
        self._dir = tempfile.mkdtemp(prefix="undertone-tts-")
        self._thread = threading.Thread(target=self._run, name="undertone-render", daemon=True)
        self._thread.start()

    def enqueue(self, text: str) -> None:
        """Render and play `text` after everything already queued."""
        with self._cond:
            if self._closed:
                return
            self._items.append(text)
            self._cond.notify_all()

    def stop(self) -> None:
        """Clear pending items, kill any render, and flush the sink; returns promptly."""
        with self._cond:
            self._generation += 1
            self._items.clear()
            proc, self._proc = self._proc, None
            self._cond.notify_all()
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)
        self._sink.clear()

    @property
    def busy(self) -> bool:
        with self._cond:
            pending = bool(self._items) or self._rendering
        return pending or self._sink.playing

    def wait_idle(self, timeout: float | None = None) -> bool:
        """True once nothing is queued, rendering, or unplayed in the sink."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            if not self._cond.wait_for(lambda: not self._items and not self._rendering, timeout):
                return False
        # The sink's `playing` flips on the audio thread, not under our condition.
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        return self._sink.wait_drained(remaining)

    def close(self) -> None:
        """Stop everything, end the worker thread, and remove the render dir."""
        self.stop()
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self._thread.join(timeout=3.0)
        shutil.rmtree(self._dir, ignore_errors=True)

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._items and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                text = self._items.popleft()
                generation = self._generation
                self._rendering = True  # set under the same lock as the pop: no false idle
                self._counter += 1
                path = Path(self._dir) / f"{self._counter}.wav"

            pcm = self._render(text, generation, path)
            played = False
            with self._cond:
                if pcm and generation == self._generation and not self._closed:
                    self._sink.play(pcm)
                    played = True
                self._rendering = False
                self._cond.notify_all()
            if played and self._on_start is not None:
                try:
                    self._on_start(text)
                except Exception:  # noqa: BLE001 - a bad callback must not kill playback
                    pass

    def _render(self, text: str, generation: int, path: Path) -> bytes | None:
        """Run `say -o` for `text`; return validated PCM, or None to skip the item."""
        try:
            argv = speaker.build_say_argv(text, out_path=str(path))
            if argv is None:
                return None
            with self._cond:
                if generation != self._generation or self._closed:
                    return None  # stop() arrived after we dequeued this item
                try:
                    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    return None
                self._proc = proc
            try:
                proc.wait(timeout=RENDER_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return None
            if proc.returncode != 0:
                return None
            return _read_pcm(path)
        finally:
            with self._cond:
                self._proc = None
            path.unlink(missing_ok=True)


def _read_pcm(path: Path) -> bytes | None:
    """Frames of a 16 kHz mono int16 WAV, or None if missing/unreadable/wrong format."""
    try:
        with wave.open(str(path), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != _PCM_RATE:
                return None
            return w.readframes(w.getnframes())
    except (OSError, EOFError, wave.Error):
        return None

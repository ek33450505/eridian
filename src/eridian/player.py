"""In-process, strictly sequential queues of speech.

SpeechQueue plays sentences one after another through `say`, so the first
sentence can start while the rest of a reply is still streaming. It is the
`--no-aec` fallback. RenderedSpeechQueue asks a Renderer for 16 kHz mono
int16 PCM pieces per sentence and hands them to a PlaybackSink, so eridian
plays the audio itself and an echo canceller sees the reference signal; it
renders ahead (the sink buffers) to keep sentences gapless. SayRenderer uses
`say -o` (argv from speaker.build_say_argv, the single sanitizing argv builder);
KokoroRenderer synthesizes with a resident Kokoro voice. on_start fires once per
item, after the first piece the sink accepts (a sink returning False screened
it out). stop() is there for barge-in.

enqueue(text, own=True) marks eridian's own words; they get its second voice.

Test seams: ERIDIAN_SAY_CMD / ERIDIAN_VOICE (via build_say_argv); tests
must point ERIDIAN_SAY_CMD at a stub (see tests/test_player.py).
RenderedSpeechQueue takes its sink and renderer as constructor arguments, so
tests pass fakes and never open an audio device or load a model.
"""

from __future__ import annotations

import collections
import contextlib
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Iterator, Protocol

from . import speaker

RENDER_TIMEOUT_S = 20.0  # bound on one `say -o` render; a hung render is killed and skipped
_PCM_RATE = 16000  # what build_say_argv(out_path=...) asks `say` to render


class PlaybackSink(Protocol):
    def play(self, pcm: bytes) -> bool | None:
        """Append 16 kHz mono int16 PCM (even length); False means it was dropped."""

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
        self._items: collections.deque[tuple[str, bool]] = collections.deque()
        # stop() bumps the generation; the worker re-checks it under the lock
        # right before spawning, so a stop() landing between dequeue and Popen
        # still prevents that item from playing.
        self._generation = 0
        self._proc: subprocess.Popen | None = None
        self._playing = False  # an item is dequeued and not yet finished
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="eridian-speech", daemon=True)
        self._thread.start()

    def enqueue(self, text: str, *, own: bool = False) -> None:
        """Play `text` after everything already queued."""
        with self._cond:
            if self._closed:
                return
            self._items.append((text, own))
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
                text, own = self._items.popleft()
                generation = self._generation
                self._playing = True  # set under the same lock as the pop: no false idle

            proc = self._spawn(text, generation, own)
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

    def _spawn(self, text: str, generation: int, own: bool = False) -> subprocess.Popen | None:
        argv = speaker.build_say_argv(text, own=own)
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


class Renderer(Protocol):
    def render(
        self, text: str, wanted: Callable[[], bool], *, own: bool = False
    ) -> Iterator[bytes]:
        """Yield 16 kHz mono int16 PCM pieces for `text`; stop early once not wanted()."""

    def cancel(self) -> None:
        """Abort in-flight work promptly; called from stop() on another thread."""

    def close(self) -> None:
        """Release resources."""


class SayRenderer:
    """Renders with `say -o`: one PCM per item, from a killable child process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._counter = 0
        # Private (0700) dir for render files; names are ours, never model text.
        self._dir = tempfile.mkdtemp(prefix="eridian-tts-")

    def render(
        self, text: str, wanted: Callable[[], bool], *, own: bool = False
    ) -> Iterator[bytes]:
        self._counter += 1  # only the queue's worker thread renders
        path = Path(self._dir) / f"{self._counter}.wav"
        try:
            argv = speaker.build_say_argv(text, out_path=str(path), own=own)
            if argv is None:
                return
            with self._lock:
                if not wanted():
                    return  # stop() arrived after the item was dequeued
                try:
                    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    return
                self._proc = proc
            try:
                proc.wait(timeout=RENDER_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return
            if proc.returncode != 0:
                return
            pcm = _read_pcm(path)
            if pcm:
                yield pcm
        finally:
            with self._lock:
                self._proc = None
            path.unlink(missing_ok=True)

    def cancel(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)

    def close(self) -> None:
        self.cancel()
        shutil.rmtree(self._dir, ignore_errors=True)


class KokoroRenderer:
    """Renders with a shared KokoroVoice, one PCM per phoneme piece.

    Inference cannot be interrupted, so cancel() is a no-op: wanted() and the
    queue's generation check discard stale output. The voice is owned by the
    caller, so close() leaves it alone.
    """

    def __init__(self, voice) -> None:
        self._voice = voice

    def render(
        self, text: str, wanted: Callable[[], bool], *, own: bool = False
    ) -> Iterator[bytes]:
        phonemes = speaker.espeak_phonemes(text)
        if phonemes is None:
            return
        for piece in self._voice.pieces(phonemes):
            if not wanted():
                return
            pcm = self._voice.render(piece, own=own)
            if pcm:
                yield pcm

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


class RenderedSpeechQueue:
    def __init__(
        self,
        on_start: Callable[[str], None] | None = None,
        *,
        sink: PlaybackSink,
        renderer: Renderer | None = None,
    ) -> None:
        self._on_start = on_start
        self._sink = sink
        self._renderer = renderer if renderer is not None else SayRenderer()
        self._cond = threading.Condition()
        self._items: collections.deque[tuple[str, bool]] = collections.deque()
        # Same generation scheme as SpeechQueue: stop() bumps it, the renderer
        # checks it (via wanted()) before starting work, and the worker re-checks
        # it under the lock right before handing each PCM to the sink, so a
        # stop() at any gap prevents playback.
        self._generation = 0
        self._rendering = False  # an item is dequeued and not yet fully handed to the sink
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="eridian-render", daemon=True)
        self._thread.start()

    def enqueue(self, text: str, *, own: bool = False) -> None:
        """Render and play `text` after everything already queued."""
        with self._cond:
            if self._closed:
                return
            self._items.append((text, own))
            self._cond.notify_all()

    def stop(self) -> None:
        """Clear pending items, cancel any render, and flush the sink; returns promptly."""
        with self._cond:
            self._generation += 1
            self._items.clear()
            self._cond.notify_all()
        self._renderer.cancel()
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
        """Stop everything, end the worker thread, and release the renderer."""
        self.stop()
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self._thread.join(timeout=3.0)
        self._renderer.close()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._items and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                text, own = self._items.popleft()
                generation = self._generation
                self._rendering = True  # set under the same lock as the pop: no false idle

            try:
                self._play_item(text, generation, own)
            finally:
                with self._cond:
                    self._rendering = False
                    self._cond.notify_all()

    def _play_item(self, text: str, generation: int, own: bool = False) -> None:
        def wanted() -> bool:
            return generation == self._generation and not self._closed

        started = False
        try:
            with contextlib.closing(self._renderer.render(text, wanted, own=own)) as pieces:
                for pcm in pieces:
                    with self._cond:
                        if not wanted():
                            break
                        accepted = self._sink.play(pcm)
                    # Only an explicit False means the sink dropped it (screened out).
                    if accepted is False or started:
                        continue
                    started = True
                    if self._on_start is not None:
                        try:
                            self._on_start(text)
                        except Exception:  # noqa: BLE001 - a bad callback must not kill playback
                            pass
        except Exception:  # noqa: BLE001 - a failed render must not kill the worker
            pass


def _read_pcm(path: Path) -> bytes | None:
    """Frames of a 16 kHz mono int16 WAV, or None if missing/unreadable/wrong format."""
    try:
        with wave.open(str(path), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != _PCM_RATE:
                return None
            return w.readframes(w.getnframes())
    except (OSError, EOFError, wave.Error):
        return None

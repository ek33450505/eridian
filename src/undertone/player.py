"""An in-process, strictly sequential queue of speech.

SpeechQueue plays sentences one after another, so the first sentence can
start while the rest of a reply is still streaming. Each `say` argv comes from
speaker.build_say_argv, the single sanitizing argv builder, and stop() is
there for barge-in.

Test seams: UNDERTONE_SAY_CMD / UNDERTONE_VOICE (via build_say_argv); tests
must point UNDERTONE_SAY_CMD at a stub (see tests/test_player.py).
"""

from __future__ import annotations

import collections
import subprocess
import threading
from typing import Callable

from . import speaker


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

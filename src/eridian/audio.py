"""Real microphone frame source.

Heavy/optional deps (`sounddevice`) are imported lazily inside functions so
`eridian hook stop` and friends stay fast and dependency-light even when
this module is imported transitively. Not unit-tested beyond import-guarding
-- a real audio stream needs a real mic, which tests must never open.
"""

from __future__ import annotations

import contextlib
import functools
import queue
import threading
import time
from typing import Any, Callable, Iterator

from .listener import FRAME_BYTES, SAMPLE_RATE


def _default_input_factory(callback: Callable[..., None]) -> Any:
    import sounddevice as sd

    return sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=FRAME_BYTES // 2,  # int16 samples, not bytes
        callback=callback,
    )


FRAME_S = FRAME_BYTES / 2 / SAMPLE_RATE  # 10 ms
STALL_S = 0.05  # no device frame for this long: yield a silent one so the consumer keeps ticking
_SILENCE = b"\x00" * FRAME_BYTES
_MAX_LAG_FRAMES = 3  # paced silence that falls further behind than this resets instead of bursting


class MicSource:
    """Mic frames from the default input device, with the device closable on demand.

    `set_input_enabled(False)` closes the input stream (the OS mic indicator
    goes off) and `frames()` keeps yielding silent frames paced at 10 ms, so
    the consumer's loop keeps running (it must, to see an unmute). Enabling
    reopens the device; if that raises, the source stays silent. A stream that
    fails to close is kept and retried (next disable, or close()); its callback
    queues nothing meanwhile. If the device delivers nothing for STALL_S, frames()
    yields a silent frame so a stall or unplug cannot freeze the consumer.
    """

    def __init__(
        self,
        *,
        input_factory: Callable[[Callable[..., None]], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        stall_s: float = STALL_S,
    ) -> None:
        self._input_factory = input_factory or _default_input_factory
        self._sleep = sleep
        self._clock = clock
        self._stall_s = stall_s
        self._lock = threading.Lock()
        self._stream: Any = None
        self._gen = 0  # switch generation; queued frames tagged with another gen are stale
        self._stalled = False
        self._leaked: list[tuple[Any, bool]] = []  # (stream, is_input) whose stop/close raised
        self._live: set[int] = set()  # tokens of streams allowed to queue frames
        self._tokens: dict[int, int] = {}  # id(stream) -> token
        self._next_token = 0
        self._started = False
        self._closed = False
        self._enabled = True
        self._frames: "queue.Queue[bytes]" = queue.Queue()
        self._next_at: float | None = None

    def _callback(
        self, token: int, gen: int, data: bytes, _frame_count: int, _time_info: object, _status: object
    ) -> None:
        # sounddevice's own audio thread; queue.Queue is thread-safe.
        if token in self._live:
            self._frames.put((gen, bytes(data)))

    def _open(self, gen: int) -> Any:
        self._next_token += 1
        token = self._next_token
        self._live.add(token)
        stream = None
        try:
            stream = self._input_factory(functools.partial(self._callback, token, gen))
            stream.start()
        except BaseException:
            self._live.discard(token)
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
            raise
        self._tokens[id(stream)] = token
        return stream

    @property
    def stalled(self) -> bool:
        """True while frames() yields synthetic silence because the open device delivers nothing."""
        return self._stalled

    def _close_stream(self, stream: Any, is_input: bool = True) -> None:
        self._live.discard(self._tokens.pop(id(stream), -1))
        try:
            try:
                stream.stop()
            finally:
                stream.close()
        except BaseException:
            self._leaked.append((stream, is_input))
            raise

    def _retry_leaked(self) -> None:
        leaked, self._leaked = self._leaked, []
        first: BaseException | None = None
        for stream, is_input in leaked:
            try:
                self._close_stream(stream, is_input)
            except BaseException as exc:  # noqa: BLE001 - try them all, report the first
                first = first or exc
        if first is not None:
            raise first

    def _drain(self) -> None:
        while True:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                return

    def start(self, input_enabled: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            if input_enabled:
                self._stream = self._open(self._gen)
            self._enabled = input_enabled
            self._started = True

    def set_input_enabled(self, enabled: bool) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._retry_leaked()  # a stream that would not close earlier gets another try
            except Exception:  # noqa: BLE001 - only the disable path reports it; a stuck leftover never blocks a reopen
                if not enabled:
                    raise
            if enabled == self._enabled:
                return
            if enabled:
                self._drain()  # free anything queued; the gen tag is what keeps it from the consumer
                new_gen = self._gen + 1
                self._stream = self._open(new_gen)  # raises: stays disabled and silent
                self._gen = new_gen
                self._enabled = True
                return
            stream, self._stream = self._stream, None
            self._enabled = False  # silent first, whatever close does
            self._gen += 1  # anything the closing stream still queues is stale
            self._drain()
            if stream is not None:
                self._close_stream(stream)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._enabled = False
            streams = [(self._stream, True), *self._leaked]
            self._stream = None
            self._leaked = []
            first: BaseException | None = None
            for stream, is_input in streams:
                if stream is None:
                    continue
                try:
                    self._close_stream(stream, is_input)
                except BaseException as exc:  # noqa: BLE001 - close every stream, then re-raise the first
                    first = first or exc
        if first is not None:
            raise first

    def frames(self) -> Iterator[bytes]:
        """Yield 320-byte frames until close(); the caller may also break out (Ctrl-C is a plain KeyboardInterrupt)."""
        if not self._started:
            raise RuntimeError("MicSource.frames() before start()")
        self._next_at = None
        while not self._closed:
            if self._enabled:
                self._next_at = None
                try:
                    gen, data = self._frames.get(timeout=self._stall_s)
                except queue.Empty:
                    self._stalled = True  # the device went quiet: keep the consumer ticking
                    if not self._closed:
                        yield _SILENCE
                    continue
                if gen != self._gen:
                    continue  # queued by a stream that has since been switched away
                self._stalled = False
                if not self._closed:
                    yield data
                continue
            # Disabled: drop what the closed device left behind, then pace silence.
            self._stalled = False
            self._drain()
            now = self._clock()
            if self._next_at is None or now - self._next_at > _MAX_LAG_FRAMES * FRAME_S:
                self._next_at = now  # first frame, or far behind after a stall: no burst
            wait = self._next_at - now
            if wait > 0:
                self._sleep(wait)
            self._next_at += FRAME_S
            if not self._closed:
                yield _SILENCE


def frame_source() -> Iterator[bytes]:
    """Yield frames from the default input device until the caller stops (used by `eridian listen`)."""
    source = MicSource()
    source.start()
    try:
        yield from source.frames()
    finally:
        source.close()


def default_input_device_name() -> str | None:
    """Best-effort name of the default input device, for status/logging."""
    import sounddevice as sd

    try:
        return sd.query_devices(kind="input")["name"]
    except Exception:  # noqa: BLE001 - this is cosmetic, never fatal
        return None

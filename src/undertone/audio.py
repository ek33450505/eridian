"""Real microphone frame source.

Heavy/optional deps (`sounddevice`) are imported lazily inside functions so
`undertone hook stop` and friends stay fast and dependency-light even when
this module is imported transitively. Not unit-tested beyond import-guarding
-- a real audio stream needs a real mic, which tests must never open.
"""

from __future__ import annotations

import queue
from typing import Iterator

from .listener import FRAME_BYTES, SAMPLE_RATE


def frame_source() -> Iterator[bytes]:
    """Yield 320-byte (10 ms) int16 mono frames from the default input device.

    Runs forever; the caller breaks out of the loop (e.g. on Ctrl-C, which
    surfaces as a plain KeyboardInterrupt since this is a generator, not a
    thread).
    """
    import sounddevice as sd

    frames: "queue.Queue[bytes]" = queue.Queue()

    def _callback(data: bytes, _frame_count: int, _time_info: object, _status: object) -> None:
        # sounddevice runs this callback on its own audio thread, not the
        # caller's thread; queue.Queue is thread-safe, so handing bytes off
        # here to be drained by frames.get() below is safe without a lock.
        frames.put(bytes(data))

    blocksize = FRAME_BYTES // 2  # int16 samples, not bytes
    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=blocksize,
        callback=_callback,
    ):
        while True:
            yield frames.get()


def default_input_device_name() -> str | None:
    """Best-effort name of the default input device, for status/logging."""
    import sounddevice as sd

    try:
        return sd.query_devices(kind="input")["name"]
    except Exception:  # noqa: BLE001 - this is cosmetic, never fatal
        return None

"""Full-duplex audio with WebRTC echo cancellation.

One `sounddevice.RawStream` carries both the mic and the speaker. Undertone
plays TTS PCM itself, so every 10 ms audio callback knows exactly which speaker
frame went out alongside the mic frame it captured. The main thread feeds that
pair to WebRTC AEC3 (via `livekit`) and yields echo-free mic frames to the
listener.

Heavy/optional deps (`sounddevice`, `livekit`) are imported lazily so light
commands stay fast. Tests inject fakes via `stream_factory` / `apm_factory`;
a real stream needs real devices, which tests must never open.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Iterator

from .listener import FRAME_BYTES, SAMPLE_RATE

# Used when the stream does not report its latency.
DEFAULT_DELAY_MS = 40

_STOP = object()


def _default_stream_factory(callback: Callable[..., None]) -> Any:
    import sounddevice as sd

    return sd.RawStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=FRAME_BYTES // 2,  # int16 samples, not bytes
        callback=callback,
    )


def _default_apm_factory(noise_suppression: bool) -> Any:
    from livekit.rtc import AudioProcessingModule

    return AudioProcessingModule(
        echo_cancellation=True,
        noise_suppression=noise_suppression,
        high_pass_filter=True,
    )


class DuplexAudio:
    def __init__(
        self,
        *,
        stream_factory: Callable[[Callable[..., None]], Any] | None = None,
        apm_factory: Callable[[bool], Any] | None = None,
        delay_ms: int | None = None,
        noise_suppression: bool = True,
    ) -> None:
        self._stream_factory = stream_factory or _default_stream_factory
        self._apm_factory = apm_factory or _default_apm_factory
        self._delay_ms = delay_ms
        self._noise_suppression = noise_suppression
        self._stream: Any = None
        self._apm: Any = None
        self._closed = False
        # (mic, ref) pairs from the audio thread; frames() is the only consumer.
        self._pairs: "queue.Queue[Any]" = queue.Queue()
        # One lock guards the playback buffer; the Condition lets wait_drained
        # sleep without the audio callback ever having to wait.
        self._cond = threading.Condition()
        self._buf = bytearray()

    def start(self) -> None:
        self._apm = self._apm_factory(self._noise_suppression)
        self._stream = self._stream_factory(self._callback)
        if self._delay_ms is None:
            self._delay_ms = self._derive_delay_ms(self._stream)
        self._stream.start()

    @staticmethod
    def _derive_delay_ms(stream: Any) -> int:
        try:
            in_lat, out_lat = stream.latency
            return round((in_lat + out_lat) * 1000)
        except Exception:  # noqa: BLE001 - latency is advisory, never fatal
            return DEFAULT_DELAY_MS

    def _callback(self, indata: Any, outdata: Any, _frames: int, _time: object, _status: object) -> None:
        # Audio thread: keep this minimal and never block.
        n = len(outdata)
        with self._cond:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
            if not self._buf:
                self._cond.notify_all()
        # Zero-pad so the AEC reference is exactly what the speaker received.
        ref = chunk.ljust(n, b"\x00")
        outdata[:] = ref
        self._pairs.put((bytes(indata), ref))

    def frames(self) -> Iterator[bytes]:
        """Yield cleaned 320-byte mic frames until close()."""
        from livekit.rtc import AudioFrame

        samples = FRAME_BYTES // 2
        while True:
            item = self._pairs.get()
            apm = self._apm
            if item is _STOP or apm is None:
                return
            mic, ref = item
            apm.set_stream_delay_ms(self._delay_ms)
            # Far end must be seen before its paired capture so AEC3 can
            # model the echo path.
            apm.process_reverse_stream(AudioFrame(ref, SAMPLE_RATE, 1, samples))
            frame = AudioFrame(bytearray(mic), SAMPLE_RATE, 1, samples)  # writable: AEC edits in place
            apm.process_stream(frame)  # modifies in place
            yield bytes(frame.data)

    # --- PlaybackSink: called from the player's thread ---

    def play(self, pcm: bytes) -> None:
        pcm = pcm[: len(pcm) - (len(pcm) % 2)]  # drop a trailing half-sample
        if not pcm:
            return
        with self._cond:
            self._buf += pcm

    def clear(self) -> None:
        """Drop unplayed PCM immediately (barge-in)."""
        with self._cond:
            self._buf.clear()
            self._cond.notify_all()

    @property
    def playing(self) -> bool:
        with self._cond:
            return bool(self._buf)

    def wait_drained(self, timeout: float | None = None) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: not self._buf or self._closed, timeout)

    def close(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._buf.clear()
            self._cond.notify_all()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        # Release the APM now; livekit complains if it outlives the interpreter.
        self._apm = None
        self._pairs.put(_STOP)

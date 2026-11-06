"""Full-duplex audio with WebRTC echo cancellation.

One `sounddevice.RawStream` carries both the mic and the speaker. Eridian
plays TTS PCM itself, so every 10 ms audio callback knows exactly which speaker
frame went out alongside the mic frame it captured. The main thread feeds that
pair to WebRTC AEC3 (via `livekit`) and yields echo-free mic frames to the
listener.

Heavy/optional deps (`sounddevice`, `livekit`) are imported lazily so light
commands stay fast. Tests inject fakes via `stream_factory` / `output_stream_factory`
/ `apm_factory`; a real stream needs real devices, which tests must never open.

Output-only mode: `set_input_enabled(False)` closes the duplex stream (so the
OS mic indicator goes off) and opens an output-only stream in its place. Its
callback pulls the same playback buffer and queues `(silence, ref)` pairs, so
`frames()` keeps yielding at the normal 10 ms cadence (the voice bridge only
runs, and so can only unmute or speak a reply, when a frame arrives) and
playback is uninterrupted. `set_input_enabled(True)` swaps back. Each switch
opens the new stream BEFORE closing the old one, so playback has no gap; for a
few ms both pull from the one buffer under its lock, so no PCM is lost or
played twice. If the input cannot be reopened the call raises and the
output-only stream keeps running. A stream that fails to close stays referenced
and is retried (next disable, or close()); its callback is inert meanwhile (silence,
no playback pull, nothing queued). Every switch flushes queued and partial audio, so
nothing captured before it reaches the listener, and frames() yields a silent frame
when the device delivers nothing for `stall_s`, so a stall cannot freeze the consumer.
"""

from __future__ import annotations

import contextlib
import functools
import queue
import threading
from typing import Any, Callable, Iterator

from .listener import FRAME_BYTES, SAMPLE_RATE

# Used when the stream does not report its latency.
DEFAULT_DELAY_MS = 40

# frames() yields a silent frame when the device delivers nothing for this long,
# so the consumer's loop (mute/unmute, control requests) keeps running on a stall.
STALL_S = 0.05

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


def _default_output_stream_factory(callback: Callable[..., None]) -> Any:
    import sounddevice as sd

    return sd.RawOutputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=FRAME_BYTES // 2,
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
        output_stream_factory: Callable[[Callable[..., None]], Any] | None = None,
        apm_factory: Callable[[bool], Any] | None = None,
        delay_ms: int | None = None,
        noise_suppression: bool = True,
        stall_s: float = STALL_S,
    ) -> None:
        self._stall_s = stall_s
        self._stream_factory = stream_factory or _default_stream_factory
        self._output_stream_factory = output_stream_factory or _default_output_stream_factory
        self._apm_factory = apm_factory or _default_apm_factory
        self._delay_ms = delay_ms
        self._noise_suppression = noise_suppression
        self._stream: Any = None
        self._out_stream: Any = None  # output-only stream, open while the input is disabled
        self._input_enabled = True
        self._switch_lock = threading.Lock()  # serializes start/set_input_enabled/close stream swaps
        # Tokens of the streams allowed to touch the playback buffer. A stream whose
        # close failed is orphaned (token removed): its callback outputs silence only.
        self._live: set[int] = set()
        self._next_token = 0
        self._tokens: dict[int, int] = {}  # id(stream) -> its token
        self._leaked: list[tuple[Any, bool]] = []  # (stream, is_input) whose stop/close raised; retried, never forgotten
        self._stalled = False  # frames() is yielding synthetic silence because the device delivered nothing
        self._switch_gen = 0  # bumped on every input switch; frames() drops stale audio when it changes
        self._apm: Any = None
        self._closed = False
        # (gen, mic, ref) triples from the audio thread; frames() is the only consumer and drops any
        # whose gen is not the current switch generation (late callbacks of a stream that was switched away).
        self._pairs: "queue.Queue[Any]" = queue.Queue()
        # One lock guards the playback buffer; the Condition lets wait_drained
        # sleep without the audio callback ever having to wait.
        self._cond = threading.Condition()
        self._buf = bytearray()

    def start(self, input_enabled: bool = True) -> None:
        """Open the engine; with input_enabled False it starts output-only (no mic device opened)."""
        with self._switch_lock:
            self._apm = self._apm_factory(self._noise_suppression)
            try:
                if input_enabled:
                    self._stream = self._open_duplex(self._switch_gen)
                else:
                    self._out_stream = self._open_output(self._switch_gen)
                self._input_enabled = input_enabled
            except BaseException:
                self._abort_streams()
                self._apm = None
                raise

    def _open_duplex(self, gen: int) -> Any:
        token = self._new_token()
        stream = None
        try:
            stream = self._stream_factory(functools.partial(self._callback, token, gen))
            if self._delay_ms is None:
                self._delay_ms = self._derive_delay_ms(stream)
            stream.start()
        except BaseException:
            self._live.discard(token)
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
            raise
        self._tokens[id(stream)] = token
        return stream

    def _new_token(self) -> int:
        self._next_token += 1
        self._live.add(self._next_token)
        return self._next_token

    def _open_output(self, gen: int) -> Any:
        token = self._new_token()
        stream = None
        try:
            stream = self._output_stream_factory(functools.partial(self._output_callback, token, gen))
            stream.start()
        except BaseException:
            self._live.discard(token)
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
            raise
        self._tokens[id(stream)] = token
        return stream

    def _abort_streams(self) -> None:
        for attr in ("_stream", "_out_stream"):
            stream = getattr(self, attr)
            setattr(self, attr, None)
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()

    def _close_stream(self, stream: Any, is_input: bool) -> None:
        """Stop and close; the stream stops touching playback first, and stays reachable if closing fails."""
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
        """Try again to close streams that failed to close.

        Raises only for an INPUT stream that still will not close (the mic may be
        open); a stuck output-only stream stays leaked and retried, silently.
        """
        leaked, self._leaked = self._leaked, []
        first: BaseException | None = None
        for stream, is_input in leaked:
            try:
                self._close_stream(stream, is_input)
            except BaseException as exc:  # noqa: BLE001 - try them all, report the first input failure
                if is_input:
                    first = first or exc
        if first is not None:
            raise first

    def _flush_stale(self) -> None:
        """After a switch: drop queued audio (the generation tag is the guarantee; this frees memory)."""
        while True:
            try:
                item = self._pairs.get_nowait()
            except queue.Empty:
                return
            if item is _STOP:
                self._pairs.put(_STOP)
                return

    @property
    def stalled(self) -> bool:
        """True while frames() is yielding synthetic silence because the device delivers nothing."""
        return self._stalled

    def set_input_enabled(self, enabled: bool) -> None:
        """Close (False) or reopen (True) the mic device. Idempotent; playback and frames() continue.

        Raises if the new stream cannot be opened; the engine then keeps the
        stream it had (reopen failure: the output-only stream keeps running; a
        failed open while disabling leaves the duplex in place). If the old input
        stream then fails to close on a disable, the switch is still committed:
        output-only runs, the duplex stream is kept in `_leaked` (it may still be
        capturing) and retried by the next call or close(); its callback is inert
        because its token is gone. On enable a stuck leftover never blocks the reopen.
        On disable, a leftover input stream that still will not close makes this
        raise BEFORE switching: the live duplex keeps capturing (the bridge still
        drops every frame while muted) until a later call succeeds.
        """
        with self._switch_lock:
            if self._closed:
                return
            try:
                self._retry_leaked()  # a stream that would not close earlier gets another try
            except Exception:  # noqa: BLE001 - only the disable path reports it
                if not enabled:
                    raise
            if enabled == self._input_enabled:
                return
            new_gen = self._switch_gen + 1
            if enabled:
                new = self._open_duplex(new_gen)
                old, self._out_stream = self._out_stream, None
                self._stream = new
            else:
                new = self._open_output(new_gen)
                old, self._stream = self._stream, None
                self._out_stream = new
            self._input_enabled = enabled
            self._switch_gen = new_gen  # from here, anything the old stream still queues is dropped
            try:
                if old is not None:
                    if enabled:  # the output-only stream is redundant now; a failed close is kept and retried
                        with contextlib.suppress(Exception):
                            self._close_stream(old, False)
                    else:
                        self._close_stream(old, True)  # the mic device may still be open: let the caller know
            finally:
                self._flush_stale()

    @staticmethod
    def _derive_delay_ms(stream: Any) -> int:
        try:
            in_lat, out_lat = stream.latency
            return round((in_lat + out_lat) * 1000)
        except Exception:  # noqa: BLE001 - latency is advisory, never fatal
            return DEFAULT_DELAY_MS

    def _callback(
        self, token: int, gen: int, indata: Any, outdata: Any, _frames: int, _time: object, _status: object
    ) -> None:
        # Audio thread: keep this minimal and never block.
        n = len(outdata)
        if token not in self._live:  # orphaned stream: silence, no playback pull, nothing queued
            outdata[:] = bytes(n)
            return
        with self._cond:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
            if not self._buf:
                self._cond.notify_all()
        # Zero-pad so the AEC reference is exactly what the speaker received.
        ref = chunk.ljust(n, b"\x00")
        outdata[:] = ref
        self._pairs.put((gen, bytes(indata), ref))

    def _output_callback(self, token: int, gen: int, outdata: Any, _frames: int, _time: object, _status: object) -> None:
        # Audio thread, input disabled: same playback pull, silence for the mic.
        n = len(outdata)
        if token not in self._live:
            outdata[:] = bytes(n)
            return
        with self._cond:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
            if not self._buf:
                self._cond.notify_all()
        ref = chunk.ljust(n, b"\x00")
        outdata[:] = ref
        self._pairs.put((gen, b"\x00" * n, ref))

    def frames(self) -> Iterator[bytes]:
        """Yield cleaned 320-byte mic frames until close()."""
        from livekit.rtc import AudioFrame

        samples = FRAME_BYTES // 2
        # Callback blocks need not be exactly one frame; re-chunk, since livekit
        # raises on any other size. mic and ref grow in lockstep.
        mic_buf = bytearray()
        ref_buf = bytearray()
        seen_gen = self._switch_gen
        while True:
            try:
                item = self._pairs.get(timeout=self._stall_s)
            except queue.Empty:
                # The device delivered nothing (hiccup, unplug): keep the consumer's loop
                # ticking with a silent frame, straight to the listener, not through the AEC.
                if self._closed:
                    return
                self._stalled = True
                yield bytes(FRAME_BYTES)
                continue
            if item is _STOP:
                return
            if item[0] != self._switch_gen:
                continue  # queued by a stream that has since been switched away
            self._stalled = False
            if self._switch_gen != seen_gen:  # the input switched: drop partial pre-switch audio
                seen_gen = self._switch_gen
                mic_buf.clear()
                ref_buf.clear()
            mic_buf += item[1]
            ref_buf += item[2]
            while len(mic_buf) >= FRAME_BYTES and len(ref_buf) >= FRAME_BYTES:
                apm = self._apm
                if apm is None:
                    return
                mic = bytes(mic_buf[:FRAME_BYTES])
                ref = bytes(ref_buf[:FRAME_BYTES])
                del mic_buf[:FRAME_BYTES]
                del ref_buf[:FRAME_BYTES]
                apm.set_stream_delay_ms(DEFAULT_DELAY_MS if self._delay_ms is None else self._delay_ms)
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
            already = self._closed
            self._closed = True
            self._buf.clear()
            self._cond.notify_all()
        with self._switch_lock:
            if already and not self._leaked:
                return  # a repeat close() only matters to retry streams that failed to close
            streams = [(self._stream, True), (self._out_stream, False), *self._leaked]
            self._stream = self._out_stream = None
            self._leaked = []
            first: BaseException | None = None
            for stream, is_input in streams:
                if stream is None:
                    continue
                try:
                    self._close_stream(stream, is_input)
                except BaseException as exc:  # noqa: BLE001 - close every stream, then re-raise the first
                    first = first or exc
        # Release the APM now; livekit complains if it outlives the interpreter.
        self._apm = None
        if not already:
            self._pairs.put(_STOP)
        if first is not None:
            raise first

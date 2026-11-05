"""DuplexAudio tests: fake stream and APM only, never a real device."""

import threading
import unittest

from eridian.duplex import DEFAULT_DELAY_MS, DuplexAudio

BLOCK = 320


class FakeStream:
    def __init__(self, callback, latency=(0.012, 0.028)):
        self.callback = callback
        if latency is not None:
            self.latency = latency
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeAPM:
    """Records calls; zeroes the capture frame whenever the last ref was nonzero."""

    def __init__(self):
        self.calls = []
        self._ref_nonzero = False

    def set_stream_delay_ms(self, ms):
        self.calls.append(("delay", ms))

    def process_reverse_stream(self, frame):
        data = bytes(frame.data)
        self._ref_nonzero = any(data)
        self.calls.append(("reverse", data))

    def process_stream(self, frame):
        self.calls.append(("capture", bytes(frame.data)))
        if self._ref_nonzero:
            memoryview(frame.data).cast("B")[:] = bytes(BLOCK)


def make(latency=(0.012, 0.028), **kw):
    holder = {}

    def factory(cb):
        holder["stream"] = FakeStream(cb, latency)
        return holder["stream"]

    apm = FakeAPM()
    d = DuplexAudio(stream_factory=factory, apm_factory=lambda ns: apm, **kw)
    d.start()
    return d, holder["stream"], apm


def tick(stream, mic=b"\x01\x00" * 160):
    out = bytearray(BLOCK)
    stream.callback(bytearray(mic), out, 160, None, None)
    return bytes(out)


class DuplexTests(unittest.TestCase):
    def test_start_starts_stream(self):
        d, stream, _ = make()
        self.assertTrue(stream.started)

    def test_reverse_before_capture_and_delay_every_frame(self):
        d, stream, apm = make()
        for _ in range(3):
            tick(stream)
        it = d.frames()
        for _ in range(3):
            next(it)
        kinds = [c[0] for c in apm.calls]
        self.assertEqual(kinds, ["delay", "reverse", "capture"] * 3)
        self.assertTrue(all(c[1] == 40 for c in apm.calls if c[0] == "delay"))

    def test_explicit_delay_override(self):
        d, stream, apm = make(delay_ms=75)
        tick(stream)
        next(d.frames())
        self.assertEqual(apm.calls[0], ("delay", 75))

    def test_missing_latency_defaults(self):
        d, stream, apm = make(latency=None)
        tick(stream)
        next(d.frames())
        self.assertEqual(apm.calls[0], ("delay", DEFAULT_DELAY_MS))
        self.assertEqual(DEFAULT_DELAY_MS, 40)

    def test_frames_yield_apm_modified_mic(self):
        d, stream, _ = make()
        mic = b"\x07\x00" * 160
        tick(stream, mic)  # nothing playing: ref silent, mic untouched
        d.play(b"\x05\x00" * 160)
        tick(stream, mic)  # ref nonzero: fake APM cancels the mic
        it = d.frames()
        self.assertEqual(next(it), mic)
        self.assertEqual(next(it), bytes(BLOCK))

    def test_play_blocks_in_order_with_zero_padding(self):
        d, stream, _ = make()
        pcm = bytes(range(1, 255)) * 2 + b"\x09\x09" * 10  # 528 bytes
        pcm = pcm[:528]
        d.play(pcm)
        self.assertEqual(tick(stream), pcm[:BLOCK])
        self.assertEqual(tick(stream), pcm[BLOCK:] + bytes(BLOCK - (len(pcm) - BLOCK)))
        self.assertEqual(tick(stream), bytes(BLOCK))

    def test_queued_ref_equals_written_output(self):
        d, stream, apm = make()
        d.play(b"\x03\x00" * 200)  # 400 bytes -> second block is padded
        outs = [tick(stream), tick(stream)]
        it = d.frames()
        next(it)
        next(it)
        refs = [c[1] for c in apm.calls if c[0] == "reverse"]
        self.assertEqual(refs, outs)

    def test_clear_drops_pending_audio(self):
        d, stream, _ = make()
        d.play(b"\x03\x00" * 500)
        d.clear()
        self.assertFalse(d.playing)
        self.assertEqual(tick(stream), bytes(BLOCK))

    def test_playing_and_wait_drained(self):
        d, stream, _ = make()
        self.assertTrue(d.wait_drained(0))  # empty: immediately true
        d.play(b"\x03\x00" * 100)  # 200 bytes, one callback
        self.assertTrue(d.playing)
        self.assertFalse(d.wait_drained(0.05))
        threading.Timer(0.05, tick, args=(stream,)).start()
        self.assertTrue(d.wait_drained(2))
        self.assertFalse(d.playing)

    def test_odd_length_ignores_trailing_byte(self):
        d, stream, _ = make()
        d.play(b"\x01\x00\x02")
        self.assertEqual(tick(stream)[:4], b"\x01\x00" + bytes(2))
        self.assertFalse(d.playing)
        d.play(b"\x01")
        self.assertFalse(d.playing)

    def test_close_unblocks_consumer_and_cleans_up(self):
        d, stream, _ = make()
        done = []
        t = threading.Thread(target=lambda: done.append(list(d.frames())))
        t.start()
        d.close()
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(done, [[]])
        self.assertTrue(stream.stopped and stream.closed)
        self.assertIsNone(d._apm)
        d.close()  # idempotent

    def test_blocks_are_rechunked_into_frames(self):
        d, stream, _ = make()
        stream.callback(bytearray(b"\x01\x00" * 320), bytearray(2 * BLOCK), 320, None, None)
        it = d.frames()
        self.assertEqual([len(next(it)), len(next(it))], [BLOCK, BLOCK])
        self.assertTrue(d._pairs.empty())

    def test_partial_block_is_carried(self):
        d, stream, apm = make()
        stream.callback(bytearray(b"\x01\x00" * 240), bytearray(480), 240, None, None)
        stream.callback(bytearray(b"\x01\x00" * 80), bytearray(160), 80, None, None)
        it = d.frames()
        self.assertEqual(len(next(it)), BLOCK)  # 480 bytes -> 1 frame, 160 carried
        self.assertEqual(len(next(it)), BLOCK)  # 160 carried + 160 -> 1 frame
        self.assertEqual(sum(1 for c in apm.calls if c[0] == "capture"), 2)

    def test_stream_start_failure_cleans_up(self):
        holder = {}

        class BadStream(FakeStream):
            def start(self):
                raise OSError("no device")

        def factory(cb):
            holder["s"] = BadStream(cb)
            return holder["s"]

        d = DuplexAudio(stream_factory=factory, apm_factory=lambda ns: FakeAPM())
        with self.assertRaises(OSError):
            d.start()
        self.assertTrue(holder["s"].closed)
        self.assertIsNone(d._apm)
        self.assertIsNone(d._stream)

    def test_stream_factory_failure_drops_apm(self):
        def factory(cb):
            raise OSError("no device")

        d = DuplexAudio(stream_factory=factory, apm_factory=lambda ns: FakeAPM())
        with self.assertRaises(OSError):
            d.start()
        self.assertIsNone(d._apm)

    def test_close_wakes_wait_drained(self):
        d, _, _ = make()
        d.play(b"\x01\x00" * 10)
        threading.Timer(0.05, d.close).start()
        self.assertTrue(d.wait_drained(2))

    def test_real_apm_smoke(self):
        try:
            from livekit.rtc import AudioProcessingModule  # noqa: F401
        except ImportError:
            self.skipTest("livekit unavailable")
        holder = {}

        def factory(cb):
            holder["s"] = FakeStream(cb)
            return holder["s"]

        d = DuplexAudio(stream_factory=factory)
        d.start()
        for _ in range(5):
            tick(holder["s"], bytes(BLOCK))
        it = d.frames()
        for _ in range(5):
            self.assertEqual(len(next(it)), BLOCK)
        d.close()


if __name__ == "__main__":
    unittest.main()

"""Mic device open/close while muted: fake streams only, never a real device."""

import json
import threading
import unittest
from unittest import mock

from eridian.audio import FRAME_S, MicSource
from eridian.cli import AudioIO
from eridian.duplex import DuplexAudio
from tests import test_voice as tv
from tests.test_control import ControlBase
from tests.test_duplex import BLOCK, FakeAPM, FakeStream, tick

FRAME = tv.FRAME


class OutStream:
    def __init__(self, callback):
        self.callback = callback
        self.started = self.stopped = self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def pull(it, n, timeout=2.0):
    """Up to n items from a generator; fails (does not hang) if it blocks. Stops quietly at StopIteration."""
    out = []

    def run():
        try:
            for _ in range(n):
                out.append(next(it))
        except StopIteration:
            pass

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise AssertionError("frames() blocked or spun instead of yielding")
    return out


def fail_once(stream, name):
    """Make stream.<name>() raise OSError the first time it is called."""
    orig = getattr(stream, name)
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise OSError(f"{name} failed")
        orig()

    setattr(stream, name, flaky)


def always_fail(stream, name):
    def boom():
        raise OSError(f"{name} failed")

    setattr(stream, name, boom)


def make_duplex(fail_duplex_open=False, stall_s=30.0):
    streams = {"duplex": [], "out": []}

    def duplex_factory(cb):
        if fail_duplex_open and streams["duplex"]:
            raise OSError("no mic")
        s = FakeStream(cb)
        streams["duplex"].append(s)
        return s

    def out_factory(cb):
        s = OutStream(cb)
        streams["out"].append(s)
        return s

    d = DuplexAudio(
        stream_factory=duplex_factory,
        output_stream_factory=out_factory,
        apm_factory=lambda ns: FakeAPM(),
        stall_s=stall_s,
    )
    return d, streams


def tick_out(stream):
    out = bytearray(BLOCK)
    stream.callback(out, 160, None, None)
    return bytes(out)


class DuplexInputSwitchTests(unittest.TestCase):
    def test_disable_closes_duplex_and_opens_output(self):
        d, st = make_duplex()
        d.start()
        d.set_input_enabled(False)
        self.assertTrue(st["duplex"][0].stopped and st["duplex"][0].closed)
        self.assertTrue(st["out"][0].started and not st["out"][0].closed)

    def test_frames_keep_flowing_with_silence_and_playback_drains(self):
        d, st = make_duplex()
        d.start()
        d.set_input_enabled(False)
        d.play(b"\x07\x00" * 160)
        out = tick_out(st["out"][0])
        self.assertEqual(out, b"\x07\x00" * 160)
        self.assertFalse(d.playing)
        tick_out(st["out"][0])
        self.assertEqual(len(pull(d.frames(), 2)), 2)  # frames yielded while the input is disabled

    def test_mic_is_silence_while_disabled(self):
        d, st = make_duplex()
        d.start()
        d.set_input_enabled(False)
        tick_out(st["out"][0])
        self.assertEqual(d._pairs.get(timeout=2)[1], bytes(BLOCK))

    def test_enable_reopens_duplex_and_closes_output(self):
        d, st = make_duplex()
        d.start()
        d.set_input_enabled(False)
        d.set_input_enabled(True)
        self.assertEqual(len(st["duplex"]), 2)
        self.assertTrue(st["duplex"][1].started and not st["duplex"][1].closed)
        self.assertTrue(st["out"][0].closed)

    def test_no_pcm_lost_or_duplicated_across_switch(self):
        d, st = make_duplex()
        d.start()
        pcm = (bytes(range(256)) * 4)[: BLOCK * 3]
        d.play(pcm)
        got = bytearray()
        out = bytearray(BLOCK)
        st["duplex"][0].callback(bytearray(BLOCK), out, 160, None, None)
        got += out
        d.set_input_enabled(False)
        got += tick_out(st["out"][0])
        d.set_input_enabled(True)
        out = bytearray(BLOCK)
        st["duplex"][1].callback(bytearray(BLOCK), out, 160, None, None)
        got += out
        self.assertEqual(bytes(got[: len(pcm)]), pcm)

    def test_reopen_failure_raises_and_output_keeps_running(self):
        d, st = make_duplex(fail_duplex_open=True)
        d.start()
        d.set_input_enabled(False)
        with self.assertRaises(OSError):
            d.set_input_enabled(True)
        self.assertFalse(st["out"][0].closed)
        d.play(b"\x01\x00" * 160)
        self.assertEqual(tick_out(st["out"][0]), b"\x01\x00" * 160)

    def test_idempotent(self):
        d, st = make_duplex()
        d.start()
        d.set_input_enabled(True)
        self.assertEqual(len(st["duplex"]), 1)
        d.set_input_enabled(False)
        d.set_input_enabled(False)
        self.assertEqual(len(st["out"]), 1)

    def test_close_while_disabled_closes_output(self):
        d, st = make_duplex()
        d.start()
        d.set_input_enabled(False)
        d.close()
        self.assertTrue(st["out"][0].stopped and st["out"][0].closed)
        d.set_input_enabled(True)  # after close: no-op
        self.assertEqual(len(st["duplex"]), 1)

    def test_start_output_only_never_opens_the_mic(self):
        d, st = make_duplex()
        d.start(input_enabled=False)
        self.assertEqual(st["duplex"], [])
        self.assertTrue(st["out"][0].started)
        d.set_input_enabled(True)
        self.assertEqual(len(st["duplex"]), 1)


    def test_stalled_device_still_yields_silence(self):
        d, st = make_duplex(stall_s=0.01)
        d.start()  # the fake stream never delivers anything
        frames = pull(d.frames(), 3)
        self.assertEqual(frames, [bytes(BLOCK)] * 3)

    def test_failed_close_is_retried_by_the_next_disable(self):
        d, st = make_duplex()
        d.start()
        mic = st["duplex"][0]
        fail_once(mic, "close")
        with self.assertRaises(OSError):
            d.set_input_enabled(False)
        self.assertFalse(mic.closed)
        d.set_input_enabled(False)  # already disabled, but the orphan gets another try
        self.assertTrue(mic.closed)

    def test_failed_close_is_retried_by_close(self):
        d, st = make_duplex()
        d.start()
        mic = st["duplex"][0]
        fail_once(mic, "close")
        with self.assertRaises(OSError):
            d.set_input_enabled(False)
        d.close()
        self.assertTrue(mic.closed)

    def test_orphaned_stream_does_not_consume_playback(self):
        d, st = make_duplex()
        d.start()
        mic = st["duplex"][0]
        fail_once(mic, "close")
        with self.assertRaises(OSError):
            d.set_input_enabled(False)
        d.play(b"\x04\x00" * 160)
        out = bytearray(BLOCK)
        mic.callback(bytearray(b"\x09\x00" * 160), out, 160, None, None)  # the orphan's callback
        self.assertEqual(bytes(out), bytes(BLOCK))
        self.assertTrue(d.playing)  # nothing was pulled
        self.assertTrue(d._pairs.empty())  # nothing was queued
        self.assertEqual(tick_out(st["out"][0]), b"\x04\x00" * 160)  # the live stream plays it once

    def test_pre_switch_audio_never_reaches_the_consumer(self):
        d, st = make_duplex()
        d.start()
        mic = st["duplex"][0]
        for _ in range(3):
            mic.callback(bytearray(b"\x09\x00" * 160), bytearray(BLOCK), 160, None, None)
        it = d.frames()
        self.assertEqual(pull(it, 1), [b"\x09\x00" * 160])  # two marked frames still queued
        d.set_input_enabled(False)
        tick_out(st["out"][0])
        self.assertEqual(pull(it, 1), [bytes(BLOCK)])

    def test_partial_blocks_dropped_on_switch(self):
        d, st = make_duplex()
        d.start()
        d._pairs.put((d._switch_gen, b"\x09" * (BLOCK + BLOCK // 2), bytes(BLOCK + BLOCK // 2)))  # 1.5 blocks in one go
        it = d.frames()
        pull(it, 1)  # half a block of marked audio now sits in frames()' own buffer
        d.set_input_enabled(False)
        tick_out(st["out"][0])
        self.assertEqual(pull(it, 1), [bytes(BLOCK)])


    def test_stuck_orphan_does_not_block_enable(self):
        d, st = make_duplex()
        d.start()
        mic = st["duplex"][0]
        always_fail(mic, "close")
        with self.assertRaises(OSError):
            d.set_input_enabled(False)
        with self.assertRaises(OSError):
            d.set_input_enabled(False)  # still stuck: the disable path keeps reporting it
        d.set_input_enabled(True)  # but the reopen goes through
        self.assertEqual(len(st["duplex"]), 2)
        self.assertTrue(st["duplex"][1].started)

    def test_failed_output_close_on_enable_is_kept_and_closed_by_close(self):
        d, st = make_duplex()
        d.start()
        d.set_input_enabled(False)
        out = st["out"][0]
        fail_once(out, "close")
        d.set_input_enabled(True)  # the redundant output stream's close failed; enable still works
        self.assertFalse(out.closed)
        d.close()
        self.assertTrue(out.closed)

    def test_stuck_output_orphan_does_not_block_disable(self):
        d, st = make_duplex()
        d.start()
        d.set_input_enabled(False)
        always_fail(st["out"][0], "close")
        d.set_input_enabled(True)
        d.set_input_enabled(False)  # the stuck OUTPUT orphan must not stop the mic from closing
        self.assertTrue(st["duplex"][1].closed)

    def test_late_old_generation_item_never_reaches_the_consumer(self):
        d, st = make_duplex()
        d.start()
        old_gen = d._switch_gen
        d.set_input_enabled(False)
        d._pairs.put((old_gen, b"\x09" * BLOCK, bytes(BLOCK)))  # a late callback of the closed duplex
        tick_out(st["out"][0])
        self.assertEqual(pull(d.frames(), 1), [bytes(BLOCK)])

    def test_stalled_flag_follows_real_frames(self):
        d, st = make_duplex(stall_s=0.01)
        d.start()
        self.assertFalse(d.stalled)
        it = d.frames()
        pull(it, 1)
        self.assertTrue(d.stalled)
        tick(st["duplex"][0])
        pull(it, 1)
        self.assertFalse(d.stalled)


class MicSourceTests(unittest.TestCase):
    def make(self, fail_after_first=False, stall_s=30.0):
        self.streams = []
        self.slept = []
        self.now = [0.0]

        def factory(cb):
            if fail_after_first and self.streams:
                raise OSError("no mic")
            s = OutStream(cb)
            self.streams.append(s)
            return s

        def sleep(t):
            self.slept.append(t)
            self.now[0] += t

        return MicSource(input_factory=factory, sleep=sleep, clock=lambda: self.now[0], stall_s=stall_s)

    def test_disable_closes_stream_and_yields_paced_silence(self):
        m = self.make()
        m.start()
        it = m.frames()
        self.streams[0].callback(b"\x05\x00" * 160, 160, None, None)
        self.assertEqual(pull(it, 1), [b"\x05\x00" * 160])
        m.set_input_enabled(False)
        self.assertTrue(self.streams[0].stopped and self.streams[0].closed)
        frames = pull(it, 5)
        self.assertEqual(frames, [bytes(BLOCK)] * 5)
        self.assertAlmostEqual(sum(self.slept), 4 * FRAME_S, places=6)  # paced at 10 ms

    def test_stale_mic_frames_dropped_when_disabled(self):
        m = self.make()
        m.start()
        it = m.frames()
        self.streams[0].callback(b"\x05\x00" * 160, 160, None, None)
        m.set_input_enabled(False)
        self.assertEqual(pull(it, 1), [bytes(BLOCK)])

    def test_stale_frames_dropped_on_enable(self):
        m = self.make()
        m.start()
        m.set_input_enabled(False)
        m._frames.put((m._gen, b"\x05\x00" * 160))  # a late callback landed after the disable
        m.set_input_enabled(True)
        self.assertTrue(m._frames.empty())

    def test_enable_reopens(self):
        m = self.make()
        m.start()
        m.set_input_enabled(False)
        m.set_input_enabled(True)
        self.assertEqual(len(self.streams), 2)
        self.assertTrue(self.streams[1].started)
        m.set_input_enabled(True)
        self.assertEqual(len(self.streams), 2)

    def test_reopen_failure_raises_and_stays_silent(self):
        m = self.make(fail_after_first=True)
        m.start()
        m.set_input_enabled(False)
        with self.assertRaises(OSError):
            m.set_input_enabled(True)
        self.assertEqual(pull(m.frames(), 1), [bytes(BLOCK)])

    def test_start_disabled_opens_nothing(self):
        m = self.make()
        m.start(input_enabled=False)
        self.assertEqual(self.streams, [])

    def test_stalled_device_still_yields_silence(self):
        m = self.make(stall_s=0.01)
        m.start()  # the stream never delivers
        self.assertEqual(pull(m.frames(), 3), [bytes(BLOCK)] * 3)

    def test_failed_close_is_retried_and_orphan_queues_nothing(self):
        m = self.make()
        m.start()
        mic = self.streams[0]
        fail_once(mic, "close")
        with self.assertRaises(OSError):
            m.set_input_enabled(False)
        mic.callback(b"\x05\x00" * 160, 160, None, None)  # the orphan's callback
        self.assertTrue(m._frames.empty())
        m.set_input_enabled(False)  # retry
        self.assertTrue(mic.closed)

    def test_failed_close_is_retried_by_close(self):
        m = self.make()
        m.start()
        mic = self.streams[0]
        fail_once(mic, "close")
        with self.assertRaises(OSError):
            m.set_input_enabled(False)
        m.close()
        self.assertTrue(mic.closed)

    def test_no_reopen_after_close_and_frames_end(self):
        m = self.make()
        m.start()
        it = m.frames()
        m.close()
        m.set_input_enabled(True)
        self.assertEqual(len(self.streams), 1)
        self.assertEqual(pull(it, 1), [])  # the generator ends

    def test_frames_before_start_raises(self):
        m = self.make()
        with self.assertRaises(RuntimeError):
            next(m.frames())

    def test_pacing_resets_after_a_stall_instead_of_bursting(self):
        m = self.make()
        m.start(input_enabled=False)
        it = m.frames()
        pull(it, 2)
        self.now[0] += 5.0  # the consumer stalled for 5 s
        before = len(self.slept)
        pull(it, 4)
        self.assertGreater(sum(self.slept[before:]), 0)  # paced again, not 400 frames at once


    def test_stuck_orphan_does_not_block_enable(self):
        m = self.make()
        m.start()
        always_fail(self.streams[0], "close")
        with self.assertRaises(OSError):
            m.set_input_enabled(False)
        m.set_input_enabled(True)
        self.assertEqual(len(self.streams), 2)

    def test_late_old_generation_frame_never_reaches_the_consumer(self):
        m = self.make(stall_s=0.01)
        m.start()
        old_gen = m._gen
        m.set_input_enabled(False)
        m.set_input_enabled(True)
        m._frames.put((old_gen, b"\x05\x00" * 160))  # a late callback of the first stream
        self.assertEqual(pull(m.frames(), 1), [bytes(BLOCK)])

    def test_stalled_flag(self):
        m = self.make(stall_s=0.01)
        m.start()
        it = m.frames()
        pull(it, 1)
        self.assertTrue(m.stalled)
        self.streams[0].callback(b"\x05\x00" * 160, 160, None, None)
        self.assertEqual(pull(it, 1), [b"\x05\x00" * 160])
        self.assertFalse(m.stalled)
        m.set_input_enabled(False)
        pull(it, 1)
        self.assertFalse(m.stalled)  # a closed device is silent by design

    def test_frames_from_the_reopened_stream_are_delivered(self):
        m = self.make()
        m.start()
        m.set_input_enabled(False)
        m.set_input_enabled(True)
        self.streams[1].callback(b"\x06\x00" * 160, 160, None, None)
        self.assertEqual(pull(m.frames(), 1), [b"\x06\x00" * 160])

    def test_queued_frame_not_yielded_after_close(self):
        m = self.make()
        m.start()
        it = m.frames()
        self.streams[0].callback(b"\x05\x00" * 160, 160, None, None)
        m.close()
        self.assertEqual(pull(it, 1), [])

    def test_nothing_yielded_after_close(self):
        m = self.make(stall_s=0.01)
        m.start()
        it = m.frames()
        pull(it, 1)
        m.close()
        self.assertEqual(pull(it, 1), [])


class AudioIOTests(unittest.TestCase):
    def test_start_input_disabled_starts_engine_output_only(self):
        d, st = make_duplex()
        io = AudioIO(player_factory=lambda: None, talk_over=True, engine=d)
        io.start(input_enabled=False)
        self.assertEqual(st["duplex"], [])
        self.assertTrue(st["out"][0].started)
        io.set_input_enabled(True)
        self.assertEqual(len(st["duplex"]), 1)
        io.close()

    def test_stalled_delegates_to_the_engine(self):
        d, st = make_duplex(stall_s=0.01)
        io = AudioIO(player_factory=lambda: None, talk_over=True, engine=d)
        io.start()
        pull(io.frames(), 1)
        self.assertTrue(io.stalled)
        io.close()

    def test_no_aec_delegates_to_mic_source(self):
        io = AudioIO(player_factory=lambda: None, talk_over=False)
        with mock.patch("eridian.audio.MicSource") as ms:
            io.start(input_enabled=False)
            io.set_input_enabled(True)
        ms.return_value.start.assert_called_once_with(input_enabled=False)
        ms.return_value.set_input_enabled.assert_called_once_with(True)


class BridgeMicControlTests(ControlBase):
    def bridge_with(self, mic):
        self.calls = []
        self.threads = []

        def control(enabled):
            self.threads.append(threading.current_thread())
            self.calls.append(enabled)
            mic(enabled)

        return self.reopen(mic_control=control)

    def test_close_on_mute_open_on_unmute_from_main_thread(self):
        b = self.bridge_with(lambda e: None)
        b.set_muted(True)
        self.assertEqual(self.calls, [])  # nothing on the caller's thread
        b.process_frame(FRAME)
        self.assertEqual(self.calls, [False])
        b.set_muted(False)
        b.process_frame(FRAME)
        self.assertEqual(self.calls, [False, True])
        self.assertTrue(all(t is threading.current_thread() for t in self.threads))
        self.assertFalse(b.is_muted())

    def test_reopen_failure_remutes_and_persists(self):
        def mic(enabled):
            if enabled:
                raise OSError("dead")

        b = self.bridge_with(mic)
        b.set_muted(True)
        b.process_frame(FRAME)
        b.set_muted(False)
        self.wake_flag = True
        b.process_frame(FRAME)
        self.assertTrue(b.is_muted())
        self.assertIn("[mic failed to reopen; muted]", self.out.getvalue())
        self.assertIs(json.loads(self.state_file.read_text())["muted"], True)
        self.assertEqual(self.wake_calls, 0)
        b.process_frame(FRAME)  # the re-mute is applied: device close is attempted again
        self.assertEqual(self.calls[-1], False)
        self.assertEqual(self.wake_calls, 0)

    def test_close_failure_stays_muted_and_drops_frames(self):
        def mic(enabled):
            if not enabled:
                raise OSError("busy")

        b = self.bridge_with(mic)
        b.set_muted(True)
        self.wake_flag = True
        b.process_frame(FRAME)
        b.process_frame(FRAME)
        self.assertTrue(b.is_muted())
        self.assertIn("[mic did not close; frames are still dropped]", self.out.getvalue())
        self.assertEqual((self.wake_calls, self.vad_calls, self.stt_calls), (0, 0, 0))

    def test_no_mic_control_is_fine(self):
        self.bridge.set_muted(True)
        self.bridge.process_frame(FRAME)
        self.bridge.set_muted(False)
        self.bridge.process_frame(FRAME)
        self.assertFalse(self.bridge.is_muted())

    def test_no_lock_held_during_mic_control(self):
        seen = []

        def mic(enabled):
            # try-acquire every bridge lock from inside the call: all must be free
            for name in ("_lock", "_state_lock", "_print_lock", "_chunk_lock"):
                lock = getattr(b, name)
                ok = lock.acquire(blocking=False)
                if ok:
                    lock.release()
                seen.append((enabled, name, ok))

        b = self.bridge_with(mic)
        b.set_muted(True)
        b.process_frame(FRAME)
        b.set_muted(False)
        b.process_frame(FRAME)
        self.assertEqual(len(seen), 8)
        self.assertEqual([x for x in seen if not x[2]], [])

    def test_remute_that_raises_is_forced_and_does_not_escape(self):
        def mic(enabled):
            if enabled:
                raise OSError("dead")

        b = self.bridge_with(mic)
        b.set_muted(True)
        b.process_frame(FRAME)
        b.set_muted(False)
        with mock.patch.object(b, "set_muted", side_effect=TimeoutError("wedged")):
            b.process_frame(FRAME)  # must not raise
        self.assertTrue(b.is_muted())
        self.assertIn("[mic failed to reopen; muted (not saved)]", self.out.getvalue())
        self.assertEqual(self.wake_calls, 0)
        b.process_frame(FRAME)  # the forced mute is applied: the device close is tried again
        self.assertEqual(self.calls[-1], False)

    def test_remute_during_second_sync_drops_the_frame(self):
        def mic(enabled):
            if enabled:
                raise OSError("dead")

        b = self.bridge_with(mic)
        b.set_muted(True)
        b.process_frame(FRAME)
        self.wake_flag = True
        # an unmute lands during the control drain, so the second sync reopens (and fails)
        with mock.patch.object(b, "_drain_control", side_effect=lambda: b.set_muted(False)):
            b.process_frame(FRAME)
        self.assertTrue(b.is_muted())
        self.assertEqual((self.wake_calls, self.vad_calls, self.stt_calls), (0, 0, 0))

    def test_wedged_is_muted_fails_toward_closing_the_mic(self):
        b = self.bridge_with(lambda e: None)
        b.set_muted(True)
        with mock.patch.object(b, "is_muted", side_effect=TimeoutError("wedged")):
            b.process_frame(FRAME)  # must not raise
        self.assertEqual(self.calls, [False])

    def test_wedged_is_muted_also_forces_the_mute(self):
        b = self.bridge_with(lambda e: None)
        with mock.patch.object(b, "is_muted", side_effect=TimeoutError("wedged")):
            b._apply_mic_device()
        self.assertEqual(self.calls, [False])
        self.assertTrue(b._muted)

    def test_print_failure_in_the_failure_branches_does_not_escape(self):
        def mic(enabled):
            raise OSError("dead")

        b = self.bridge_with(mic)
        b.set_muted(True)
        with mock.patch.object(b, "_print", side_effect=OSError("closed stdout")):
            b.process_frame(FRAME)  # close failure branch
        b.set_muted(False)
        with mock.patch.object(b, "_print", side_effect=OSError("closed stdout")):
            b.process_frame(FRAME)  # reopen failure branch (its re-mute print fails too)
        self.assertTrue(b.is_muted())

    def test_force_mute_still_mutes_when_the_lock_cannot_be_taken(self):
        b = self.bridge_with(lambda e: None)
        with mock.patch("eridian.voice.FORCE_MUTE_LOCK_S", 0.01):
            self.assertTrue(b._lock.acquire(timeout=1))
            try:
                b._force_mute()  # must not raise or block
                self.assertTrue(b._muted)
            finally:
                b._lock.release()

    def make_stall_bridge(self):
        self.stalled = False
        self.out_before = len(self.out.getvalue())
        return self.reopen(mic_stalled=lambda: self.stalled)

    def test_stall_notice_once_each_way(self):
        b = self.make_stall_bridge()
        b.process_frame(FRAME)
        self.stalled = True
        b.process_frame(FRAME)
        b.process_frame(FRAME)
        self.assertEqual(self.out.getvalue().count("[mic stalled; no input]"), 1)
        self.stalled = False
        b.process_frame(FRAME)
        b.process_frame(FRAME)
        self.assertEqual(self.out.getvalue().count("[mic input back]"), 1)

    def test_no_stall_notice_while_muted(self):
        b = self.make_stall_bridge()
        b.set_muted(True)
        self.stalled = True
        b.process_frame(FRAME)
        b.process_frame(FRAME)
        self.assertNotIn("[mic stalled", self.out.getvalue())
        b.set_muted(False)
        self.stalled = False
        b.process_frame(FRAME)
        self.assertNotIn("[mic input back]", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()

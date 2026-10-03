"""Tests for undertone.screen.ScreenedSink (no audio devices)."""

import unittest

from undertone.screen import HISTORY_S, LOOKAHEAD_S, ScreenedSink

MARK = b"\x01\x00" * 160
A = b"\x02\x00" * 160
B = b"\x03\x00" * 160
ZERO = bytes(320)
CLEAN = b"\x00\x00" * 320


def clean(seconds: float) -> bytes:
    return bytes(int(seconds * 32000))


class FakeInner:
    def __init__(self) -> None:
        self.played: list[bytes] = []
        self.clears = 0
        self.is_playing = False
        self.drain_args: list = []

    def play(self, pcm: bytes) -> None:
        self.played.append(pcm)

    def clear(self) -> None:
        self.clears += 1

    @property
    def playing(self) -> bool:
        return self.is_playing

    def wait_drained(self, timeout=None) -> bool:
        self.drain_args.append(timeout)
        return True


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


class RecordingFactory:
    """Stateless: fires on MARK. Records frames seen by each detector."""

    def __init__(self) -> None:
        self.runs: list[list[bytes]] = []

    @property
    def calls(self) -> int:
        return len(self.runs)

    def __call__(self):
        frames: list[bytes] = []
        self.runs.append(frames)

        def detect(frame: bytes) -> bool:
            frames.append(frame)
            return frame == MARK

        return detect


class StatefulFactory(RecordingFactory):
    """Fires on B only after seeing A earlier in the same instance."""

    def __call__(self):
        inner = super().__call__()
        seen_a = False

        def detect(frame: bytes) -> bool:
            nonlocal seen_a
            inner(frame)
            if frame == B and seen_a:
                return True
            if frame == A:
                seen_a = True
            return False

        return detect


class LateFiringFactory(RecordingFactory):
    """Fires on a ZERO frame only after MARK was seen in the same instance."""

    def __call__(self):
        inner = super().__call__()
        seen_mark = False

        def detect(frame: bytes) -> bool:
            nonlocal seen_mark
            inner(frame)
            if frame == MARK:
                seen_mark = True
                return False
            return seen_mark and frame == ZERO

        return detect


class ScreenedSinkTest(unittest.TestCase):
    def _make(self, factory, **kw):
        self.inner = FakeInner()
        self.clock = Clock()
        self.blocked = 0

        def on_blocked() -> None:
            self.blocked += 1

        kw.setdefault("on_blocked", on_blocked)
        return ScreenedSink(self.inner, detector_factory=factory, clock=self.clock, **kw)

    def test_construction_does_not_call_factory(self) -> None:
        f = RecordingFactory()
        self._make(f)
        self.assertEqual(f.calls, 0)

    def test_clean_pcm_passes_through_identical(self) -> None:
        s = self._make(RecordingFactory())
        s.play(CLEAN)
        self.assertEqual(self.inner.played, [CLEAN])
        self.assertEqual(self.blocked, 0)

    def test_mark_is_blocked_then_clean_plays(self) -> None:
        s = self._make(RecordingFactory())
        s.play(CLEAN + MARK + CLEAN)
        self.assertEqual(self.inner.played, [])
        self.assertEqual(self.blocked, 1)
        s.play(CLEAN)
        self.assertEqual(self.inner.played, [CLEAN])
        self.assertEqual(self.blocked, 1)

    def test_late_firing_detector_is_caught_by_lookahead(self) -> None:
        s = self._make(LateFiringFactory())
        s.play(MARK)  # ends the chunk; fires only on the silence after it
        self.assertEqual(self.inner.played, [])
        self.assertEqual(self.blocked, 1)

    def test_lookahead_silence_is_appended(self) -> None:
        f = RecordingFactory()
        s = self._make(f)
        s.play(A)
        self.assertEqual(f.runs[0], [A] + [ZERO] * int(LOOKAHEAD_S * 100))

    def test_cross_chunk_via_history(self) -> None:
        s = self._make(StatefulFactory())
        s.play(A)
        s.play(B)
        self.assertEqual(self.inner.played, [A])
        self.assertEqual(self.blocked, 1)

    def test_history_cap_forgets_old_audio(self) -> None:
        s = self._make(StatefulFactory())
        s.play(A)
        for _ in range(5):
            s.play(clean(0.5))  # 2.5 s of approved clean audio, no gap
        s.play(B)
        self.assertEqual(self.inner.played[0], A)
        self.assertEqual(self.inner.played[-1], B)
        self.assertEqual(self.blocked, 0)

    def test_long_gap_empties_history(self) -> None:
        s = self._make(StatefulFactory())
        s.play(A)
        self.clock.t += 0.01 + HISTORY_S + 0.5
        s.play(B)
        self.assertEqual(self.inner.played, [A, B])
        self.assertEqual(self.blocked, 0)

    def test_short_gap_still_blocks_and_is_modeled(self) -> None:
        f = StatefulFactory()
        s = self._make(f)
        s.play(A)
        self.clock.t += 0.01 + 0.3
        s.play(B)
        self.assertEqual(self.inner.played, [A])
        self.assertEqual(self.blocked, 1)
        frames = f.runs[1]
        between = frames[frames.index(A) + 1 : frames.index(B)]
        self.assertTrue(all(fr == ZERO for fr in between))
        self.assertTrue(29 <= len(between) <= 31, len(between))

    def test_blocked_chunk_not_added_to_history(self) -> None:
        f = RecordingFactory()
        s = self._make(f)
        s.play(A)
        s.play(MARK)
        s.play(CLEAN)
        self.assertEqual(self.blocked, 1)
        self.assertNotIn(MARK, f.runs[2])
        self.assertIn(A, f.runs[2])

    def test_fresh_detector_per_nonempty_play(self) -> None:
        f = RecordingFactory()
        s = self._make(f)
        s.play(b"")
        s.play(CLEAN)
        s.play(MARK)
        s.play(CLEAN)
        self.assertEqual(f.calls, 3)

    def test_frame_alignment_across_odd_pieces(self) -> None:
        f = RecordingFactory()
        s = self._make(f)
        s.play(b"\x00\x00" * 250)  # 500 bytes
        s.play(b"\x00\x00" * 70)  # 140 bytes
        for run in f.runs:
            self.assertTrue(all(len(fr) == 320 for fr in run))
        self.assertEqual(self.inner.played, [b"\x00\x00" * 250, b"\x00\x00" * 70])

    def test_odd_length_trimmed(self) -> None:
        s = self._make(RecordingFactory())
        s.play(CLEAN + b"\x07")
        self.assertEqual(self.inner.played, [CLEAN])

    def test_empty_and_single_byte_are_noops(self) -> None:
        f = RecordingFactory()
        s = self._make(f)
        s.play(b"")
        s.play(b"\x01")
        self.assertEqual(self.inner.played, [])
        self.assertEqual(f.calls, 0)

    def test_clear_keeps_history_so_immediate_split_is_blocked(self) -> None:
        s = self._make(StatefulFactory())
        s.play(A)
        s.clear()
        self.assertEqual(self.inner.clears, 1)
        self.assertFalse(s.play(B))
        self.assertEqual(self.inner.played, [A])

    def test_clear_then_real_silence_drops_history(self) -> None:
        s = self._make(StatefulFactory())
        s.play(A)
        s.clear()
        self.clock.t += HISTORY_S + 0.5
        self.assertTrue(s.play(B))
        self.assertEqual(self.inner.clears, 1)
        self.assertEqual(self.inner.played, [A, B])

    def test_play_return_values(self) -> None:
        s = self._make(RecordingFactory())
        self.assertTrue(s.play(b""))
        self.assertTrue(s.play(b"\x01"))
        self.assertTrue(s.play(CLEAN))
        self.assertFalse(s.play(MARK))

    def _errors_sink(self, factory):
        self.errors: list[BaseException] = []
        return self._make(factory, on_error=self.errors.append)

    def test_factory_exception_fails_closed(self) -> None:
        def factory():
            raise RuntimeError("model load failed")

        s = self._errors_sink(factory)
        self.assertFalse(s.play(CLEAN))
        self.assertEqual(self.inner.played, [])
        self.assertEqual(self.blocked, 0)  # the screen broke; it did not fire
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(str(self.errors[0]), "model load failed")

    def test_detector_exception_fails_closed(self) -> None:
        def factory():
            def detect(frame: bytes) -> bool:
                raise RuntimeError("inference failed")

            return detect

        s = self._errors_sink(factory)
        self.assertFalse(s.play(CLEAN))
        self.assertEqual(self.inner.played, [])
        self.assertEqual(self.blocked, 0)
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(s._history, b"")

    def test_on_error_fires_once_but_later_failures_stay_blocked(self) -> None:
        def factory():
            raise RuntimeError("down")

        s = self._errors_sink(factory)
        self.assertFalse(s.play(CLEAN))
        self.assertFalse(s.play(CLEAN))
        self.assertFalse(s.play(CLEAN))
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(self.blocked, 0)
        self.assertEqual(self.inner.played, [])

    def test_on_error_raising_is_swallowed(self) -> None:
        def boom(exc) -> None:
            raise RuntimeError("x")

        def factory():
            raise RuntimeError("down")

        s = self._make(factory, on_error=boom)
        self.assertFalse(s.play(CLEAN))

    def test_failure_without_on_error_is_still_blocked(self) -> None:
        def factory():
            raise RuntimeError("down")

        s = self._make(factory)
        self.assertFalse(s.play(CLEAN))
        self.assertEqual(self.blocked, 0)

    def test_on_blocked_raising_is_swallowed(self) -> None:
        def boom() -> None:
            raise RuntimeError("x")

        s = self._make(RecordingFactory(), on_blocked=boom)
        s.play(MARK)
        self.assertEqual(self.inner.played, [])

    def test_no_on_blocked_is_fine(self) -> None:
        s = ScreenedSink(FakeInner(), detector_factory=RecordingFactory())
        s.play(MARK)

    def test_playing_and_wait_drained_delegate(self) -> None:
        s = self._make(RecordingFactory())
        self.assertFalse(s.playing)
        self.inner.is_playing = True
        self.assertTrue(s.playing)
        self.assertTrue(s.wait_drained(2.5))
        self.assertEqual(self.inner.drain_args, [2.5])


if __name__ == "__main__":
    unittest.main()

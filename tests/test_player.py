"""Tests for eridian.player.SpeechQueue and RenderedSpeechQueue.

ERIDIAN_SAY_CMD always points at a stub script; these tests never run real `say`.
"""

import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
import wave
from unittest import mock

from eridian import player
from eridian import speaker
from eridian.player import KokoroRenderer, RenderedSpeechQueue, SayRenderer, SpeechQueue

_RECORDING_STUB = """#!/bin/sh
echo "$@" >> "$STUB_LOG_FILE"
"""

# exec so the stub's pid IS the sleeping process: terminate leaves no orphan.
_SLEEPY_STUB = """#!/bin/sh
echo "$$" >> "$STUB_PID_FILE"
echo "$@" >> "$STUB_LOG_FILE"
exec sleep 30
"""


class SpeechQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._path = Path(self._tmp.name)
        self._log = self._path / "stub.log"
        self._pids = self._path / "stub.pids"
        self._env = mock.patch.dict(
            os.environ,
            {
                "ERIDIAN_HOME": str(self._path / "home"),
                "STUB_LOG_FILE": str(self._log),
                "STUB_PID_FILE": str(self._pids),
                "ERIDIAN_SAY_CMD": self._stub("recording.sh", _RECORDING_STUB),
                "ERIDIAN_VOICE": "",
            },
        )
        self._env.start()
        self.queues: list[SpeechQueue] = []

    def tearDown(self) -> None:
        for q in self.queues:
            q.close()
        self._env.stop()
        self._tmp.cleanup()

    def _stub(self, name: str, body: str) -> str:
        p = self._path / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return str(p)

    def _queue(self, **kw) -> SpeechQueue:
        q = SpeechQueue(**kw)
        self.queues.append(q)
        return q

    def _use_sleepy(self) -> None:
        os.environ["ERIDIAN_SAY_CMD"] = self._stub("sleepy.sh", _SLEEPY_STUB)

    def _lines(self) -> list[str]:
        return self._log.read_text().splitlines() if self._log.exists() else []

    def _wait_for(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_order_preserved(self) -> None:
        q = self._queue()
        for t in ("one", "two", "three"):
            q.enqueue(t)
        self.assertTrue(q.wait_idle(5.0))
        self.assertEqual(self._lines(), ["-- one", "-- two", "-- three"])

    def test_wake_phrase_is_never_spoken(self) -> None:
        q = self._queue()
        q.enqueue("Say hey jarvis now.")
        self.assertTrue(q.wait_idle(5.0))
        lines = self._lines()
        self.assertEqual(lines, ["-- Say the wake word now."])
        self.assertNotIn("jarvis", " ".join(lines).lower())

    def test_stop_kills_current_and_drops_pending(self) -> None:
        self._use_sleepy()
        q = self._queue()
        q.enqueue("first")
        q.enqueue("second")
        q.enqueue("third")
        self.assertTrue(self._wait_for(lambda: self._lines() == ["-- first"]))
        start = time.monotonic()
        q.stop()
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertTrue(q.wait_idle(3.0))
        time.sleep(0.2)
        self.assertEqual(self._lines(), ["-- first"])

    def test_own_voice_reaches_build_say_argv_in_order(self) -> None:
        os.environ["ERIDIAN_VOICE"] = "Evan"
        os.environ["ERIDIAN_OWN_VOICE"] = "Fred"
        q = self._queue()
        q.enqueue("plain")
        q.enqueue("mine", own=True)
        q.enqueue("plain again")
        self.assertTrue(q.wait_idle(5.0))
        self.assertEqual(
            self._lines(),
            ["-v Evan -- plain", "-v Fred -- mine", "-v Evan -- plain again"],
        )

    def test_enqueue_after_stop_plays(self) -> None:
        self._use_sleepy()
        q = self._queue()
        q.enqueue("old")
        self.assertTrue(self._wait_for(lambda: self._lines() == ["-- old"]))
        q.stop()
        os.environ["ERIDIAN_SAY_CMD"] = self._stub("recording.sh", _RECORDING_STUB)
        q.enqueue("fresh")
        self.assertTrue(q.wait_idle(5.0))
        self.assertEqual(self._lines(), ["-- old", "-- fresh"])

    def test_wait_idle_true_after_finish_false_on_timeout_while_sleeping(self) -> None:
        q = self._queue()
        q.enqueue("quick")
        self.assertTrue(q.wait_idle(5.0))
        self.assertFalse(q.busy)

        self._use_sleepy()
        q.enqueue("long")
        self.assertTrue(self._wait_for(lambda: len(self._lines()) == 2))
        self.assertTrue(q.busy)
        self.assertFalse(q.wait_idle(0.2))

    def test_idle_queue_wait_idle_true(self) -> None:
        self.assertTrue(self._queue().wait_idle(0.1))

    def test_on_start_called_once_per_item(self) -> None:
        started: list[str] = []
        q = self._queue(on_start=started.append)
        q.enqueue("a")
        q.enqueue("b")
        self.assertTrue(q.wait_idle(5.0))
        self.assertEqual(started, ["a", "b"])

    def test_raising_on_start_does_not_break_playback(self) -> None:
        def boom(_text: str) -> None:
            raise RuntimeError("nope")

        q = self._queue(on_start=boom)
        q.enqueue("a")
        q.enqueue("b")
        self.assertTrue(q.wait_idle(5.0))
        self.assertEqual(self._lines(), ["-- a", "-- b"])

    def test_nul_only_text_skipped(self) -> None:
        started: list[str] = []
        q = self._queue(on_start=started.append)
        q.enqueue("\x00\x00")
        q.enqueue("ok")
        self.assertTrue(q.wait_idle(5.0))
        self.assertEqual(self._lines(), ["-- ok"])
        self.assertEqual(started, ["ok"])

    def test_close_leaves_no_child_running(self) -> None:
        self._use_sleepy()
        q = self._queue()
        q.enqueue("x")
        self.assertTrue(self._wait_for(self._pids.exists))
        pid = int(self._pids.read_text().split()[0])
        q.close()
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_double_dash_precedes_flag_like_text(self) -> None:
        q = self._queue()
        q.enqueue("-o/tmp/x")
        self.assertTrue(q.wait_idle(5.0))
        self.assertEqual(self._lines(), ["-- -o/tmp/x"])


# Renders by copying $STUB_WAV to the path after -o (a stand-in for `say -o`).
_RENDER_STUB = """#!/bin/sh
echo "$@" >> "$STUB_LOG_FILE"
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; echo "$out" >> "$STUB_OUT_FILE"; fi
  shift
done
cp "$STUB_WAV" "$out"
"""

_SLEEPY_RENDER_STUB = """#!/bin/sh
echo "$$" >> "$STUB_PID_FILE"
exec sleep 30
"""


def _write_wav(path: Path, frames: bytes, *, rate: int = 16000, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


class FakeSink:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._playing = False
        self.played: list[bytes] = []
        self.clears = 0
        self.auto_drain = False  # True: play() leaves the sink idle (instant playback)

    def play(self, pcm: bytes) -> None:
        with self._cond:
            self.played.append(pcm)
            self._playing = not self.auto_drain

    def clear(self) -> None:
        with self._cond:
            self.clears += 1
            self._playing = False
            self._cond.notify_all()

    @property
    def playing(self) -> bool:
        with self._cond:
            return self._playing

    def drain(self) -> None:
        with self._cond:
            self._playing = False
            self._cond.notify_all()

    def wait_drained(self, timeout: float | None = None) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: not self._playing, timeout)


class RenderedSpeechQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._path = Path(self._tmp.name)
        self._log = self._path / "stub.log"
        self._pids = self._path / "stub.pids"
        self._outs = self._path / "stub.outs"
        self._wav = self._path / "fixture.wav"
        self.frames = bytes(range(64)) * 4  # 128 int16 samples
        _write_wav(self._wav, self.frames)
        self._env = mock.patch.dict(
            os.environ,
            {
                "ERIDIAN_HOME": str(self._path / "home"),
                "STUB_LOG_FILE": str(self._log),
                "STUB_PID_FILE": str(self._pids),
                "STUB_OUT_FILE": str(self._outs),
                "STUB_WAV": str(self._wav),
                "ERIDIAN_SAY_CMD": self._stub("render.sh", _RENDER_STUB),
                "ERIDIAN_VOICE": "",
            },
        )
        self._env.start()
        self.sink = FakeSink()
        self.sink.auto_drain = True
        self.queues: list[RenderedSpeechQueue] = []

    def tearDown(self) -> None:
        for q in self.queues:
            q.close()
        self._env.stop()
        self._tmp.cleanup()

    def _stub(self, name: str, body: str) -> str:
        p = self._path / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return str(p)

    def _queue(self, **kw) -> RenderedSpeechQueue:
        q = RenderedSpeechQueue(sink=self.sink, **kw)
        self.queues.append(q)
        return q

    def _wait_for(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_say_renderer_passes_own_voice_to_say(self) -> None:
        os.environ["ERIDIAN_VOICE"] = "Evan"
        os.environ["ERIDIAN_OWN_VOICE"] = "Fred"
        q = self._queue()
        q.enqueue("plain")
        q.enqueue("mine", own=True)
        self.assertTrue(q.wait_idle(5.0))
        lines = self._log.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("-v Evan "), lines[0])
        self.assertTrue(lines[1].startswith("-v Fred "), lines[1])

    def test_pcm_delivered_in_order_and_equals_fixture(self) -> None:
        other = self._path / "other.wav"
        _write_wav(other, b"\x01\x00" * 50)
        q = self._queue()
        q.enqueue("one")
        self.assertTrue(q.wait_idle(5.0))
        os.environ["STUB_WAV"] = str(other)
        q.enqueue("two")
        self.assertTrue(q.wait_idle(5.0))
        self.assertEqual(self.sink.played, [self.frames, b"\x01\x00" * 50])

    def test_argv_uses_render_flags(self) -> None:
        q = self._queue()
        q.enqueue("hi")
        self.assertTrue(q.wait_idle(5.0))
        line = self._log.read_text().strip()
        self.assertIn("--file-format=WAVE --data-format=LEI16@16000 -- hi", line)

    def test_on_start_called_per_item_and_after_play(self) -> None:
        seen: list[tuple[str, int]] = []
        q = self._queue(on_start=lambda t: seen.append((t, len(self.sink.played))))
        q.enqueue("a")
        q.enqueue("b")
        self.assertTrue(q.wait_idle(5.0))
        self.assertEqual(seen, [("a", 1), ("b", 2)])

    def test_raising_on_start_does_not_break_playback(self) -> None:
        def boom(_text: str) -> None:
            raise RuntimeError("nope")

        q = self._queue(on_start=boom)
        q.enqueue("a")
        q.enqueue("b")
        self.assertTrue(q.wait_idle(5.0))
        self.assertEqual(len(self.sink.played), 2)

    def test_wrong_format_wav_is_dropped(self) -> None:
        bad = self._path / "bad.wav"
        started: list[str] = []
        q = self._queue(on_start=started.append)
        for kw in ({"rate": 22050}, {"channels": 2}):
            _write_wav(bad, self.frames, **kw)
            os.environ["STUB_WAV"] = str(bad)
            q.enqueue("bad")
            self.assertTrue(q.wait_idle(5.0))
        self.assertEqual(self.sink.played, [])
        self.assertEqual(started, [])

    def test_missing_output_file_is_dropped(self) -> None:
        os.environ["STUB_WAV"] = str(self._path / "nonexistent.wav")
        q = self._queue()
        q.enqueue("x")
        self.assertTrue(q.wait_idle(5.0))
        self.assertEqual(self.sink.played, [])

    def test_render_files_unlinked_and_dir_removed_on_close(self) -> None:
        q = self._queue()
        q.enqueue("a")
        q.enqueue("b")
        self.assertTrue(q.wait_idle(5.0))
        outs = self._outs.read_text().split()
        self.assertEqual(len(outs), 2)
        for o in outs:
            self.assertFalse(os.path.exists(o))
        render_dir = os.path.dirname(outs[0])
        self.assertTrue(os.path.isdir(render_dir))
        self.assertEqual(os.stat(render_dir).st_mode & 0o777, 0o700)
        q.close()
        self.assertFalse(os.path.exists(render_dir))

    def test_stop_kills_sleepy_render_and_plays_nothing(self) -> None:
        os.environ["ERIDIAN_SAY_CMD"] = self._stub("sleepy.sh", _SLEEPY_RENDER_STUB)
        q = self._queue()
        q.enqueue("first")
        q.enqueue("second")
        self.assertTrue(self._wait_for(self._pids.exists))
        pid = int(self._pids.read_text().split()[0])
        start = time.monotonic()
        q.stop()
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertGreaterEqual(self.sink.clears, 1)
        self.assertTrue(q.wait_idle(3.0))
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(self.sink.played, [])
        self.assertEqual(len(self._pids.read_text().split()), 1)  # "second" was dropped

    def test_stop_between_render_and_play_plays_nothing(self) -> None:
        q = self._queue()
        real = player._read_pcm

        def read_then_stop(path):
            pcm = real(path)
            q.stop()  # lands after the render finished, before the sink hand-off
            return pcm

        with mock.patch.object(player, "_read_pcm", read_then_stop):
            q.enqueue("racy")
            self.assertTrue(q.wait_idle(5.0))
        self.assertEqual(self.sink.played, [])
        q.enqueue("after")
        self.assertTrue(q.wait_idle(5.0))
        self.assertEqual(self.sink.played, [self.frames])

    def test_stop_between_dequeue_and_spawn_prevents_the_spawn(self) -> None:
        q = self._queue()
        real = speaker.build_say_argv

        def build_then_stop(*args, **kwargs):
            argv = real(*args, **kwargs)
            q.stop()  # lands after dequeue, before the renderer's pre-spawn check
            return argv

        with mock.patch.object(speaker, "build_say_argv", build_then_stop):
            q.enqueue("racy")
            self.assertTrue(q.wait_idle(5.0))
        self.assertFalse(self._log.exists())  # the stub never ran
        self.assertEqual(self.sink.played, [])

    def test_busy_reflects_sink_playing(self) -> None:
        self.sink.auto_drain = False
        q = self._queue()
        self.assertFalse(q.busy)
        q.enqueue("a")
        self.assertTrue(self._wait_for(lambda: self.sink.playing))
        self.assertTrue(q.busy)
        self.sink.drain()
        self.assertTrue(self._wait_for(lambda: not q.busy))

    def test_busy_true_while_rendering(self) -> None:
        os.environ["ERIDIAN_SAY_CMD"] = self._stub("sleepy.sh", _SLEEPY_RENDER_STUB)
        q = self._queue()
        q.enqueue("a")
        self.assertTrue(self._wait_for(self._pids.exists))
        self.assertTrue(q.busy)

    def test_wait_idle_times_out_while_playing_then_true_when_drained(self) -> None:
        self.sink.auto_drain = False
        q = self._queue()
        q.enqueue("a")
        self.assertTrue(self._wait_for(lambda: self.sink.playing))
        self.assertFalse(q.wait_idle(0.2))
        threading.Timer(0.1, self.sink.drain).start()
        self.assertTrue(q.wait_idle(3.0))

    def test_idle_queue_wait_idle_true(self) -> None:
        self.assertTrue(self._queue().wait_idle(0.1))

    def test_empty_text_is_skipped(self) -> None:
        started: list[str] = []
        q = self._queue(on_start=started.append)
        q.enqueue("\x00\x00")
        q.enqueue("ok")
        self.assertTrue(q.wait_idle(5.0))
        self.assertEqual(started, ["ok"])
        self.assertEqual(self.sink.played, [self.frames])
        self.assertEqual(len(self._log.read_text().splitlines()), 1)

    def test_enqueue_after_close_is_ignored(self) -> None:
        q = self._queue()
        q.close()
        q.enqueue("late")
        self.assertEqual(self.sink.played, [])



class FakeVoice:
    def __init__(self, pieces: list[str], pcms: dict[str, bytes | None]) -> None:
        self._pieces = pieces
        self._pcms = pcms
        self.rendered: list[str] = []
        self.own_flags: list[bool] = []

    def pieces(self, phonemes: str) -> list[str]:
        return list(self._pieces)

    def render(self, piece: str, *, own: bool = False) -> bytes | None:
        self.rendered.append(piece)
        self.own_flags.append(own)
        return self._pcms[piece]


class KokoroRendererTest(unittest.TestCase):
    def _render(self, voice, wanted=lambda: True, phonemes="ph", **kw) -> list[bytes]:
        with mock.patch.object(speaker, "espeak_phonemes", return_value=phonemes):
            return list(KokoroRenderer(voice).render("hello", wanted, **kw))

    def test_own_reaches_every_piece_render(self) -> None:
        voice = FakeVoice(["a", "b"], {"a": b"\x01\x00", "b": b"\x02\x00"})
        self._render(voice, own=True)
        self.assertEqual(voice.own_flags, [True, True])

    def test_own_defaults_to_false(self) -> None:
        voice = FakeVoice(["a", "b"], {"a": b"\x01\x00", "b": b"\x02\x00"})
        self._render(voice)
        self.assertEqual(voice.own_flags, [False, False])

    def test_yields_per_piece_in_order(self) -> None:
        voice = FakeVoice(["a", "b", "c"], {"a": b"\x01\x00", "b": b"\x02\x00", "c": b"\x03\x00"})
        self.assertEqual(self._render(voice), [b"\x01\x00", b"\x02\x00", b"\x03\x00"])
        self.assertEqual(voice.rendered, ["a", "b", "c"])

    def test_none_phonemes_yields_nothing(self) -> None:
        voice = FakeVoice(["a"], {"a": b"\x01\x00"})
        self.assertEqual(self._render(voice, phonemes=None), [])
        self.assertEqual(voice.rendered, [])

    def test_not_wanted_before_second_piece_skips_it(self) -> None:
        voice = FakeVoice(["a", "b"], {"a": b"\x01\x00", "b": b"\x02\x00"})
        self.assertEqual(self._render(voice, wanted=lambda: not voice.rendered), [b"\x01\x00"])
        self.assertEqual(voice.rendered, ["a"])

    def test_none_render_is_skipped(self) -> None:
        voice = FakeVoice(["a", "b", "c"], {"a": b"\x01\x00", "b": None, "c": b"\x03\x00"})
        self.assertEqual(self._render(voice), [b"\x01\x00", b"\x03\x00"])
        self.assertEqual(voice.rendered, ["a", "b", "c"])


class StubRenderer:
    """Yields the given pieces; optionally runs a hook before each yield."""

    def __init__(self, pieces: list[bytes], before_each=None) -> None:
        self.pieces = pieces
        self.before_each = before_each
        self.cancels = 0
        self.closed = 0
        self.calls: list[tuple[str, bool]] = []

    def render(self, text, wanted, *, own=False):
        self.calls.append((text, own))
        for i, pcm in enumerate(self.pieces):
            if self.before_each is not None:
                self.before_each(i)
            yield pcm

    def cancel(self) -> None:
        self.cancels += 1

    def close(self) -> None:
        self.closed += 1


class PickySink(FakeSink):
    """A sink whose play() returns False for the 0-based call indices in `reject`."""

    def __init__(self, reject: set[int]) -> None:
        super().__init__()
        self.auto_drain = True
        self.reject = reject
        self.calls = 0

    def play(self, pcm: bytes) -> bool:
        idx = self.calls
        self.calls += 1
        if idx in self.reject:
            return False
        super().play(pcm)
        return True


class RenderedSpeechQueueRendererTest(unittest.TestCase):
    def setUp(self) -> None:
        self.queues: list[RenderedSpeechQueue] = []

    def tearDown(self) -> None:
        for q in self.queues:
            q.close()

    def _queue(self, sink, renderer, on_start=None) -> RenderedSpeechQueue:
        q = RenderedSpeechQueue(on_start, sink=sink, renderer=renderer)
        self.queues.append(q)
        return q

    def test_own_reaches_the_renderer_in_order_and_defaults_false(self) -> None:
        renderer = StubRenderer([b"\x01\x00"])
        sink = FakeSink()
        sink.auto_drain = True
        q = self._queue(sink, renderer)
        q.enqueue("one")
        q.enqueue("two", own=True)
        q.enqueue("three")
        q.enqueue("four", own=True)
        self.assertTrue(q.wait_idle(3.0))
        self.assertEqual(
            renderer.calls, [("one", False), ("two", True), ("three", False), ("four", True)]
        )

    def test_rejected_piece_is_skipped_and_on_start_fires_once(self) -> None:
        sink = PickySink({1})
        started: list[str] = []
        q = self._queue(sink, StubRenderer([b"\x01\x00", b"\x02\x00", b"\x03\x00"]), started.append)
        q.enqueue("hi")
        self.assertTrue(q.wait_idle(3.0))
        self.assertEqual(sink.played, [b"\x01\x00", b"\x03\x00"])
        self.assertEqual(started, ["hi"])

    def test_on_start_follows_first_accepted_piece(self) -> None:
        sink = PickySink({0})
        started: list[str] = []
        q = self._queue(sink, StubRenderer([b"\x01\x00", b"\x02\x00"]), started.append)
        q.enqueue("hi")
        self.assertTrue(q.wait_idle(3.0))
        self.assertEqual(sink.played, [b"\x02\x00"])
        self.assertEqual(started, ["hi"])

    def test_all_pieces_rejected_never_calls_on_start(self) -> None:
        sink = PickySink({0, 1, 2})
        started: list[str] = []
        q = self._queue(sink, StubRenderer([b"\x01\x00"] * 3), started.append)
        q.enqueue("hi")
        self.assertTrue(q.wait_idle(3.0))
        self.assertEqual(sink.played, [])
        self.assertEqual(started, [])

    def test_none_from_sink_counts_as_played(self) -> None:
        sink = FakeSink()
        sink.auto_drain = True
        started: list[str] = []
        q = self._queue(sink, StubRenderer([b"\x01\x00"]), started.append)
        q.enqueue("hi")
        self.assertTrue(q.wait_idle(3.0))
        self.assertEqual(started, ["hi"])

    def test_stop_mid_item_sends_no_further_pieces(self) -> None:
        sink = FakeSink()
        sink.auto_drain = True
        holder: dict = {}

        def before_each(i: int) -> None:
            if i == 1:
                holder["q"].stop()  # lands between piece 1 and piece 2

        q = self._queue(sink, StubRenderer([b"\x01\x00", b"\x02\x00", b"\x03\x00"], before_each))
        holder["q"] = q
        q.enqueue("hi")
        self.assertTrue(q.wait_idle(3.0))
        self.assertEqual(sink.played, [b"\x01\x00"])

    def test_stop_cancels_renderer_and_close_closes_it(self) -> None:
        sink = FakeSink()
        renderer = StubRenderer([])
        q = self._queue(sink, renderer)
        q.stop()
        self.assertEqual(renderer.cancels, 1)
        self.assertGreaterEqual(sink.clears, 1)
        q.close()
        self.assertEqual(renderer.closed, 1)

    def test_default_renderer_is_say(self) -> None:
        q = RenderedSpeechQueue(sink=FakeSink())
        self.queues.append(q)
        self.assertIsInstance(q._renderer, SayRenderer)


if __name__ == "__main__":
    unittest.main()

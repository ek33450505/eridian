"""Tests for undertone.player.SpeechQueue.

UNDERTONE_SAY_CMD always points at a stub script; these tests never run real `say`.
"""

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from undertone.player import SpeechQueue

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
                "UNDERTONE_HOME": str(self._path / "home"),
                "STUB_LOG_FILE": str(self._log),
                "STUB_PID_FILE": str(self._pids),
                "UNDERTONE_SAY_CMD": self._stub("recording.sh", _RECORDING_STUB),
                "UNDERTONE_VOICE": "",
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
        os.environ["UNDERTONE_SAY_CMD"] = self._stub("sleepy.sh", _SLEEPY_STUB)

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

    def test_enqueue_after_stop_plays(self) -> None:
        self._use_sleepy()
        q = self._queue()
        q.enqueue("old")
        self.assertTrue(self._wait_for(lambda: self._lines() == ["-- old"]))
        q.stop()
        os.environ["UNDERTONE_SAY_CMD"] = self._stub("recording.sh", _RECORDING_STUB)
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


if __name__ == "__main__":
    unittest.main()

"""Integration tests for undertone.speaker against a real unix socket.

UNDERTONE_SAY_CMD always points at a stub script that records its args to a
file instead of producing audio -- these tests must never shell out to `say`.
"""

import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from undertone import paths, speaker

# Stub that appends its argv to $STUB_LOG_FILE, one line per invocation,
# then exits immediately.
_RECORDING_STUB = """#!/bin/sh
echo "$@" >> "$STUB_LOG_FILE"
"""

# Stub that records its argv, then sleeps -- long enough for a test to
# observe it still running and to exercise `stop` / interrupt-by-new-say.
_SLEEPY_STUB = """#!/bin/sh
echo "$@" >> "$STUB_LOG_FILE"
sleep 5
"""


def _write_stub(dir_path: Path, name: str, body: str) -> str:
    stub_path = dir_path / name
    stub_path.write_text(body)
    stub_path.chmod(stub_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(stub_path)


class SpeakerIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._log_file = self._tmp_path / "stub.log"
        self._home = self._tmp_path / "home"

        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "UNDERTONE_HOME": str(self._home),
                "STUB_LOG_FILE": str(self._log_file),
                "UNDERTONE_SAY_CMD": self._write_stub("recording_stub.sh", _RECORDING_STUB),
                "UNDERTONE_VOICE": "",
            },
            clear=False,
        )
        self._env_patch.start()

        self._stop_event = threading.Event()
        self._server_thread = threading.Thread(
            target=speaker.serve, args=(self._stop_event,), daemon=True
        )
        self._server_thread.start()
        self._wait_for_daemon_up()

    def tearDown(self) -> None:
        self._stop_event.set()
        self._server_thread.join(timeout=2.0)
        self._env_patch.stop()
        self._tmp.cleanup()

    def _write_stub(self, name: str, body: str) -> str:
        return _write_stub(self._tmp_path, name, body)

    def _wait_for_daemon_up(self) -> None:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if speaker.send({"op": "ping"}, timeout=0.2):
                return
            time.sleep(0.02)
        self.fail("speaker daemon did not come up in time")

    def _read_log_lines(self) -> list[str]:
        if not self._log_file.exists():
            return []
        return [line for line in self._log_file.read_text().splitlines() if line]

    def _wait_for_log_lines(self, count: int, timeout: float = 2.0) -> list[str]:
        deadline = time.time() + timeout
        lines = self._read_log_lines()
        while time.time() < deadline and len(lines) < count:
            time.sleep(0.02)
            lines = self._read_log_lines()
        return lines

    def test_ping_reports_ok(self) -> None:
        self.assertTrue(speaker.send({"op": "ping"}, timeout=0.5))

    def test_say_is_recorded_by_stub(self) -> None:
        self.assertTrue(speaker.send({"op": "say", "text": "hello world"}))
        deadline = time.time() + 2.0
        while time.time() < deadline and not self._read_log_lines():
            time.sleep(0.02)
        lines = self._read_log_lines()
        self.assertEqual(len(lines), 1)
        # "--" is injected before the text to stop `say` from parsing it as
        # flags (see test_speaker_security.ArgvInjectionTest).
        self.assertEqual(lines[0], "-- hello world")

    def test_socket_permissions_are_0600(self) -> None:
        mode = paths.socket_path().stat().st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_stop_kills_a_long_running_stub(self) -> None:
        sleepy_cmd = self._write_stub("sleepy_stub.sh", _SLEEPY_STUB)
        with mock.patch.dict(os.environ, {"UNDERTONE_SAY_CMD": sleepy_cmd}):
            self.assertTrue(speaker.send({"op": "say", "text": "long speech"}))
            self._wait_for_log_lines(1)  # confirm it actually started before stopping
            self.assertTrue(speaker.send({"op": "stop"}))
        # If the sleepy stub were still running, `_current_process` would
        # still be alive; confirm the daemon itself is still responsive,
        # which it would not be if `stop` left things in a bad state.
        self.assertTrue(speaker.send({"op": "ping"}, timeout=0.5))

    def test_new_say_interrupts_current_speech(self) -> None:
        sleepy_cmd = self._write_stub("sleepy_stub.sh", _SLEEPY_STUB)
        with mock.patch.dict(os.environ, {"UNDERTONE_SAY_CMD": sleepy_cmd}):
            self.assertTrue(speaker.send({"op": "say", "text": "first"}))
            self._wait_for_log_lines(1)  # confirm "first" actually started before interrupting
            with mock.patch.dict(
                os.environ,
                {"UNDERTONE_SAY_CMD": self._write_stub("recording_stub.sh", _RECORDING_STUB)},
            ):
                self.assertTrue(speaker.send({"op": "say", "text": "second"}))
        lines = self._wait_for_log_lines(2)
        self.assertEqual(lines, ["-- first", "-- second"])

    def test_unknown_op_returns_not_ok(self) -> None:
        self.assertFalse(speaker.send({"op": "nonsense"}))

    def test_send_returns_false_when_daemon_down(self) -> None:
        self._stop_event.set()
        self._server_thread.join(timeout=2.0)
        self.assertFalse(speaker.send({"op": "ping"}, timeout=0.3))


if __name__ == "__main__":
    unittest.main()

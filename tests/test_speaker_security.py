"""Security-hardening tests for undertone.speaker (post-review fixes).

Covers: argv flag-injection into the TTS command, unbounded read buffers,
slow-client starvation of the single-threaded accept loop, and refusing to
clobber a non-socket file sitting at the socket path. UNDERTONE_SAY_CMD
always points at a stub that records args to a file -- never real audio.
"""

import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from undertone import paths, speaker

_RECORDING_STUB = """#!/bin/sh
echo "$@" >> "$STUB_LOG_FILE"
"""


def _write_stub(dir_path: Path, name: str, body: str) -> str:
    stub_path = dir_path / name
    stub_path.write_text(body)
    stub_path.chmod(stub_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(stub_path)


class BuildSayArgvSanitizingTest(unittest.TestCase):
    def test_wake_phrase_and_say_commands_never_reach_argv(self) -> None:
        with mock.patch.dict(os.environ, {"UNDERTONE_SAY_CMD": "say"}):
            argv = speaker.build_say_argv("Say hey Jarvis [[inpt PHON]]now[[inpt TEXT]], J.A.R.V.I.S.")
        self.assertIsNotNone(argv)
        text = argv[-1]
        self.assertEqual(argv[-2], "--")
        self.assertNotIn("[[", text)
        self.assertNotIn("]]", text)
        self.assertNotIn("jarvis", text.lower())
        self.assertIn("the wake word", text)


class _ServerBackedTestCase(unittest.TestCase):
    """Starts a real speaker.serve() in a background thread against a temp home."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._log_file = self._tmp_path / "stub.log"

        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "UNDERTONE_HOME": str(self._tmp_path / "home"),
                "STUB_LOG_FILE": str(self._log_file),
                "UNDERTONE_SAY_CMD": _write_stub(self._tmp_path, "recording_stub.sh", _RECORDING_STUB),
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

    def _raw_connect(self, timeout: float = 2.0) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(paths.socket_path()))
        return sock

    def _assert_daemon_responsive(self, timeout: float = 2.0) -> None:
        # A single ping with a short timeout can flake under CPU contention
        # (this machine runs several concurrent agent sessions); retry briefly
        # before concluding the daemon actually died.
        deadline = time.time() + timeout
        while time.time() < deadline:
            if speaker.send({"op": "ping"}, timeout=0.3):
                return
            time.sleep(0.05)
        self.fail("daemon did not respond to ping within the retry window")


class ArgvInjectionTest(_ServerBackedTestCase):
    def test_text_starting_with_dash_is_passed_after_double_dash(self) -> None:
        self.assertTrue(speaker.send({"op": "say", "text": "-o/tmp/pwned"}))
        lines = self._wait_for_log_lines(1)
        self.assertEqual(len(lines), 1)
        # The stub's `echo "$@"` joins argv with spaces -- "--" must appear
        # immediately before the (otherwise flag-shaped) text, and the text
        # itself must arrive byte-for-byte, not swallowed as an option.
        self.assertEqual(lines[0], "-- -o/tmp/pwned")


class CrashResistanceTest(_ServerBackedTestCase):
    """Malformed single connections must never take the daemon down."""

    def _send_raw_line(self, raw: bytes) -> None:
        sock = self._raw_connect(timeout=2.0)
        with sock:
            sock.sendall(raw + b"\n")
            try:
                sock.recv(4096)
            except OSError:
                pass

    def test_nul_byte_in_text_does_not_crash_daemon(self) -> None:
        # subprocess.Popen raises ValueError("embedded null byte") on this --
        # the daemon must strip it (not die) and keep serving.
        self.assertTrue(speaker.send({"op": "say", "text": "hello\x00world"}))
        lines = self._wait_for_log_lines(1)
        self.assertEqual(lines, ["-- helloworld"])
        self._assert_daemon_responsive()

    def test_non_dict_json_top_level_does_not_crash_daemon(self) -> None:
        for payload in (b"[1]", b'"just a string"', b"42"):
            with self.subTest(payload=payload):
                self._send_raw_line(payload)
                self._assert_daemon_responsive()

    def test_non_str_text_does_not_crash_daemon(self) -> None:
        self.assertTrue(speaker.send({"op": "say", "text": 12345}))
        self._assert_daemon_responsive()

    def test_unknown_op_does_not_crash_daemon(self) -> None:
        self.assertFalse(speaker.send({"op": "do-a-barrel-roll"}))
        self._assert_daemon_responsive()


class BufferCapTest(_ServerBackedTestCase):
    def test_oversized_message_closes_connection_without_responding(self) -> None:
        sock = self._raw_connect(timeout=3.0)
        with sock:
            oversized = b"a" * (64 * 1024 + 1)  # one over the cap, no newline
            sock.sendall(oversized)
            # Server must drop the connection rather than keep buffering.
            reply = sock.recv(4096)
        self.assertEqual(reply, b"")  # EOF: server closed without responding

    def test_daemon_still_serves_normal_requests_after_oversized_attempt(self) -> None:
        sock = self._raw_connect(timeout=3.0)
        with sock:
            sock.sendall(b"a" * (64 * 1024 + 1))
            sock.recv(4096)
        self._assert_daemon_responsive()


class SlowClientTest(_ServerBackedTestCase):
    def test_trickling_client_is_dropped_within_one_connection_deadline(self) -> None:
        sock = self._raw_connect(timeout=3.0)
        start = time.monotonic()
        dropped = False
        with sock:
            try:
                for _ in range(6):  # ~1.8s of trickling, one byte at a time
                    sock.sendall(b"x")
                    time.sleep(0.3)
                sock.recv(4096)  # if we get here without EOF/timeout, it wasn't dropped
            except (OSError, socket.timeout):
                dropped = True
            else:
                # recv() returning b"" also counts as dropped.
                pass
        elapsed = time.monotonic() - start
        self.assertTrue(dropped or elapsed < 1.8, "server never dropped the trickling client")
        self.assertLess(elapsed, 1.8, "connection deadline did not fire promptly")

    def test_normal_client_served_right_after_a_dropped_slow_one(self) -> None:
        slow_sock = self._raw_connect(timeout=3.0)
        with slow_sock:
            for _ in range(4):
                try:
                    slow_sock.sendall(b"x")
                except OSError:
                    break
                time.sleep(0.3)
        # Whether or not the slow client noticed, the server's accept loop
        # must be free to serve the next connection immediately.
        self._assert_daemon_responsive()


class VoiceSelectionTest(unittest.TestCase):
    """Pure argv tests for build_say_argv -- `say` is never run."""

    def _argv(self, voice: str | None) -> list[str] | None:
        with mock.patch.dict(os.environ, {"UNDERTONE_SAY_CMD": "say-stub"}, clear=False):
            if voice is None:
                os.environ.pop("UNDERTONE_VOICE", None)
            else:
                os.environ["UNDERTONE_VOICE"] = voice
            return speaker.build_say_argv("hello")

    def test_default_voice_is_evan_enhanced(self) -> None:
        self.assertEqual(
            self._argv(None), ["say-stub", "-v", "Evan (Enhanced)", "--", "hello"]
        )

    def test_env_override_selects_voice(self) -> None:
        self.assertEqual(
            self._argv("Zoe (Premium)"), ["say-stub", "-v", "Zoe (Premium)", "--", "hello"]
        )

    def test_empty_voice_omits_v_flag(self) -> None:
        self.assertEqual(self._argv(""), ["say-stub", "--", "hello"])


class StaleSocketPathTest(unittest.TestCase):
    """serve() must never delete a path it doesn't own. No server runs here."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env_patch = mock.patch.dict(
            os.environ, {"UNDERTONE_HOME": self._tmp.name}, clear=False
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._tmp.cleanup()

    def test_refuses_to_start_over_a_regular_file(self) -> None:
        sock_path = paths.socket_path()
        sock_path.write_text("not a socket, please don't delete me")

        # serve() must bail out before ever reaching the accept loop, so an
        # unset stop_event is fine -- there's nothing to stop.
        rc = speaker.serve(threading.Event())

        self.assertEqual(rc, 1)
        self.assertTrue(sock_path.exists())
        self.assertEqual(sock_path.read_text(), "not a socket, please don't delete me")

    def test_refuses_to_start_over_a_symlink(self) -> None:
        sock_path = paths.socket_path()
        target = Path(self._tmp.name) / "symlink-target"
        target.write_text("victim")
        os.symlink(target, sock_path)

        rc = speaker.serve(threading.Event())

        self.assertEqual(rc, 1)
        self.assertTrue(os.path.islink(sock_path))
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(), "victim")


if __name__ == "__main__":
    unittest.main()

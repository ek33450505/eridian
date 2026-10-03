"""Tests for the announce hook command (undertone.announce)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from undertone import announce, cli

TOK = "0123456789abcdef0123456789abcdef"


class EventForTests(unittest.TestCase):
    def test_table(self) -> None:
        sub_run = {"type": "subagent", "status": "running"}
        cases = [
            ("stop", {"background_tasks": []}, "finished"),
            ("stop", {}, "finished"),
            ("stop", {"background_tasks": "x"}, "finished"),
            ("stop", {"background_tasks": [sub_run]}, None),
            ("stop", {"background_tasks": [{"type": "shell", "status": "running"}, sub_run]}, None),
            ("stop", {"background_tasks": [{"type": "subagent", "status": "completed"}]}, "finished"),
            ("stop", {"background_tasks": [{"type": "shell", "status": "running"}]}, "finished"),
            ("stop", {"background_tasks": ["junk", 3]}, "finished"),
            ("stop", {"stop_hook_active": True}, "finished"),
            ("stop", ["not", "a", "dict"], "finished"),
            ("stop", None, "finished"),
            ("stopfailure", {"error": "rate_limit"}, "error"),
            ("stopfailure", None, "error"),
            ("permissionrequest", {"tool_name": "Read"}, "permission"),
            ("permissionrequest", {"tool_name": "AskUserQuestion"}, "question"),
            ("permissionrequest", {"tool_name": "ExitPlanMode"}, "plan"),
            ("permissionrequest", {}, "permission"),
            ("permissionrequest", {"tool_name": 5}, "permission"),
            ("permissionrequest", "junk", "permission"),
            ("bogus", {}, None),
        ]
        for hook, payload, want in cases:
            with self.subTest(hook=hook, payload=payload):
                self.assertEqual(announce.event_for(hook, payload), want)


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="an")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def run_main(self, argv, stdin="{}"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = announce.main(argv, io.StringIO(stdin))
        self.assertEqual((rc, out.getvalue(), err.getvalue()), (0, "", ""))

    def args(self, hook="stop", home=None, token=TOK):
        return [hook, "--home", home or self.home, "--session", token]

    def sent(self, argv, stdin="{}", sock_ok=True):
        with mock.patch.object(announce, "_socket_ok", return_value=sock_ok), mock.patch.object(
            announce, "_send", return_value=True
        ) as send:
            self.run_main(argv, stdin)
        return send.call_args_list

    def test_sends_exact_line_per_hook(self) -> None:
        cases = [
            ("stop", "{}", "finished"),
            ("stopfailure", "{}", "error"),
            ("permissionrequest", '{"tool_name":"Read"}', "permission"),
            ("permissionrequest", '{"tool_name":"AskUserQuestion"}', "question"),
            ("permissionrequest", '{"tool_name":"ExitPlanMode"}', "plan"),
        ]
        for hook, stdin, event in cases:
            with self.subTest(hook=hook, event=event):
                calls = self.sent(self.args(hook), stdin)
                want = json.dumps({"type": "announce", "session": TOK, "event": event}) + "\n"
                self.assertEqual(calls, [mock.call(Path(self.home) / "voice.sock", want.encode())])

    def test_payload_text_never_forwarded(self) -> None:
        stdin = json.dumps({"tool_name": "Read", "last_assistant_message": "SECRETWORDS"})
        calls = self.sent(self.args("permissionrequest"), stdin)
        self.assertNotIn(b"SECRETWORDS", calls[0].args[1])

    def test_nothing_sent(self) -> None:
        running = json.dumps({"background_tasks": [{"type": "subagent", "status": "running"}]})
        cases = {
            "bad-token": (self.args(token="XYZ"), "{}", True),
            "relative-home": (self.args(home="rel/home"), "{}", True),
            "no-socket": (self.args(), "{}", False),
            "bg-agents": (self.args(), running, True),
        }
        for name, (argv, stdin, ok) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.sent(argv, stdin, sock_ok=ok), [])

    def test_regular_file_at_socket_path_not_used(self) -> None:
        (Path(self.home) / "voice.sock").write_text("x")
        with mock.patch.object(announce, "_send") as send:
            self.run_main(self.args())
        send.assert_not_called()

    def test_socket_ok_checks_type_and_uid(self) -> None:
        path = Path(self.home) / "voice.sock"

        def fake(mode, uid):
            return types.SimpleNamespace(st_mode=mode, st_uid=uid)

        sock_mode = stat.S_IFSOCK | 0o600
        with mock.patch.object(announce.os, "lstat", return_value=fake(sock_mode, os.getuid())):
            self.assertTrue(announce._socket_ok(path))
        with mock.patch.object(announce.os, "lstat", return_value=fake(sock_mode, os.getuid() + 1)):
            self.assertFalse(announce._socket_ok(path))
        with mock.patch.object(announce.os, "lstat", return_value=fake(stat.S_IFREG | 0o600, os.getuid())):
            self.assertFalse(announce._socket_ok(path))
        self.assertFalse(announce._socket_ok(path))  # missing

    def test_silent_and_zero_on_every_failure(self) -> None:
        for name, argv, stdin in [
            ("no-args", [], "{}"),
            ("unknown-event", self.args("bogus"), "{}"),
            ("missing-session", ["stop", "--home", self.home], "{}"),
            ("help", ["-h"], "{}"),
            ("garbage", self.args(), "}{not json"),
            ("nested", self.args("permissionrequest"), "[" * 5000),
        ]:
            with self.subTest(name), mock.patch.object(announce, "_socket_ok", return_value=True), mock.patch.object(
                announce, "_send", return_value=True
            ):
                self.run_main(argv, stdin)

    def test_stdin_drained_on_every_path(self) -> None:
        for name, argv in [
            ("bad-token", self.args(token="XYZ")),
            ("relative-home", self.args(home="rel/home")),
            ("bad-argv", ["bogus"]),
        ]:
            with self.subTest(name):
                stdin = io.StringIO('{"tool_name": "Read"}')
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(announce.main(argv, stdin), 0)
                self.assertEqual(stdin.read(), "")

    def test_unreadable_stdin_is_silent(self) -> None:
        class Broken:
            def read(self, n):
                raise OSError("closed")

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(announce, "_socket_ok", return_value=True), mock.patch.object(
            announce, "_send", return_value=True
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(announce.main(self.args(), Broken()), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_broken_voice_guard_import_is_silent(self) -> None:
        with mock.patch.dict(sys.modules, {"undertone.voice_guard": None}):
            self.run_main(self.args())
        self.assertIsNone(announce.event_for("bogus", {}))

    def test_send_raising_is_swallowed(self) -> None:
        with mock.patch.object(announce, "_socket_ok", return_value=True), mock.patch.object(
            announce, "_send", side_effect=RuntimeError("boom")
        ):
            self.run_main(self.args())

    def test_send_failure_logged_as_error(self) -> None:
        with mock.patch.object(announce, "_socket_ok", return_value=True), mock.patch.object(
            announce, "_send", return_value=False
        ):
            self.run_main(self.args())
        self.assertIn("announce error stop send-failed", self._log())

    def test_log_has_decisions_but_never_token(self) -> None:
        self.sent(self.args())
        self.sent(self.args(), sock_ok=False)
        self.sent(self.args(token="XYZ"))
        log = self._log()
        self.assertIn("announce sent stop finished", log)
        self.assertIn("announce skipped stop no-socket", log)
        self.assertIn("announce skipped stop bad-token", log)
        self.assertNotIn(TOK, log)
        self.assertNotIn("XYZ", log)

    def test_relative_home_logs_nothing(self) -> None:
        cwd = os.getcwd()
        os.chdir(self.home)
        self.addCleanup(os.chdir, cwd)
        self.run_main(["stop", "--home", "rel", "--session", TOK])
        self.assertEqual(os.listdir(self.home), [])

    def _log(self) -> str:
        return (Path(self.home) / "logs" / "guard.log").read_text()


class EndToEndTests(unittest.TestCase):
    def test_hook_commands_silent_without_socket(self) -> None:
        home = tempfile.mkdtemp(prefix="an")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        hooks = cli.voice_guard_settings(sys.executable, Path(home), TOK)["hooks"]
        payloads = {
            "Stop": {"hook_event_name": "Stop", "background_tasks": []},
            "PermissionRequest": {"hook_event_name": "PermissionRequest", "tool_name": "Read"},
        }
        for event, payload in payloads.items():
            with self.subTest(event):
                cmd = hooks[event][0]["hooks"][-1]["command"]
                res = subprocess.run(
                    ["/bin/sh", "-c", cmd], input=json.dumps(payload), capture_output=True, text=True, timeout=30
                )
                self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))


class BrokenInstallTests(unittest.TestCase):
    def test_import_survives_missing_voice_guard(self) -> None:
        code = (
            "import sys, io\n"
            "sys.modules['undertone.voice_guard'] = None\n"
            "from undertone import announce\n"
            f"sys.exit(announce.main(['stop', '--home', '/', '--session', '{TOK}'], io.StringIO('{{}}')))\n"
        )
        res = subprocess.run([sys.executable, "-P", "-c", code], capture_output=True, text=True, timeout=30,
                             env={**os.environ, "PYTHONPATH": str(Path(announce.__file__).parent.parent)})
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))


class SocketTests(unittest.TestCase):
    """Needs unix-socket bind; cannot run in the Claude sandbox."""

    def test_line_delivered_then_closed(self) -> None:
        home = tempfile.mkdtemp(prefix="an")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        path = os.path.join(home, "voice.sock")
        if len(path) >= 100:
            self.skipTest("socket path too long")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(srv.close)
        srv.bind(path)
        srv.listen(1)
        srv.settimeout(5)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = announce.main(["stop", "--home", home, "--session", TOK], io.StringIO("{}"))
        self.assertEqual((rc, out.getvalue(), err.getvalue()), (0, "", ""))
        conn, _ = srv.accept()
        self.addCleanup(conn.close)
        conn.settimeout(5)
        data = b""
        while chunk := conn.recv(4096):
            data += chunk  # b"" means the sender closed
        want = json.dumps({"type": "announce", "session": TOK, "event": "finished"}) + "\n"
        self.assertEqual(data, want.encode())


if __name__ == "__main__":
    unittest.main()

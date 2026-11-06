"""Tests for the control-socket client (eridian.control) and the voice / mute commands.

Fake unix-socket servers in a short temp ERIDIAN_HOME; one end-to-end test against a real
VoiceBridge. Never the real ~/.eridian.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from eridian import cli, control
from tests import test_voice as tv


class _FakeServer:
    """Serves control.sock: answers each request line via `handler`, or misbehaves."""

    def __init__(self, path: Path, handler=None, mode: str = "normal") -> None:
        self.requests: list[dict] = []
        self.handler = handler or (lambda req: {"ok": True})
        self.mode = mode
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(8)
        self._stop = threading.Event()
        self._conns: list[socket.socket] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self._conns.append(conn)
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        buf = b""
        try:
            while b"\n" not in buf:
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
            req = json.loads(buf.split(b"\n", 1)[0])
            self.requests.append(req)
            if self.mode == "silent":
                self._stop.wait(5)
            elif self.mode == "big":
                conn.sendall(b"x" * 200_000)
            elif self.mode == "drip":
                for _ in range(100):
                    if self._stop.is_set():
                        return
                    conn.sendall(b"x")
                    self._stop.wait(0.3)
            elif self.mode == "deep":
                conn.sendall(b"[" * 30_000 + b"\n")
            elif self.mode == "garbage":
                conn.sendall(b"not json\n")
            elif self.mode == "list":
                conn.sendall(b"[1,2]\n")
            else:
                conn.sendall(json.dumps(self.handler(req)).encode() + b"\n")
        except OSError:
            pass

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self.sock.close()
        for c in self._conns:
            with contextlib.suppress(OSError):
                c.close()


class ControlCliBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="ut", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        patch = mock.patch.dict(os.environ, {"ERIDIAN_HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(mock.patch.stopall)
        self.sock_path = self.home / "control.sock"
        self.slept: list[float] = []
        mock.patch.object(cli, "_sleep", self.slept.append).start()

    def serve(self, handler=None, mode: str = "normal") -> _FakeServer:
        server = _FakeServer(self.sock_path, handler, mode)
        self.addCleanup(server.close)
        return server

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()


def _status(sessions=None, muted=False, saved=True, state_error=False) -> dict:
    return {
        "ok": True,
        "muted": muted,
        "state_error": state_error,
        "saved": saved,
        "sessions": sessions if sessions is not None else [],
    }


def _sess(sid="1a2b3c4d5e6f", pid=4321, cwd="/work/proj", on=True, target=False) -> dict:
    return {"id": sid, "pid": pid, "cwd": cwd, "on": on, "target": target}


class RequestTest(ControlCliBase):
    def test_happy_path(self) -> None:
        server = self.serve(lambda req: {"ok": True, "echo": req["op"]})
        self.assertEqual(control.request({"op": "mute"}), {"ok": True, "echo": "mute"})
        self.assertEqual(server.requests, [{"op": "mute"}])

    def test_explicit_path(self) -> None:
        self.serve()
        self.assertTrue(control.request({"op": "x"}, path=self.sock_path)["ok"])

    def test_not_running_without_socket(self) -> None:
        with self.assertRaisesRegex(control.ControlError, "not running"):
            control.request({"op": "status"})
        self.assertFalse(self.sock_path.exists())

    def test_does_not_create_the_home(self) -> None:
        missing = self.home / "nope"
        with mock.patch.dict(os.environ, {"ERIDIAN_HOME": str(missing)}):
            with self.assertRaises(control.ControlError):
                control.request({"op": "status"})
        self.assertFalse(missing.exists())

    def test_stale_socket_refused_connection(self) -> None:
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(self.sock_path))
        s.close()  # file stays, nobody listens
        with self.assertRaisesRegex(control.ControlError, "not running"):
            control.request({"op": "status"})

    def test_symlink_refused(self) -> None:
        real = self.home / "real.sock"
        _FakeServer(real)
        self.sock_path.symlink_to(real)
        with self.assertRaisesRegex(control.ControlError, "not a socket"):
            control.request({"op": "status"})

    def test_non_socket_refused(self) -> None:
        self.sock_path.write_text("hi")
        with self.assertRaisesRegex(control.ControlError, "not a socket"):
            control.request({"op": "status"})

    def test_wrong_owner_refused(self) -> None:
        self.serve()
        with mock.patch("eridian.control.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(control.ControlError, "another user"):
                control.request({"op": "status"})

    def test_timeout(self) -> None:
        self.serve(mode="silent")
        with self.assertRaisesRegex(control.ControlError, "did not answer"):
            control.request({"op": "status"}, timeout=0.2)

    def test_total_deadline_beats_a_dripping_server(self) -> None:
        self.serve(mode="drip")
        start = time.monotonic()
        with self.assertRaisesRegex(control.ControlError, "did not answer"):
            control.request({"op": "status"}, timeout=1.0)
        self.assertLess(time.monotonic() - start, 2.0)

    def test_peer_uid_mismatch_refused(self) -> None:
        self.serve()
        with mock.patch.object(control, "_peer_uid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(control.ControlError, "another user"):
                control.request({"op": "status"})

    def test_peer_uid_is_the_real_uid_on_this_platform(self) -> None:
        self.serve()
        s = socket.socket(socket.AF_UNIX)
        self.addCleanup(s.close)
        s.connect(str(self.sock_path))
        self.assertEqual(control._peer_uid(s), os.getuid())

    def test_peer_uid_unreadable_fails_closed(self) -> None:
        self.serve()
        with mock.patch.object(control.sys, "platform", "plan9"):
            with self.assertRaisesRegex(control.ControlError, "could not verify"):
                control.request({"op": "status"})

    def test_peer_uid_unknown_xucred_version_refused(self) -> None:
        s = mock.Mock()
        s.getsockopt.return_value = struct.pack("II", 1, os.getuid()) + b"\0" * 68
        with mock.patch.object(control.sys, "platform", "darwin"):
            with self.assertRaisesRegex(control.ControlError, "could not verify"):
                control._peer_uid(s)

    def test_peer_uid_getsockopt_failure_fails_closed(self) -> None:
        s = mock.Mock()
        s.getsockopt.side_effect = OSError
        with self.assertRaises(control.ControlError):
            control._peer_uid(s)

    def test_deeply_nested_response_is_malformed(self) -> None:
        self.serve(mode="deep")
        with self.assertRaisesRegex(control.ControlError, "malformed"):
            control.request({"op": "status"})

    def test_oversized_response(self) -> None:
        self.serve(mode="big")
        with self.assertRaisesRegex(control.ControlError, "too large"):
            control.request({"op": "status"})

    def test_malformed_json(self) -> None:
        self.serve(mode="garbage")
        with self.assertRaisesRegex(control.ControlError, "malformed"):
            control.request({"op": "status"})

    def test_non_object_response(self) -> None:
        self.serve(mode="list")
        with self.assertRaisesRegex(control.ControlError, "malformed"):
            control.request({"op": "status"})


class VoiceStatusTest(ControlCliBase):
    def test_lists_sessions(self) -> None:
        self.serve(lambda r: _status([_sess(target=True), _sess("pid-7", 7, "/b", on=False)], muted=True))
        rc, out, _ = self.run_cli("voice", "status")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "muted: yes")
        self.assertEqual(lines[1], "* on  1a2b3c4d5e6f pid 4321 /work/proj")
        self.assertEqual(lines[2], "  off pid-7 pid 7 /b")
        self.assertEqual(len(lines), 3)

    def test_non_boolean_muted_shows_as_muted(self) -> None:
        for bad in (0, None, "no"):
            with self.subTest(muted=bad):
                server = self.serve(lambda r, bad=bad: _status(muted=bad))
                _, out, _ = self.run_cli("voice", "status")
                self.assertTrue(out.startswith("muted: yes\n"))
                server.close()
                self.sock_path.unlink()

    def test_no_sessions(self) -> None:
        self.serve(lambda r: _status())
        rc, out, _ = self.run_cli("voice", "status")
        self.assertEqual((rc, out), (0, "muted: no\nNo Claude sessions are attached.\n"))

    def test_not_saved_warning(self) -> None:
        self.serve(lambda r: _status([_sess()], saved=False))
        _, out, _ = self.run_cli("voice", "status")
        self.assertIn("Voice settings aren't saved; an off session or mute may turn back on if attach restarts.", out)

    def test_non_true_saved_shows_the_warning(self) -> None:
        def no_saved(r):
            reply = _status([_sess()])
            del reply["saved"]
            return reply

        cases = {"None": lambda r: _status(saved=None), "0": lambda r: _status(saved=0),
                 "str": lambda r: _status(saved="no"), "missing": no_saved}
        for name, handler in cases.items():
            with self.subTest(saved=name):
                server = self.serve(handler)
                _, out, _ = self.run_cli("voice", "status")
                self.assertIn("Voice settings aren't saved", out)
                server.close()
                self.sock_path.unlink()

    def test_non_false_state_error_shows_the_note(self) -> None:
        for bad in (None, 1, "no"):
            with self.subTest(state_error=bad):
                server = self.serve(lambda r, bad=bad: _status(state_error=bad))
                _, out, _ = self.run_cli("voice", "status")
                self.assertIn("Saved voice settings were unreadable", out)
                server.close()
                self.sock_path.unlink()

    def test_saved_prints_no_warning(self) -> None:
        self.serve(lambda r: _status([_sess()]))
        _, out, _ = self.run_cli("voice", "status")
        self.assertNotIn("saved", out)

    def test_state_error_line(self) -> None:
        self.serve(lambda r: _status(state_error=True))
        _, out, _ = self.run_cli("voice", "status")
        self.assertIn(
            "Saved voice settings were unreadable, so every session is off and the mic is "
            "muted until you turn one on.",
            out,
        )

    def test_on_and_target_must_be_real_booleans(self) -> None:
        self.serve(lambda r: _status([_sess(on="yes", target=1)]))
        _, out, _ = self.run_cli("voice", "status")
        self.assertIn("\n  off 1a2b3c4d5e6f", out)

    def test_cwd_is_sanitized_and_truncated(self) -> None:
        evil = "/x\x1b]0;pwned\x07\x1b[31mred" + "y" * 300
        self.serve(lambda r: _status([_sess(cwd=evil)]))
        _, out, _ = self.run_cli("voice", "status")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertIn("/x]0;pwned[31mred", out)  # what is left is inert text
        self.assertLess(max(len(line) for line in out.splitlines()), 140)

    def test_not_running(self) -> None:
        rc, out, err = self.run_cli("voice", "status")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, "eridian: " + control.NOT_RUNNING + "\n")

    def test_bad_status_reply(self) -> None:
        self.serve(lambda r: {"ok": True})
        rc, _, err = self.run_cli("voice", "status")
        self.assertEqual(rc, 1)
        self.assertIn("unexpected", err)


class VoiceSetTest(ControlCliBase):
    def handler(self, saved=True, sessions=None):
        sessions = sessions if sessions is not None else [_sess(), _sess("ffffffffffff", 99, "/o")]

        def h(req):
            if req["op"] == "status":
                return _status(sessions, saved=saved)
            if req["session"] in ("1a2b3c4d5e6f", "4321"):
                return {"ok": True}
            return {"ok": False, "error": "no such session"}

        return h

    def test_off_by_id(self) -> None:
        server = self.serve(self.handler())
        rc, out, err = self.run_cli("voice", "off", "--session", "1a2b3c4d5e6f")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "Voice off for session 1a2b3c4d5e6f.\n")
        self.assertEqual(server.requests[0], {"op": "voice_off", "session": "1a2b3c4d5e6f"})

    def test_on_by_pid(self) -> None:
        server = self.serve(self.handler())
        rc, out, _ = self.run_cli("voice", "on", "--session", "4321")
        self.assertEqual(rc, 0)
        self.assertEqual(server.requests, [{"op": "voice_on", "session": "4321"}])
        self.assertIn("Voice on", out)

    def test_off_warns_when_not_saved(self) -> None:
        self.serve(self.handler(saved=False))
        rc, _, err = self.run_cli("voice", "off", "--session", "4321")
        self.assertEqual(rc, 0)
        self.assertIn("Voice settings aren't saved", err)

    def test_off_no_warning_when_saved(self) -> None:
        self.serve(self.handler())
        _, _, err = self.run_cli("voice", "off", "--session", "4321")
        self.assertEqual(err, "")

    def test_unknown_session_exits_2(self) -> None:
        self.serve(self.handler())
        rc, _, err = self.run_cli("voice", "off", "--session", "000000000000")
        self.assertEqual(rc, 2)
        self.assertEqual(err, "eridian: No such session\n")

    def test_trailing_newline_in_id_is_rejected(self) -> None:
        server = self.serve(self.handler())
        rc, _, _ = self.run_cli("voice", "off", "--session", "4321\n")
        self.assertEqual((rc, server.requests), (2, []))

    def test_bad_id_exits_2_without_connecting(self) -> None:
        server = self.serve(self.handler())
        rc, _, err = self.run_cli("voice", "off", "--session", "../etc; rm")
        self.assertEqual(rc, 2)
        self.assertIn("Bad session id", err)
        self.assertEqual(server.requests, [])

    def test_server_error_exits_1(self) -> None:
        self.serve(lambda r: {"ok": False, "error": "can't turn on a session without a token"})
        rc, _, err = self.run_cli("voice", "on", "--session", "4321")
        self.assertEqual(rc, 1)
        self.assertIn("without a token", err)

    def test_this_session_resolved_by_ancestry(self) -> None:
        server = self.serve(self.handler())
        parents = {4321: 500, 99: 600}  # channel pid -> its parent (claude)
        with mock.patch.object(cli, "_ppid_of", side_effect=lambda p, d=None: parents.get(p, 1)), \
                mock.patch.object(cli.os, "getppid", return_value=77), \
                mock.patch.object(cli, "_own_ancestors", return_value=[77, 500, 1]):
            rc, out, _ = self.run_cli("voice", "off")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "Voice off for session 1a2b3c4d5e6f (pid 4321).\n")
        self.assertEqual(server.requests[1], {"op": "voice_off", "session": "1a2b3c4d5e6f"})

    def test_this_session_no_match(self) -> None:
        server = self.serve(self.handler())
        with mock.patch.object(cli, "_ppid_of", return_value=1), \
                mock.patch.object(cli, "_own_ancestors", return_value=[77, 88]):
            rc, _, err = self.run_cli("voice", "off")
        self.assertEqual(rc, 2)
        self.assertIn("Couldn't tell which session this is; pass --session", err)
        self.assertEqual([r["op"] for r in server.requests], ["status"])

    def test_several_sessions_under_one_parent_is_ambiguous(self) -> None:
        sessions = [_sess(), _sess("ffffffffffff", 99, "/o")]
        server = self.serve(self.handler(sessions=sessions))
        with mock.patch.object(cli, "_ppid_of", return_value=500), \
                mock.patch.object(cli, "_own_ancestors", return_value=[500]):
            rc, _, err = self.run_cli("voice", "off")
        self.assertEqual(rc, 2)
        self.assertIn("Several sessions match; pass --session", err)
        self.assertEqual([r["op"] for r in server.requests], ["status"])

    def test_off_warns_when_saved_is_not_a_real_true(self) -> None:
        self.serve(self.handler(saved="yes"))
        _, _, err = self.run_cli("voice", "off", "--session", "4321")
        self.assertIn("Voice settings aren't saved", err)

    def test_this_session_gives_up_if_a_parent_lookup_fails_midway(self) -> None:
        sessions = [_sess(), _sess("ffffffffffff", 99, "/o")]
        answers = {4321: 500, 99: None}  # budget runs out before the second lookup
        with mock.patch.object(cli, "_ppid_of", side_effect=lambda p, d=None: answers[p]), \
                mock.patch.object(cli, "_own_ancestors", return_value=[500]):
            self.assertEqual(cli._this_session(sessions), [])

    def test_exhausted_budget_means_pass_session(self) -> None:
        self.serve(self.handler())
        with mock.patch.object(cli, "_ppid_of", return_value=None):
            rc, _, err = self.run_cli("voice", "off")
        self.assertEqual(rc, 2)
        self.assertIn("pass --session", err)

    def test_nearest_ancestor_wins_over_a_farther_one(self) -> None:
        parents = {4321: 500, 99: 600}
        sessions = [_sess(), _sess("ffffffffffff", 99, "/o")]
        with mock.patch.object(cli, "_ppid_of", side_effect=lambda p, d=None: parents[p]), \
                mock.patch.object(cli, "_own_ancestors", return_value=[600, 500]):
            found = cli._this_session(sessions)
        self.assertEqual([s["pid"] for s in found], [99])

    def test_this_session_skips_non_dict_entries(self) -> None:
        with mock.patch.object(cli, "_ppid_of", return_value=500), \
                mock.patch.object(cli, "_own_ancestors", return_value=[500]):
            found = cli._this_session(["junk", 7, None, _sess()])
        self.assertEqual([s["pid"] for s in found], [4321])

    def test_own_ancestors_stops_when_ps_fails(self) -> None:
        with mock.patch.object(cli.os, "getppid", return_value=10), \
                mock.patch.object(cli, "_ppid_of", return_value=None):
            self.assertEqual(cli._own_ancestors(time.monotonic() + 5), [10])

    def test_own_ancestors_caps_at_64_levels(self) -> None:
        calls = []

        def next_pid(p, d=None):
            calls.append(p)
            if len(calls) > 100:
                raise AssertionError("walk did not stop at the cap")
            return p + 1

        with mock.patch.object(cli.os, "getppid", return_value=1000), \
                mock.patch.object(cli, "_ppid_of", side_effect=next_pid):
            self.assertEqual(len(cli._own_ancestors(time.monotonic() + 5)), 64)

    def test_own_ancestors_cycle_guard(self) -> None:
        chain = {10: 20, 20: 10}
        with mock.patch.object(cli.os, "getppid", return_value=10), \
                mock.patch.object(cli, "_ppid_of", side_effect=lambda p, d=None: chain[p]):
            self.assertEqual(cli._own_ancestors(time.monotonic() + 5), [10, 20])

    def test_ppid_of_uses_absolute_ps_and_remaining_budget(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout=" 42\n")
        with mock.patch.object(cli.subprocess, "run", return_value=done) as run:
            self.assertEqual(cli._ppid_of(7, time.monotonic() + 2), 42)
        self.assertEqual(run.call_args.args[0][0], "/bin/ps")
        self.assertLessEqual(run.call_args.kwargs["timeout"], 2)

    def test_ppid_of_after_deadline_does_not_spawn(self) -> None:
        with mock.patch.object(cli.subprocess, "run") as run:
            self.assertIsNone(cli._ppid_of(7, time.monotonic() - 1))
        run.assert_not_called()

    def test_ppid_of_ps_failure_is_none(self) -> None:
        with mock.patch.object(cli.subprocess, "run", side_effect=OSError):
            self.assertIsNone(cli._ppid_of(7, time.monotonic() + 2))

    def test_own_ancestors_walks_ps(self) -> None:
        chain = {10: 20, 20: 30, 30: 1}
        with mock.patch.object(cli.os, "getppid", return_value=10), \
                mock.patch.object(cli, "_ppid_of", side_effect=lambda p, d=None: chain[p]):
            self.assertEqual(cli._own_ancestors(time.monotonic() + 5), [10, 20, 30])


class MuteTest(ControlCliBase):
    def test_mute_ok(self) -> None:
        server = self.serve(lambda r: _status() if r["op"] == "status" else {"ok": True})
        rc, out, err = self.run_cli("mute")
        self.assertEqual((rc, out, err), (0, "Muted.\n", ""))
        self.assertEqual(server.requests[0], {"op": "mute"})

    def test_mute_warns_when_not_saved(self) -> None:
        self.serve(lambda r: _status(saved=False) if r["op"] == "status" else {"ok": True})
        rc, _, err = self.run_cli("mute")
        self.assertEqual(rc, 0)
        self.assertIn("Voice settings aren't saved", err)

    def test_mute_server_error(self) -> None:
        self.serve(lambda r: {"ok": False, "error": "could not save"})
        rc, _, err = self.run_cli("mute")
        self.assertEqual((rc, err), (1, "eridian: Could not save\n"))

    def test_unmute_success(self) -> None:
        self.serve(lambda r: _status(muted=False) if r["op"] == "status" else {"ok": True})
        rc, out, _ = self.run_cli("unmute")
        self.assertEqual((rc, out), (0, "Unmuted.\n"))
        self.assertTrue(self.slept)

    def test_unmute_fails_safe_unless_muted_is_exactly_false(self) -> None:
        for bad in (1, "no", None, 0):
            with self.subTest(muted=bad):
                server = self.serve(
                    lambda r, bad=bad: _status(muted=bad) if r["op"] == "status" else {"ok": True}
                )
                rc, out, err = self.run_cli("unmute")
                self.assertEqual((rc, out), (1, ""))
                self.assertIn("Muted again", err)
                server.close()
                self.sock_path.unlink()

    def test_unmute_missing_muted_key_fails_safe(self) -> None:
        def h(r):
            if r["op"] != "status":
                return {"ok": True}
            reply = _status()
            del reply["muted"]
            return reply

        self.serve(h)
        rc, _, err = self.run_cli("unmute")
        self.assertEqual(rc, 1)
        self.assertIn("Muted again", err)

    def test_unmute_reports_remute(self) -> None:
        self.serve(lambda r: _status(muted=True) if r["op"] == "status" else {"ok": True})
        rc, out, err = self.run_cli("unmute")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("Muted again: the mic may have failed to reopen, or something muted it", err)

    def test_unmute_catches_late_remute(self) -> None:
        polls = []

        def h(req):
            if req["op"] != "status":
                return {"ok": True}
            polls.append(1)
            return _status(muted=len(polls) >= 3)

        self.serve(h)
        rc, _, err = self.run_cli("unmute")
        self.assertEqual(rc, 1)
        self.assertIn("Muted again", err)

    def test_not_running(self) -> None:
        for cmd in ("mute", "unmute"):
            rc, _, err = self.run_cli(cmd)
            self.assertEqual((rc, err), (1, "eridian: " + control.NOT_RUNNING + "\n"))


class EndToEndTest(tv.VoiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.dict(os.environ, {"ERIDIAN_HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_real_bridge(self) -> None:
        self.start()
        self.attach(pid=4242, cwd="/work/proj")
        with mock.patch.object(cli, "_sleep", lambda s: None):
            rc, out, _ = self.run_cli("voice", "status")
            self.assertEqual(rc, 0)
            self.assertIn("muted: no", out)
            self.assertIn("pid 4242 /work/proj", out)
            self.assertIn(" on ", out)

            rc, out, _ = self.run_cli("voice", "off", "--session", "4242")
            self.assertEqual(rc, 0, out)
            self.assertIn("Voice off", out)
            self.assertIn(" off ", self.run_cli("voice", "status")[1])

            rc, out, _ = self.run_cli("mute")
            self.assertEqual((rc, out), (0, "Muted.\n"))
            self.assertIn("muted: yes", self.run_cli("voice", "status")[1])

            self.bridge.process_frame(tv.FRAME)
            rc, out, err = self.run_cli("unmute")
            self.assertEqual((rc, out, err), (0, "Unmuted.\n", ""))
            self.assertIn("muted: no", self.run_cli("voice", "status")[1])


if __name__ == "__main__":
    unittest.main()

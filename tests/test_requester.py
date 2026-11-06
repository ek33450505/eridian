"""Layer-2 requester check: attach refuses voice_on / unmute from a Claude descendant.

Real unix sockets in a short temp ERIDIAN_HOME; the ps walk is exercised through an
injected runner, plus one live check against the real /bin/ps.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import unittest
from unittest import mock

from tests import test_control as tc
from tests import test_voice as tv
from eridian import voice
from eridian.voice import REQUESTER_MAX_STEPS, VoiceBridge, requester_refusal


def chain(*rows):
    """A fake ps runner over {pid: (ppid, comm)} built from (pid, ppid, comm) rows."""
    table = {row[0]: (row[1], row[2], row[3] if len(row) > 3 else row[2]) for row in rows}
    calls = []

    def run(pid, timeout):
        calls.append(pid)
        return table.get(pid)

    run.calls = calls
    return run


class RequesterRefusalTest(unittest.TestCase):
    def test_claude_symlink_in_chain_is_refused(self) -> None:
        run = chain((50, 40, "/bin/zsh"), (40, 30, "/opt/homebrew/bin/claude"), (30, 1, "/usr/bin/login"))
        self.assertEqual(requester_refusal(50, ps_runner=run), "claude")

    def test_claude_code_package_path_is_refused(self) -> None:
        comm = "/x/lib/node_modules/@anthropic-ai/claude-code/bin/wrapper"
        run = chain((50, 40, "/bin/zsh"), (40, 1, comm))
        self.assertEqual(requester_refusal(50, ps_runner=run), "claude")

    def test_case_is_folded(self) -> None:
        run = chain((50, 40, "/bin/zsh"), (40, 1, "/Some/Dir/CLAUDE.EXE"))
        self.assertEqual(requester_refusal(50, ps_runner=run), "claude")

    def test_the_peer_itself_counts(self) -> None:
        self.assertEqual(requester_refusal(50, ps_runner=chain((50, 1, "claude"))), "claude")

    def test_node_comm_with_claude_code_args_is_refused(self) -> None:
        args = "node /x/lib/node_modules/@anthropic-ai/claude-code/cli.js --flag"
        run = chain((50, 40, "/bin/zsh"), (40, 1, "node", args))
        self.assertEqual(requester_refusal(50, ps_runner=run), "claude")

    def test_native_installer_versions_path_is_refused(self) -> None:
        run = chain((50, 40, "/bin/zsh"), (40, 1, "/Users/x/.local/share/claude/versions/2.1.0"))
        self.assertEqual(requester_refusal(50, ps_runner=run), "claude")

    def test_args_basename_claude_is_refused(self) -> None:
        run = chain((50, 1, "node", "claude --resume"))
        self.assertEqual(requester_refusal(50, ps_runner=run), "claude")

    def test_plain_node_is_not_refused(self) -> None:
        run = chain((50, 40, "node", "node server.js"), (40, 1, "/bin/zsh"))
        self.assertIsNone(requester_refusal(50, ps_runner=run))

    def test_initial_pid_0_and_1_are_refused(self) -> None:
        for pid in (0, 1, -5):
            self.assertEqual(requester_refusal(pid, ps_runner=chain()), "unverified", pid)

    def test_clean_chain_to_pid_1_is_allowed(self) -> None:
        run = chain((50, 40, "/bin/zsh"), (40, 30, "/usr/bin/login"), (30, 1, "/sbin/launchd-child"))
        self.assertIsNone(requester_refusal(50, ps_runner=run))

    def test_ps_failure_mid_walk_is_refused(self) -> None:
        run = chain((50, 40, "/bin/zsh"))  # pid 40 unknown -> None
        self.assertEqual(requester_refusal(50, ps_runner=run), "unverified")

    def test_deadline_exhausted_is_refused(self) -> None:
        run = chain((50, 1, "/bin/zsh"))
        self.assertEqual(requester_refusal(50, deadline_s=0.0, ps_runner=run), "unverified")
        self.assertEqual(run.calls, [])

    def test_loop_hits_the_step_bound_and_is_refused(self) -> None:
        calls = []

        def loop(pid, timeout):
            calls.append(pid)
            if len(calls) > REQUESTER_MAX_STEPS + 5:
                raise AssertionError("walk is unbounded")
            return (pid, "/bin/zsh", "/bin/zsh")  # its own parent: never reaches pid 1

        self.assertEqual(requester_refusal(50, ps_runner=loop), "unverified")
        self.assertEqual(len(calls), REQUESTER_MAX_STEPS)

    def test_pid_none_is_refused(self) -> None:
        self.assertEqual(requester_refusal(None, ps_runner=chain()), "unverified")


def _descends_from_claude() -> bool:
    """Independent detection: raw ps walk, not requester_refusal."""
    pid = os.getpid()
    for _ in range(64):
        if pid <= 1:
            return False
        out = subprocess.run(["/bin/ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True).stdout
        parts = out.split(None, 1)
        if len(parts) != 2:
            return False
        low = parts[1].strip().casefold()
        if low.rsplit("/", 1)[-1] in ("claude", "claude.exe") or "@anthropic-ai/claude-code" in low:
            return True
        pid = int(parts[0])
    return False


class RealAncestryTest(unittest.TestCase):
    def test_real_ps_sees_claude_ancestor(self) -> None:
        if not _descends_from_claude():
            self.skipTest("this process does not descend from Claude Code")
        self.assertEqual(requester_refusal(os.getpid()), "claude")


class ControlRequesterTest(tc.ControlBase):
    verdict_value: str | None = "claude"

    def setUp(self) -> None:
        self.checked: list[int | None] = []
        super().setUp()

    def make_bridge(self, **overrides) -> VoiceBridge:
        overrides.setdefault("requester_check", self.verdict)
        return super().make_bridge(**overrides)

    def verdict(self, pid):
        self.checked.append(pid)
        return self.verdict_value

    def test_on_ops_are_refused_without_state_change(self) -> None:
        self.start()
        self.attach(pid=11)
        self.assertEqual(self.ask({"op": "voice_off", "session": "11"}), {"ok": True})
        self.assertEqual(self.ask({"op": "mute"}), {"ok": True})
        self.checked.clear()
        want = {"ok": False, "error": "refused: requested from a Claude session"}
        self.assertEqual(self.ask({"op": "voice_on", "session": "11"}), want)
        self.assertEqual(self.ask({"op": "unmute"}), want)
        self.assertEqual(self.checked, [os.getpid()] * 2)
        self.assertTrue(self.bridge.is_muted())
        sessions = self.ask({"op": "status"})["sessions"]
        self.assertEqual([s["on"] for s in sessions], [False])
        self.assertIn(f"[control] refused unmute from a Claude session (pid {os.getpid()})", self.out.getvalue())

    def test_unverified_reply_and_log(self) -> None:
        self.verdict_value = "unverified"
        self.start()
        self.ask({"op": "mute"})
        self.assertEqual(
            self.ask({"op": "unmute"}), {"ok": False, "error": "refused: could not verify the requester"}
        )
        self.assertTrue(self.bridge.is_muted())
        self.assertIn("[control] refused unmute (requester unverified)", self.out.getvalue())

    def test_off_direction_ops_never_reach_the_check(self) -> None:
        self.start()
        self.attach(pid=11)
        for req in ({"op": "status"}, {"op": "mute"}, {"op": "voice_off", "session": "11"}):
            self.assertTrue(self.ask(req)["ok"])
        self.assertEqual(self.checked, [])

    def test_peer_pid_is_read_from_the_socket(self) -> None:
        self.start()
        ch = self.ctl()
        ch.send_obj({"op": "status"})
        ch.read_line()
        with self.bridge._lock:
            pids = [c.peer_pid for c in self.bridge._control_conns]
        self.assertEqual(pids, [os.getpid()])

    def test_peer_pid_change_between_accept_and_check_is_unverified(self) -> None:
        self.start()
        self.ask({"op": "mute"})
        ch = self.ctl()
        ch.send_obj({"op": "status"})
        ch.read_line()
        with self.bridge._lock:
            for c in self.bridge._control_conns:
                c.peer_pid = os.getpid() + 1  # as if the accept-time pid differed
        ch.send_obj({"op": "unmute"})
        self.assertEqual(ch.read_line(), {"ok": False, "error": "refused: could not verify the requester"})
        self.assertEqual(self.checked, [])
        self.assertTrue(self.bridge.is_muted())

    def test_unreadable_peer_pid_is_unverified(self) -> None:
        self.start()
        self.ask({"op": "mute"})
        with mock.patch.object(voice, "_peer_pid", return_value=None):
            reply = self.ask({"op": "unmute"})
        self.assertEqual(reply, {"ok": False, "error": "refused: could not verify the requester"})
        self.assertEqual(self.checked, [])

    def test_ninth_connection_can_still_mute_but_not_unmute(self) -> None:
        self.verdict_value = None
        self.start()
        held = [self.ctl() for _ in range(voice.CONTROL_MAX_CONNECTIONS)]
        for ch in held:
            ch.send_obj({"op": "status"})
            self.assertTrue(ch.read_line()["ok"])
        reserve = self.ctl()
        reserve.send_obj({"op": "unmute"})
        self.assertEqual(reserve.read_line(), {"ok": False, "error": "too many connections"})
        reserve2 = self.ctl()
        reserve2.send_obj({"op": "mute"})
        self.assertEqual(reserve2.read_line(), {"ok": True})
        self.assertTrue(self.bridge.is_muted())
        self.assertIsNone(reserve2.read_line())  # closed after one request
        self.assertEqual(self.checked, [])

    def test_default_check_is_the_real_one(self) -> None:
        default = inspect.signature(VoiceBridge.__init__).parameters["requester_check"].default
        self.assertIs(default, voice.requester_refusal)
        b = tv.VoiceTestCase.make_bridge(self)  # no override: the production default
        self.addCleanup(b.close)
        self.assertIs(b._requester_check, requester_refusal)


if __name__ == "__main__":
    unittest.main()

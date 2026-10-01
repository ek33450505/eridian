"""Tests for undertone.brain against a fake `claude` CLI (tests/fake_claude.py).

Never runs the real `claude`; UNDERTONE_HOME is always a temp dir.
"""

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from undertone import brain
from undertone.brain import (
    BrainError, ClaudeBrain, TextDelta, ToolUse, TurnEnd, build_command,
)

FAKE = Path(__file__).with_name("fake_claude.py")
FAKE_SESSION = "11111111-2222-4333-8444-555555555555"


def _reply(events) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDelta))


class PolicyTest(unittest.TestCase):
    def test_safety_flags_present(self) -> None:
        argv = build_command("claude")
        for flag in ("--restricted", "--strict-mcp-config", "--permission-prompts"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob,WebSearch")

    def test_no_dangerous_tools_or_bypass(self) -> None:
        argv = build_command("claude", FAKE_SESSION)
        tools = argv[argv.index("--tools") + 1]
        for banned in ("Bash", "Edit", "Write", "WebFetch", "NotebookEdit"):
            self.assertNotIn(banned, tools)
        # The voice prompt legitimately mentions nothing dangerous; scan flags only.
        flags = [a for a in argv if a.startswith("--")]
        for a in flags:
            self.assertNotIn("dangerously", a)
        self.assertNotIn("bypassPermissions", " ".join(a for a in argv if a != brain.VOICE_PROMPT))

    def test_resume_only_when_given(self) -> None:
        self.assertNotIn("--resume", build_command("claude"))
        argv = build_command("claude", FAKE_SESSION)
        self.assertEqual(argv[argv.index("--resume") + 1], FAKE_SESSION)

    def test_invalid_resume_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_command("claude", "not-a-uuid; rm -rf /")


class BrainTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"UNDERTONE_HOME": os.path.join(self.tmp.name, "home")})
        env.start()
        self.addCleanup(env.stop)
        self.wrapper = os.path.join(self.tmp.name, "claude")
        with open(self.wrapper, "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        os.chmod(self.wrapper, os.stat(self.wrapper).st_mode | stat.S_IEXEC)

    def make(self, **kw) -> ClaudeBrain:
        b = ClaudeBrain(cwd=self.tmp.name, claude_cmd=self.wrapper, **kw)
        self.addCleanup(b.close)
        b.start()
        return b


class BrainTest(BrainTestCase):
    def test_happy_path(self) -> None:
        b = self.make()
        events = list(b.ask("say: hello there world"))
        self.assertEqual(_reply(events), "hello there world ")
        end = events[-1]
        self.assertEqual(end, TurnEnd(session_id=FAKE_SESSION, is_error=False, interrupted=False))
        self.assertEqual(b.session_id, FAKE_SESSION)

    def test_two_turns_one_process(self) -> None:
        b = self.make()
        pid = b._proc.pid
        self.assertEqual(_reply(b.ask("say: one")), "one ")
        self.assertEqual(_reply(b.ask("say: two")), "two ")
        self.assertEqual(b._proc.pid, pid)

    def test_tool_use_surfaces(self) -> None:
        events = list(self.make().ask("tool: go"))
        self.assertIn(ToolUse("Read"), events)
        self.assertEqual(_reply(events), "done reading")
        self.assertIsInstance(events[-1], TurnEnd)

    def test_garbage_ignored(self) -> None:
        events = list(self.make().ask("garbage:"))
        self.assertEqual(_reply(events), "clean")
        self.assertIsInstance(events[-1], TurnEnd)

    def test_interrupt_from_other_thread_then_next_turn_works(self) -> None:
        b = self.make()
        got_delta = threading.Event()
        events = []

        def consume() -> None:
            for ev in b.ask("slow:"):
                events.append(ev)
                got_delta.set()

        t = threading.Thread(target=consume)
        t.start()
        self.assertTrue(got_delta.wait(10))
        b.interrupt()
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertTrue(events[-1].interrupted)
        self.assertTrue(events[-1].is_error)  # the real CLI reports interrupted turns as errors
        # Interrupt cut it short of the 60 ticks.
        self.assertLess(len(events), 59)
        follow = list(b.ask("say: still here"))
        self.assertEqual(_reply(follow), "still here ")
        self.assertFalse(follow[-1].interrupted)

    def test_interrupt_without_turn_is_noop(self) -> None:
        b = self.make()
        b.interrupt()
        self.assertEqual(_reply(b.ask("say: fine")), "fine ")

    def test_interrupt_on_dead_child_does_not_raise(self) -> None:
        b = self.make()
        b.close()
        b.interrupt()

    def test_child_death_raises(self) -> None:
        b = self.make()
        with self.assertRaises(BrainError):
            list(b.ask("die:"))
        deadline = time.time() + 5
        while b.alive and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(b.alive)

    def test_turn_timeout_raises(self) -> None:
        b = self.make(turn_timeout_s=0.5)
        with self.assertRaises(BrainError):
            list(b.ask("hang:"))
        self.assertFalse(b.alive)

    def test_ask_before_start_raises(self) -> None:
        b = ClaudeBrain(cwd=self.tmp.name, claude_cmd=self.wrapper)
        with self.assertRaises(BrainError):
            list(b.ask("say: x"))

    def test_missing_binary_raises_brain_error(self) -> None:
        b = ClaudeBrain(cwd=self.tmp.name, claude_cmd=os.path.join(self.tmp.name, "nope"))
        with self.assertRaises(BrainError):
            b.start()

    def test_safety_flags_reach_child(self) -> None:
        argv_file = os.path.join(self.tmp.name, "argv.json")
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_ARGV_FILE": argv_file}):
            b = self.make(resume_session_id=FAKE_SESSION)
            list(b.ask("say: x"))
        argv = json.loads(Path(argv_file).read_text())
        for flag in ("--restricted", "--strict-mcp-config"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob,WebSearch")
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(argv[argv.index("--resume") + 1], FAKE_SESSION)

    def _ask_collect(self, b: ClaudeBrain, text: str = "say: x"):
        """Iterate manually so events yielded before a raise are visible."""
        events = []
        try:
            for ev in b.ask(text):
                events.append(ev)
        except BrainError as exc:
            return events, exc
        return events, None

    def _policy_case(self, env: dict, message: str = "policy mismatch") -> ClaudeBrain:
        with mock.patch.dict(os.environ, env):
            b = self.make()
            events, exc = self._ask_collect(b)
        self.assertIsNotNone(exc)
        self.assertIn(message, str(exc))
        self.assertEqual(events, [])
        self.assertFalse(b.alive)
        return b

    def test_no_init_raises_before_any_event(self) -> None:
        self._policy_case({"FAKE_CLAUDE_NO_INIT": "1"}, "before a verified system/init")

    def test_delta_before_init_raises_before_any_event(self) -> None:
        self._policy_case({"FAKE_CLAUDE_DELTA_BEFORE_INIT": "1"}, "before a verified system/init")

    def test_policy_mismatch_bash_advertised(self) -> None:
        self._policy_case({"FAKE_CLAUDE_TOOLS": "Read,Grep,Glob,WebSearch,Bash"})

    def test_policy_mismatch_missing_tool(self) -> None:
        self._policy_case({"FAKE_CLAUDE_TOOLS": "Read"})

    def test_policy_mismatch_mcp_servers(self) -> None:
        self._policy_case({"FAKE_CLAUDE_MCP": "1"})

    def test_policy_mismatch_mcp_key_missing(self) -> None:
        self._policy_case({"FAKE_CLAUDE_NO_MCP_KEY": "1"})

    def test_policy_order_independent(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_TOOLS": "WebSearch,Read,Glob,Grep"}):
            b = self.make()
            self.assertEqual(_reply(b.ask("say: ok")), "ok ")

    def test_brain_log_mode_and_rotation(self) -> None:
        log_dir = Path(os.environ["UNDERTONE_HOME"]) / "logs"
        log_dir.mkdir(parents=True)
        log = log_dir / "brain.log"
        log.write_bytes(b"x" * 1_100_000)
        os.chmod(log, 0o644)
        self.make()
        self.assertTrue((log_dir / "brain.log.1").exists())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_brain_log_symlink_refused(self) -> None:
        log_dir = Path(os.environ["UNDERTONE_HOME"]) / "logs"
        log_dir.mkdir(parents=True)
        target = Path(self.tmp.name) / "victim"
        target.write_text("keep")
        (log_dir / "brain.log").symlink_to(target)
        b = ClaudeBrain(cwd=self.tmp.name, claude_cmd=self.wrapper)
        with self.assertRaises(BrainError):
            b.start()
        self.assertEqual(target.read_text(), "keep")

    def test_env_var_selects_command(self) -> None:
        with mock.patch.dict(os.environ, {"UNDERTONE_CLAUDE_CMD": self.wrapper}):
            b = ClaudeBrain(cwd=self.tmp.name)
            self.addCleanup(b.close)
            b.start()
            self.assertEqual(_reply(b.ask("say: env")), "env ")

    def test_close_leaves_no_child(self) -> None:
        b = self.make()
        list(b.ask("say: x"))
        proc = b._proc
        b.close()
        self.assertIsNotNone(proc.poll())
        self.assertFalse(b.alive)


class SessionPersistenceTest(BrainTestCase):
    def test_round_trip_and_mode(self) -> None:
        self.assertIsNone(brain.load_session_id())
        brain.save_session_id(FAKE_SESSION)
        self.assertEqual(brain.load_session_id(), FAKE_SESSION)
        path = Path(os.environ["UNDERTONE_HOME"]) / "chat_session"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_save_rejects_non_uuid(self) -> None:
        with self.assertRaises(ValueError):
            brain.save_session_id("../../etc/passwd")
        self.assertIsNone(brain.load_session_id())

    def test_save_replaces_symlink_not_target(self) -> None:
        home = Path(os.environ["UNDERTONE_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        target = Path(self.tmp.name) / "victim"
        target.write_text("keep")
        link = home / "chat_session"
        link.symlink_to(target)
        brain.save_session_id(FAKE_SESSION)
        self.assertFalse(link.is_symlink())
        self.assertEqual(target.read_text(), "keep")
        self.assertEqual(brain.load_session_id(), FAKE_SESSION)

    def test_no_temp_files_left(self) -> None:
        brain.save_session_id(FAKE_SESSION)
        names = os.listdir(os.environ["UNDERTONE_HOME"])
        self.assertEqual([n for n in names if n.startswith(".chat_session")], [])

    def test_non_uuid_session_id_from_child_ignored(self) -> None:
        b = ClaudeBrain(cwd=self.tmp.name, claude_cmd=self.wrapper)
        b._policy_ok = True  # bypass the init gate; this test targets session_id parsing
        list(b._translate({"type": "result", "session_id": "evil --flag", "is_error": False}))
        self.assertIsNone(b.session_id)

    def test_invalid_content_is_none(self) -> None:
        path = Path(os.environ["UNDERTONE_HOME"]) / "chat_session"
        brain.save_session_id(FAKE_SESSION)
        path.write_text("garbage; --dangerously-skip-permissions")
        self.assertIsNone(brain.load_session_id())

    def test_existing_loose_file_tightened(self) -> None:
        path = Path(os.environ["UNDERTONE_HOME"]) / "chat_session"
        brain.save_session_id(FAKE_SESSION)
        os.chmod(path, 0o644)
        brain.save_session_id(FAKE_SESSION)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()

"""Smoke tests for undertone.cli."""

import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from undertone import cli, paths, voice_guard


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env_patch = mock.patch.dict(
            os.environ, {"UNDERTONE_HOME": self._tmp.name}, clear=False
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._tmp.cleanup()

    def _run(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(list(argv))
        return rc, buf.getvalue()


class RetiredCommandsTest(CliTestCase):
    def test_phase1_commands_are_gone(self) -> None:
        parser = cli.build_parser()
        for name in ("serve", "say", "stop", "on", "off", "status", "hook", "hooks-snippet"):
            with self.subTest(command=name):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                    parser.parse_args([name])
                self.assertEqual(ctx.exception.code, 2)

    def test_help_lists_exactly_the_remaining_commands(self) -> None:
        sub = next(a for a in cli.build_parser()._actions if a.dest == "command")
        self.assertEqual(
            sorted(sub.choices), ["attach", "channel", "chat", "listen", "models", "session"]
        )


class ChatParserTest(unittest.TestCase):
    def test_chat_is_registered_with_defaults(self) -> None:
        args = cli.build_parser().parse_args(["chat"])
        self.assertIs(args.func, cli._cmd_chat)
        self.assertFalse(args.no_beep)
        self.assertFalse(args.resume)
        self.assertEqual(args.follow_up, 6.0)

    def test_chat_flags(self) -> None:
        args = cli.build_parser().parse_args(
            ["chat", "--no-beep", "--resume", "--follow-up", "0"]
        )
        self.assertTrue(args.no_beep)
        self.assertTrue(args.resume)
        self.assertEqual(args.follow_up, 0.0)


class ChatWorkdirTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.undertone_home = self.root / "undertone"
        self.workspace = self.undertone_home / "workspace"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _workdir(self, cwd: Path) -> tuple[Path, bool]:
        return cli.chat_workdir(cwd, self.home, self.undertone_home)

    def _assert_substituted(self, cwd: Path) -> None:
        workdir, substituted = self._workdir(cwd)
        self.assertTrue(substituted, f"{cwd} should be substituted")
        self.assertEqual(workdir, self.workspace.resolve())
        self.assertEqual(workdir.stat().st_mode & 0o777, 0o700)

    def _assert_allowed(self, cwd: Path) -> None:
        workdir, substituted = self._workdir(cwd)
        self.assertFalse(substituted, f"{cwd} should be allowed")
        self.assertEqual(workdir, cwd.resolve())
        self.assertFalse(self.undertone_home.exists())

    def test_a_home_itself(self) -> None:
        self._assert_substituted(self.home)

    def test_b_ancestors_of_home_and_root(self) -> None:
        self._assert_substituted(self.root)
        self._assert_substituted(self.home / "..")
        self._assert_substituted(Path("/"))

    def test_c_dot_directories_under_home(self) -> None:
        for name in (".ssh", ".claude", ".config"):
            d = self.home / name / "sub"
            d.mkdir(parents=True)
            self._assert_substituted(self.home / name)
            self._assert_substituted(d)

    def test_d_library_and_inside(self) -> None:
        d = self.home / "Library" / "Mail"
        d.mkdir(parents=True)
        self._assert_substituted(self.home / "Library")
        self._assert_substituted(d)

    def test_e_top_level_personal_folders_only(self) -> None:
        for name in ("Documents", "Desktop", "Downloads"):
            sub = self.home / name / "proj"
            sub.mkdir(parents=True)
            self._assert_allowed(sub)  # first: no workspace created yet
            self._assert_substituted(self.home / name)
            shutil.rmtree(self.undertone_home)

    def test_symlink_to_home_is_substituted(self) -> None:
        link = self.root / "homelink"
        link.symlink_to(self.home)
        self._assert_substituted(link)

    def test_symlinked_project_returns_resolved_target(self) -> None:
        target = self.home / "project"
        target.mkdir()
        link = self.root / "projlink"
        link.symlink_to(target)
        workdir, substituted = self._workdir(link)
        self.assertFalse(substituted)
        self.assertEqual(workdir, target)

    def test_project_dir_under_home_is_allowed(self) -> None:
        project = self.home / "code" / "project"
        project.mkdir(parents=True)
        self._assert_allowed(project)

    def test_workspace_symlink_is_refused(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.undertone_home.mkdir()
        self.workspace.symlink_to(elsewhere)
        with self.assertRaises(cli.WorkdirError):
            self._workdir(self.home)


class SessionHelpersTest(CliTestCase):
    TOK = "0123456789abcdef0123456789abcdef"

    def test_build_session_argv_exact(self) -> None:
        argv = cli.build_session_argv("{mcp}", "{set}", ["--model", "opus"])
        self.assertEqual(
            argv,
            [
                "claude",
                "--mcp-config",
                "{mcp}",
                "--settings",
                "{set}",
                "--dangerously-load-development-channels",
                "server:undertone",
                "--allowedTools",
                "mcp__undertone__reply",
                "--model",
                "opus",
            ],
        )

    def test_build_session_argv_custom_command(self) -> None:
        argv = cli.build_session_argv("{}", "{}", [], claude_cmd="/x/claude2")
        self.assertEqual(argv[0], "/x/claude2")

    def test_channel_mcp_config_shape(self) -> None:
        cfg = cli.channel_mcp_config("/py", Path("/home/u"), self.TOK)
        self.assertEqual(
            cfg,
            {
                "mcpServers": {
                    "undertone": {
                        "command": "/py",
                        "args": ["-P", "-m", "undertone.cli", "channel"],
                        "env": {"UNDERTONE_HOME": "/home/u", "UNDERTONE_SESSION": self.TOK},
                    }
                }
            },
        )

    def test_voice_guard_settings_shape(self) -> None:
        st = cli.voice_guard_settings("/py", Path("/h o/me"), self.TOK)
        self.assertIs(st["disableAllHooks"], False)
        hooks = st["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop", "SubagentStart", "SubagentStop"})
        for event, entries in hooks.items():
            self.assertEqual(entries[0]["hooks"][0]["timeout"], 10 if event == "PreToolUse" else 30)
            self.assertEqual(entries[0]["hooks"][0]["type"], "command")
        stop = hooks["Stop"][0]["hooks"][0]["command"]
        self.assertIn(self.TOK, shlex.split(stop))
        self.assertIn("stop", shlex.split(stop))
        self.assertNotIn("exit 2", stop)
        for event in ("SubagentStart", "SubagentStop"):
            self.assertNotIn("matcher", hooks[event][0])
            argv = shlex.split(hooks[event][0]["hooks"][0]["command"])
            self.assertIn(event.lower(), argv)
            self.assertIn(self.TOK, argv)
            self.assertNotIn("exit 2", hooks[event][0]["hooks"][0]["command"])
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "*")
        self.assertTrue(hooks["PreToolUse"][0]["hooks"][0]["command"].endswith("|| exit 2"))
        start = hooks["SessionStart"][0]["hooks"][0]["command"]
        self.assertEqual(
            shlex.split(start),
            ["/py", "-P", "-m", "undertone.voice_guard", "sessionstart",
             "--home", "/h o/me", "--session", self.TOK],
        )

    def test_session_execs_with_inline_configs(self) -> None:
        with mock.patch.object(cli.os, "execvpe") as execvpe, mock.patch.dict(
            os.environ,
            {
                "UNDERTONE_CLAUDE_CMD": "fakeclaude",
                "CLAUDE_CODE_SIMPLE": "1",
                "CLAUDE_CODE_SAFE_MODE": "1",
            },
        ):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                cli.main(["session", "--model", "opus"])
        cmd, argv, env = execvpe.call_args.args
        self.assertEqual(cmd, "fakeclaude")
        mcp = json.loads(argv[argv.index("--mcp-config") + 1])
        settings = json.loads(argv[argv.index("--settings") + 1])
        envcfg = mcp["mcpServers"]["undertone"]["env"]
        tok = envcfg["UNDERTONE_SESSION"]
        self.assertRegex(tok, voice_guard.TOKEN_RE)
        self.assertEqual(envcfg["UNDERTONE_HOME"], self._tmp.name)
        for entries in settings["hooks"].values():
            self.assertIn(tok, shlex.split(entries[0]["hooks"][0]["command"]))
        self.assertFalse((Path(self._tmp.name) / "channel.mcp.json").exists())
        self.assertEqual(argv[-2:], ["--model", "opus"])
        self.assertNotIn("CLAUDE_CODE_SIMPLE", env)
        self.assertNotIn("CLAUDE_CODE_SAFE_MODE", env)
        self.assertEqual(env.get("PATH"), os.environ.get("PATH"))
        self.assertEqual(env["UNDERTONE_HOME"], self._tmp.name)
        self.assertNotIn("UNDERTONE_SPEAK", env)
        self.assertIn("undertone attach", err.getvalue())

    def test_session_refuses_flags_that_defeat_the_guard(self) -> None:
        for flags in (["--settings", "x"], ["--settings=x"], ["--bare"], ["--safe-mode"]):
            with self.subTest(flags=flags), mock.patch.object(cli.os, "execvpe") as execvpe:
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = cli.main(["session", *flags])
                self.assertEqual(rc, 2)
                execvpe.assert_not_called()
                self.assertIn("remove " + flags[0].split("=")[0], err.getvalue())

    def _pretooluse(self, executable: str, home: str, tok: str) -> subprocess.CompletedProcess:
        cmd = cli.voice_guard_settings(executable, Path(home), tok)["hooks"]["PreToolUse"][0][
            "hooks"
        ][0]["command"]
        payload = {"tool_name": "Write", "tool_input": {"file_path": "x"}}
        return subprocess.run(
            ["/bin/sh", "-c", cmd], input=json.dumps(payload), capture_output=True, text=True
        )

    def test_pretooluse_command_end_to_end(self) -> None:
        home = tempfile.mkdtemp(prefix="ut")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        voice_guard.record_armed(home, self.TOK)
        voice_guard.record_voice(home, self.TOK, "delete it")
        res = self._pretooluse(sys.executable, home, self.TOK)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("deny", res.stdout)

    def test_pretooluse_blocks_when_interpreter_missing(self) -> None:
        home = tempfile.mkdtemp(prefix="ut")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        res = self._pretooluse("/nonexistent/python", home, self.TOK)
        self.assertEqual(res.returncode, 2)

    def test_new_commands_registered(self) -> None:
        parser = cli.build_parser()
        self.assertIs(parser.parse_args(["channel"]).func, cli._cmd_channel)
        self.assertIs(parser.parse_args(["session"]).func, cli._cmd_session)
        args = parser.parse_args(["attach", "--no-beep", "--follow-up", "0"])
        self.assertIs(args.func, cli._cmd_attach)
        self.assertTrue(args.no_beep)
        self.assertEqual(args.follow_up, 0.0)
        self.assertEqual(parser.parse_args(["attach"]).follow_up, 6.0)


class FakeEngine:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    def play(self, pcm: bytes) -> None:
        pass

    def clear(self) -> None:
        pass

    @property
    def playing(self) -> bool:
        return False

    def wait_drained(self, timeout: float | None = None) -> bool:
        return True

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def frames(self):
        return iter([b"frame"])


class MakeAudioIoTest(unittest.TestCase):
    def test_aec_on_builds_rendered_queue_on_the_engine(self) -> None:
        from undertone.player import RenderedSpeechQueue
        from undertone.screen import ScreenedSink

        engine = FakeEngine()
        io_ = cli.make_audio_io(
            False, engine_factory=lambda: engine, screen_factory=lambda: (lambda frame: False)
        )
        queues = [io_.player_factory(), io_.player_factory(lambda text: None)]
        try:
            for q in queues:
                self.assertIsInstance(q, RenderedSpeechQueue)
                self.assertIsInstance(q._sink, ScreenedSink)
                self.assertIs(q._sink._inner, engine)
        finally:
            for q in queues:
                q.close()

    def test_wake_factory_applies_probability_cutoff(self) -> None:
        mww = mock.MagicMock()
        fake_mod = mock.MagicMock()
        fake_mod.MicroWakeWord.from_builtin.return_value = mww
        with mock.patch.dict(sys.modules, {"pymicro_wakeword": fake_mod}):
            cli._make_wake_factory(probability_cutoff=0.8)()
            self.assertEqual(mww.probability_cutoff, 0.8)
            mww2 = mock.MagicMock(probability_cutoff=0.97)
            fake_mod.MicroWakeWord.from_builtin.return_value = mww2
            cli._make_wake_factory()()
            self.assertEqual(mww2.probability_cutoff, 0.97)

    def test_aec_on_talk_over_and_engine_delegation(self) -> None:
        engine = FakeEngine()
        io_ = cli.make_audio_io(
            False, engine_factory=lambda: engine, screen_factory=lambda: (lambda frame: False)
        )
        self.assertTrue(io_.talk_over)
        self.assertIs(io_.engine, engine)
        io_.start()
        self.assertTrue(engine.started)
        self.assertEqual(list(io_.frames()), [b"frame"])
        io_.close()
        self.assertTrue(engine.closed)
        self.assertIn("on", cli.banner_line(io_))

    def test_no_aec_uses_say_queue_without_engine(self) -> None:
        from undertone.player import SpeechQueue

        def factory():
            raise AssertionError("engine must not be built with --no-aec")

        io_ = cli.make_audio_io(True, engine_factory=factory)
        self.assertFalse(io_.talk_over)
        self.assertIsNone(io_.engine)
        q = io_.player_factory()
        try:
            self.assertIsInstance(q, SpeechQueue)
        finally:
            q.close()
        io_.start()
        io_.close()  # no engine: no-ops
        self.assertIn("off", cli.banner_line(io_))

    def test_parser_accepts_no_aec(self) -> None:
        parser = cli.build_parser()
        self.assertTrue(parser.parse_args(["chat", "--no-aec"]).no_aec)
        self.assertTrue(parser.parse_args(["attach", "--no-aec"]).no_aec)
        self.assertFalse(parser.parse_args(["chat"]).no_aec)
        self.assertFalse(parser.parse_args(["attach"]).no_aec)


if __name__ == "__main__":
    unittest.main()

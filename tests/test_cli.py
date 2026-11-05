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
        for name in ("serve", "say", "on", "off", "status", "hook", "hooks-snippet"):
            with self.subTest(command=name):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                    parser.parse_args([name])
                self.assertEqual(ctx.exception.code, 2)

    def test_help_lists_exactly_the_remaining_commands(self) -> None:
        sub = next(a for a in cli.build_parser()._actions if a.dest == "command")
        self.assertEqual(
            sorted(sub.choices),
            [
                "attach", "channel", "chat", "launch", "listen", "models",
                "safe-phrase", "session", "shell-init", "stop",
            ],
        )


class SafePhraseCommandTest(CliTestCase):
    def _safe(self, *argv: str, entries=("copper maple", "copper maple"), tty=True):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("getpass.getpass", side_effect=list(entries)), \
                mock.patch("sys.stdin.isatty", return_value=tty), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["safe-phrase", *argv])
        return rc, out.getvalue(), err.getvalue()

    def _file(self) -> Path:
        return voice_guard._safe_phrase_path(self._tmp.name)

    def test_set_succeeds_without_leaking_phrase(self) -> None:
        rc, out, err = self._safe()
        self.assertEqual(rc, 0)
        self.assertIn("Safe phrase set.", out)
        self.assertIn("Keep it private: anyone who hears it can use it to confirm.", out)
        self.assertEqual(self._file().stat().st_mode & 0o777, 0o600)
        self.assertTrue(voice_guard.safe_phrase_configured(self._tmp.name))
        for word in ("copper", "maple"):
            self.assertNotIn(word, out + err)

    def test_set_without_tty_exits_2_and_never_prompts(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("getpass.getpass") as gp, \
                mock.patch("sys.stdin.isatty", return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["safe-phrase"])
        self.assertEqual(rc, 2)
        gp.assert_not_called()
        self.assertIn("needs an interactive terminal", err.getvalue())
        self.assertFalse(self._file().exists())

    def test_status_and_clear_work_without_tty(self) -> None:
        self._safe()
        rc, out, _ = self._safe("--status", tty=False)
        self.assertEqual((rc, out.strip()), (0, "A safe phrase is set."))
        rc, out, _ = self._safe("--clear", tty=False)
        self.assertEqual(rc, 0)
        self.assertIn("Safe phrase removed", out)
        self.assertFalse(self._file().exists())

    def test_mismatch_exits_1_and_writes_nothing(self) -> None:
        rc, _, err = self._safe(entries=("copper maple", "copper birch"))
        self.assertEqual(rc, 1)
        self.assertIn("didn't match", err)
        self.assertFalse(self._file().exists())

    def test_invalid_phrase_exits_2_and_writes_nothing(self) -> None:
        rc, _, err = self._safe(entries=("hey jarvis",))
        self.assertEqual(rc, 2)
        self.assertTrue(err.strip())
        self.assertFalse(self._file().exists())

    def test_eof_exits_1_and_writes_nothing(self) -> None:
        for effect in (EOFError, KeyboardInterrupt):
            with self.subTest(effect=effect.__name__):
                out, err = io.StringIO(), io.StringIO()
                with mock.patch("getpass.getpass", side_effect=effect), \
                        mock.patch("sys.stdin.isatty", return_value=True), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = cli.main(["safe-phrase"])
                self.assertEqual(rc, 1)
                self.assertFalse(self._file().exists())

    def test_clear_with_and_without_phrase(self) -> None:
        rc, out, _ = self._safe("--clear")
        self.assertEqual((rc, out.strip()), (0, "No safe phrase was set."))
        self._safe()
        rc, out, _ = self._safe("--clear")
        self.assertEqual(rc, 0)
        self.assertIn("Safe phrase removed", out)
        self.assertFalse(self._file().exists())

    def test_status_both_ways(self) -> None:
        rc, out, _ = self._safe("--status")
        self.assertEqual(rc, 0)
        self.assertIn("No safe phrase is set", out)
        self._safe()
        rc, out, _ = self._safe("--status")
        self.assertEqual((rc, out.strip()), (0, "A safe phrase is set."))

    def test_help_lists_subcommand(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            cli.main(["--help"])
        self.assertIn("safe-phrase", buf.getvalue())


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

    def test_voice_guard_settings_denies_reading_the_safe_phrase_file(self) -> None:
        st = cli.voice_guard_settings("/py", Path("/tmp/x"), self.TOK)
        self.assertEqual(st["permissions"], {"deny": ["Read(//tmp/x/safe_phrase.json)"]})

    def test_voice_guard_settings_shape(self) -> None:
        st = cli.voice_guard_settings("/py", Path("/h o/me"), self.TOK)
        self.assertIs(st["disableAllHooks"], False)
        hooks = st["hooks"]
        self.assertEqual(set(hooks), {
            "SessionStart", "UserPromptSubmit", "PreToolUse", "Stop", "StopFailure",
            "PermissionRequest", "SubagentStart", "SubagentStop",
        })
        for event, entries in hooks.items():
            self.assertEqual(entries[0]["hooks"][0]["timeout"], 10 if event in ("PreToolUse", "StopFailure", "PermissionRequest") else 30)
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
        self.assertEqual(len(hooks["Stop"][0]["hooks"]), 2)
        self.assertIn("undertone.voice_guard", hooks["Stop"][0]["hooks"][0]["command"])
        for event, hook in (("Stop", "stop"), ("StopFailure", "stopfailure"), ("PermissionRequest", "permissionrequest")):
            handler = hooks[event][0]["hooks"][-1]
            self.assertIs(handler["async"], True)
            self.assertEqual(handler["timeout"], 10)
            self.assertNotIn("exit 2", handler["command"])
            self.assertEqual(
                shlex.split(handler["command"]),
                ["/py", "-P", "-m", "undertone.announce", hook,
                 "--home", "/h o/me", "--session", self.TOK],
            )
        self.assertEqual(hooks["PermissionRequest"][0]["matcher"], "*")
        self.assertNotIn("matcher", hooks["StopFailure"][0])
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
            for handler in entries[0]["hooks"]:
                self.assertIn(tok, shlex.split(handler["command"]))
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

    def test_attach_announce_option(self) -> None:
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["attach"]).announce, "all")
        for level in ("all", "permission", "off"):
            self.assertEqual(parser.parse_args(["attach", "--announce", level]).announce, level)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["attach", "--announce", "loud"])

    def test_cmd_attach_passes_announce(self) -> None:
        args = cli.build_parser().parse_args(["attach", "--announce", "permission"])
        with mock.patch("undertone.voice.run_attach", return_value=0) as run:
            self.assertEqual(cli._cmd_attach(args), 0)
        self.assertEqual(run.call_args.kwargs["announce"], "permission")


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
            False,
            tts="say",
            engine_factory=lambda: engine,
            screen_factory=lambda: (lambda frame: False),
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
            False,
            tts="say",
            engine_factory=lambda: engine,
            screen_factory=lambda: (lambda frame: False),
        )
        self.assertTrue(io_.talk_over)
        self.assertIs(io_.engine, engine)
        io_.start()
        self.assertTrue(engine.started)
        self.assertEqual(list(io_.frames()), [b"frame"])
        io_.close()
        self.assertTrue(engine.closed)
        self.assertIn("on", cli.banner_line(io_))
        self.assertIn("Voice: say (", cli.banner_line(io_))

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

    def _kokoro_io(self, *, factory, which="/usr/bin/espeak-ng", err=None, **kw):
        err = err if err is not None else io.StringIO()
        kw.setdefault("screen_factory", lambda: (lambda frame: False))
        with mock.patch.object(cli.shutil, "which", return_value=which):
            return cli.make_audio_io(
                False,
                engine_factory=FakeEngine,
                kokoro_factory=factory,
                err=err,
                **kw,
            )

    def test_kokoro_path_shares_one_voice_across_players(self) -> None:
        from undertone.player import KokoroRenderer

        voice = mock.Mock(voice="am_michael", own_voice="af_heart", voice_names=["am_michael"])
        factory = mock.Mock(return_value=voice)
        err = io.StringIO()
        io_ = self._kokoro_io(factory=factory, err=err)
        queues = [io_.player_factory(), io_.player_factory(lambda text: None)]
        try:
            for q in queues:
                self.assertIsInstance(q._renderer, KokoroRenderer)
                self.assertIs(q._renderer._voice, voice)
        finally:
            for q in queues:
                q.close()
        factory.assert_called_once_with()
        self.assertEqual(
            io_.voice_label, "Kokoro (am_michael; undertone's own voice: af_heart)"
        )
        self.assertIn(
            "Voice: Kokoro (am_michael; undertone's own voice: af_heart).", cli.banner_line(io_)
        )
        self.assertEqual(err.getvalue(), "")

    def test_kokoro_unavailable_falls_back_to_say_with_one_line(self) -> None:
        from undertone.kokoro import KokoroUnavailable
        from undertone.player import SayRenderer

        err = io.StringIO()
        io_ = self._kokoro_io(factory=mock.Mock(side_effect=KokoroUnavailable("x")), err=err)
        q = io_.player_factory()
        try:
            self.assertIsInstance(q._renderer, SayRenderer)
        finally:
            q.close()
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("using say", lines[0])
        self.assertTrue(io_.voice_label.startswith("say ("))

    def test_missing_espeak_falls_back_without_loading_the_voice(self) -> None:
        from undertone.player import SayRenderer

        factory = mock.Mock()
        err = io.StringIO()
        io_ = self._kokoro_io(factory=factory, which=None, err=err)
        q = io_.player_factory()
        try:
            self.assertIsInstance(q._renderer, SayRenderer)
        finally:
            q.close()
        factory.assert_not_called()
        self.assertIn("brew install espeak-ng", err.getvalue())
        self.assertEqual(len(err.getvalue().splitlines()), 1)

    def test_screen_failure_prints_one_distinct_line(self) -> None:
        def broken_screen():
            raise RuntimeError("detector down")

        err = io.StringIO()
        io_ = self._kokoro_io(factory=mock.Mock(), tts="say", err=err, screen_factory=broken_screen)
        q = io_.player_factory()
        try:
            self.assertFalse(q._sink.play(b"\x00\x00" * 160))
            self.assertFalse(q._sink.play(b"\x00\x00" * 160))
        finally:
            q.close()
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("wake-word screen failed (detector down)", lines[0])
        self.assertIn("muted until restart", lines[0])

    def test_tts_say_never_loads_kokoro(self) -> None:
        factory = mock.Mock()
        io_ = self._kokoro_io(factory=factory, tts="say")
        factory.assert_not_called()
        self.assertTrue(io_.voice_label.startswith("say ("))

    def test_no_aec_keeps_say_queue_even_with_kokoro_tts(self) -> None:
        from undertone.player import SpeechQueue

        factory = mock.Mock()
        io_ = cli.make_audio_io(True, tts="kokoro", kokoro_factory=factory)
        q = io_.player_factory()
        try:
            self.assertIsInstance(q, SpeechQueue)
        finally:
            q.close()
        factory.assert_not_called()
        self.assertTrue(io_.voice_label.startswith("say ("))
        self.assertIn("Voice: say (", cli.banner_line(io_))

    def test_voice_label_honors_undertone_voice(self) -> None:
        env = {"UNDERTONE_VOICE": "Daniel", "UNDERTONE_OWN_VOICE": ""}
        with mock.patch.dict(os.environ, env):
            io_ = cli.make_audio_io(True)
        self.assertEqual(io_.voice_label, "say (Daniel; undertone's own voice: Samantha)")

    def test_say_label_default_and_own_override(self) -> None:
        env = {"UNDERTONE_VOICE": "", "UNDERTONE_OWN_VOICE": ""}
        with mock.patch.dict(os.environ, env):
            err = io.StringIO()
            io_ = cli.make_audio_io(True, err=err)
            self.assertEqual(
                io_.voice_label, "say (Evan (Enhanced); undertone's own voice: Samantha)"
            )
            self.assertEqual(err.getvalue(), "")
            os.environ["UNDERTONE_OWN_VOICE"] = "Fred"
            self.assertEqual(
                cli.make_audio_io(True, err=err).voice_label,
                "say (Evan (Enhanced); undertone's own voice: Fred)",
            )
            self.assertEqual(err.getvalue(), "")

    def test_same_own_voice_refuses_for_kokoro(self) -> None:
        voice = mock.Mock(voice="af_heart", own_voice="af_heart", voice_names=["af_heart"])
        with self.assertRaises(cli.OwnVoiceError) as ctx:
            self._kokoro_io(factory=mock.Mock(return_value=voice))
        self.assertIn("af_heart", str(ctx.exception))

    def test_blend_containing_own_voice_refuses(self) -> None:
        blend = "am_adam:0.6,af_heart:0.4"
        voice = mock.Mock(voice=blend, own_voice="af_heart", voice_names=["am_adam", "af_heart"])
        with self.assertRaises(cli.OwnVoiceError) as ctx:
            self._kokoro_io(factory=mock.Mock(return_value=voice))
        self.assertIn(blend, str(ctx.exception))

    def test_blend_without_own_voice_is_silent(self) -> None:
        voice = mock.Mock(voice="am_adam:0.6,bm_george:0.4", own_voice="af_heart",
                          voice_names=["am_adam", "bm_george"])
        err = io.StringIO()
        self._kokoro_io(factory=mock.Mock(return_value=voice), err=err)
        self.assertEqual(err.getvalue(), "")

    def test_same_own_voice_refuses_for_say_fallback(self) -> None:
        env = {"UNDERTONE_VOICE": "Samantha", "UNDERTONE_OWN_VOICE": "Samantha"}
        with mock.patch.dict(os.environ, env), self.assertRaises(cli.OwnVoiceError) as ctx:
            self._kokoro_io(factory=mock.Mock(), tts="say")
        self.assertIn("Samantha", str(ctx.exception))

    def test_same_own_voice_refuses_on_no_aec_path(self) -> None:
        env = {"UNDERTONE_VOICE": "Samantha", "UNDERTONE_OWN_VOICE": ""}
        with mock.patch.dict(os.environ, env), self.assertRaises(cli.OwnVoiceError):
            cli.make_audio_io(True, err=io.StringIO())

    def test_default_say_voices_do_not_refuse(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("UNDERTONE_VOICE", "UNDERTONE_OWN_VOICE")}
        with mock.patch.dict(os.environ, env, clear=True):
            cli.make_audio_io(True, err=io.StringIO())

    def test_parser_tts_option(self) -> None:
        parser = cli.build_parser()
        for cmd in ("chat", "attach"):
            self.assertEqual(parser.parse_args([cmd]).tts, "kokoro")
            self.assertEqual(parser.parse_args([cmd, "--tts", "say"]).tts, "say")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args([cmd, "--tts", "foo"])

    def test_models_fetch_calls_both_fetchers_in_order(self) -> None:
        from undertone import kokoro, stt

        calls: list[str] = []
        out = io.StringIO()
        with (
            mock.patch.object(stt, "fetch_model", lambda: calls.append("stt")),
            mock.patch.object(kokoro, "fetch_model", lambda: calls.append("kokoro")),
            mock.patch.object(cli.shutil, "which", return_value=None),
            contextlib.redirect_stdout(out),
        ):
            rc = cli._cmd_models_fetch(mock.Mock())
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["stt", "kokoro"])
        self.assertIn("models cached", out.getvalue())
        self.assertIn("brew install espeak-ng", out.getvalue())

    def test_models_fetch_omits_espeak_hint_when_installed(self) -> None:
        from undertone import kokoro, stt

        out = io.StringIO()
        with (
            mock.patch.object(stt, "fetch_model"),
            mock.patch.object(kokoro, "fetch_model"),
            mock.patch.object(cli.shutil, "which", return_value="/x/espeak-ng"),
            contextlib.redirect_stdout(out),
        ):
            cli._cmd_models_fetch(mock.Mock())
        self.assertNotIn("espeak", out.getvalue())


class LaunchModeTest(unittest.TestCase):
    def _mode(self, argv, env=None, stdin=True, stdout=True) -> str:
        return cli.launch_mode(argv, env or {}, stdin, stdout)

    def test_voice_cases(self) -> None:
        for argv in ([], ["--model", "opus"], ["fix the bug"], ["--resume"]):
            with self.subTest(argv=argv):
                self.assertEqual(self._mode(argv), "voice")

    def test_passthrough_cases(self) -> None:
        cases = {
            "optout": ([], {"UNDERTONE_AUTOLAUNCH": "0"}, True, True),
            "stdin not tty": ([], {}, False, True),
            "stdout not tty": ([], {}, True, False),
            "print": (["-p", "hi"], {}, True, True),
            "print long": (["--print"], {}, True, True),
            "help": (["--help"], {}, True, True),
            "help short": (["-h"], {}, True, True),
            "version": (["-v"], {}, True, True),
            "version long": (["--version"], {}, True, True),
            "settings": (["--settings", "x.json"], {}, True, True),
            "settings=": (["--settings=x.json"], {}, True, True),
            "bare": (["--bare"], {}, True, True),
            "safe-mode": (["--safe-mode"], {}, True, True),
            "subcommand": (["mcp", "list"], {}, True, True),
            "subcommand after option": (["--model", "x", "doctor"], {}, True, True),
        }
        for name, (argv, env, stdin, stdout) in cases.items():
            with self.subTest(name):
                self.assertEqual(self._mode(argv, env, stdin, stdout), "passthrough")

    def test_autolaunch_other_value_still_voice(self) -> None:
        self.assertEqual(self._mode([], {"UNDERTONE_AUTOLAUNCH": "1"}), "voice")


class LaunchCommandTest(CliTestCase):
    def test_passthrough_execs_plain_claude(self) -> None:
        with (
            mock.patch.dict(os.environ, {"UNDERTONE_CLAUDE_CMD": "fakeclaude"}),
            mock.patch.object(cli.os, "execvpe") as execvpe,
            mock.patch.object(cli, "_ensure_attach") as ensure,
        ):
            self.assertEqual(cli.main(["launch", "--", "mcp", "list"]), 1)
        execvpe.assert_called_once_with(
            "fakeclaude", ["fakeclaude", "mcp", "list"], os.environ
        )
        ensure.assert_not_called()

    def test_voice_ensures_attach_then_runs_session(self) -> None:
        with (
            mock.patch.object(cli, "launch_mode", return_value="voice"),
            mock.patch.object(cli, "_ensure_attach") as ensure,
            mock.patch.object(cli, "_exec_session", return_value=0) as session,
        ):
            self.assertEqual(cli.main(["launch", "--", "--model", "x"]), 0)
        ensure.assert_called_once_with()
        session.assert_called_once_with(["--model", "x"], warn_no_voice=False)


class EnsureAttachTest(CliTestCase):
    def test_spawns_detached_attach_with_log_and_pidfile(self) -> None:
        from undertone import voice

        child = mock.Mock(pid=4242)
        err = io.StringIO()
        with (
            mock.patch.object(voice, "_server_answers", return_value=False),
            mock.patch.object(cli.subprocess, "Popen", return_value=child) as popen,
            contextlib.redirect_stderr(err),
        ):
            cli._ensure_attach()
        argv = popen.call_args.args[0]
        self.assertEqual(argv, [sys.executable, "-m", "undertone", "attach"])
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        log = paths.attach_log_path()
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(paths.read_attach_pid(), 4242)
        self.assertEqual(paths.attach_pid_path().stat().st_mode & 0o777, 0o600)
        self.assertIn("starting voice in the background", err.getvalue())
        self.assertIn(str(log), err.getvalue())

    def test_no_spawn_when_socket_answers(self) -> None:
        from undertone import voice

        with (
            mock.patch.object(voice, "_server_answers", return_value=True),
            mock.patch.object(cli.subprocess, "Popen") as popen,
        ):
            cli._ensure_attach()
        popen.assert_not_called()

    def test_no_spawn_while_previous_attach_still_loading(self) -> None:
        from undertone import voice

        paths.write_attach_pid(777)
        with (
            mock.patch.object(voice, "_server_answers", return_value=False),
            mock.patch.object(cli, "_pid_is_attach", return_value=True),
            mock.patch.object(cli.subprocess, "Popen") as popen,
        ):
            cli._ensure_attach()
        popen.assert_not_called()

    def test_symlinked_pidfile_is_refused(self) -> None:
        target = Path(self._tmp.name) / "victim"
        target.write_text("keep")
        paths.attach_pid_path().symlink_to(target)
        with self.assertRaises(OSError):
            paths.write_attach_pid(1)
        self.assertEqual(target.read_text(), "keep")

    def test_preexisting_loose_log_is_tightened(self) -> None:
        from undertone import voice

        log = paths.attach_log_path()
        log.write_text("old\n")
        log.chmod(0o644)
        with (
            mock.patch.object(voice, "_server_answers", return_value=False),
            mock.patch.object(cli.subprocess, "Popen", return_value=mock.Mock(pid=9)),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            cli._ensure_attach()
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_symlinked_log_warns_and_does_not_raise(self) -> None:
        from undertone import voice

        target = Path(self._tmp.name) / "victim"
        target.write_text("keep")
        paths.attach_log_path().symlink_to(target)
        err = io.StringIO()
        with (
            mock.patch.object(voice, "_server_answers", return_value=False),
            mock.patch.object(cli.subprocess, "Popen") as popen,
            contextlib.redirect_stderr(err),
        ):
            cli._ensure_attach()
        popen.assert_not_called()
        self.assertIn("could not start voice", err.getvalue())
        self.assertEqual(target.read_text(), "keep")

    def test_popen_failure_warns_and_does_not_raise(self) -> None:
        from undertone import voice

        err = io.StringIO()
        with (
            mock.patch.object(voice, "_server_answers", return_value=False),
            mock.patch.object(cli.subprocess, "Popen", side_effect=OSError("boom")),
            contextlib.redirect_stderr(err),
        ):
            cli._ensure_attach()
        self.assertIn("boom", err.getvalue())

    def test_spawn_takes_the_flock(self) -> None:
        from undertone import voice

        modes = []
        real_flock = cli.fcntl.flock
        with (
            mock.patch.object(voice, "_server_answers", return_value=True),
            mock.patch.object(
                cli.fcntl, "flock", side_effect=lambda fd, op: modes.append(op) or real_flock(fd, op)
            ),
        ):
            cli._ensure_attach()
        self.assertEqual(modes, [cli.fcntl.LOCK_EX])
        self.assertEqual(paths.attach_lock_path().stat().st_mode & 0o777, 0o600)


class StopCommandTest(CliTestCase):
    def test_no_pidfile(self) -> None:
        rc, out = self._run("stop")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to stop", out)

    def test_refuses_pid_that_is_not_attach(self) -> None:
        paths.write_attach_pid(31337)
        with (
            mock.patch.object(cli, "_pid_is_attach", return_value=False),
            mock.patch.object(cli.os, "kill") as kill,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            rc, _ = self._run("stop")
        self.assertEqual(rc, 1)
        kill.assert_not_called()

    def test_sigterms_a_verified_attach(self) -> None:
        paths.write_attach_pid(31337)
        with (
            mock.patch.object(cli, "_pid_is_attach", return_value=True),
            mock.patch.object(cli.os, "kill") as kill,
        ):
            rc, out = self._run("stop")
        self.assertEqual(rc, 0)
        kill.assert_called_once_with(31337, cli.signal.SIGTERM)
        self.assertIn("31337", out)

    def test_pid_is_attach_checks_command_line(self) -> None:
        def ps(command: str):
            return mock.Mock(stdout=command)

        with mock.patch.object(cli.subprocess, "run", return_value=ps("vim notes.txt\n")):
            self.assertFalse(cli._pid_is_attach(1))
        with mock.patch.object(
            cli.subprocess,
            "run",
            return_value=ps("/x/python -m undertone attach\n"),
        ):
            self.assertTrue(cli._pid_is_attach(1))

    def test_pid_is_attach_rejects_lookalike_command_lines(self) -> None:
        for command in (
            "tail -f /Users/x/.undertone/attach.log\n",
            "vim undertone-attach-notes\n",
        ):
            with self.subTest(command=command):
                with mock.patch.object(
                    cli.subprocess, "run", return_value=mock.Mock(stdout=command)
                ):
                    self.assertFalse(cli._pid_is_attach(1))
        for command in (
            "/x/.venv/bin/python /x/.venv/bin/undertone attach --no-beep\n",
            "/x/python -m undertone attach\n",
        ):
            with self.subTest(command=command):
                with mock.patch.object(
                    cli.subprocess, "run", return_value=mock.Mock(stdout=command)
                ):
                    self.assertTrue(cli._pid_is_attach(1))

    def test_remove_pid_only_if_it_is_ours(self) -> None:
        paths.write_attach_pid(5)
        paths.remove_attach_pid(6)
        self.assertEqual(paths.read_attach_pid(), 5)
        paths.remove_attach_pid(5)
        self.assertIsNone(paths.read_attach_pid())


class ShellInitTest(CliTestCase):
    def test_zsh_function_uses_absolute_executable(self) -> None:
        with mock.patch.object(cli.sys, "argv", ["/opt/v/bin/undertone", "shell-init", "zsh"]):
            rc, out = self._run("shell-init", "zsh")
        self.assertEqual(rc, 0)
        self.assertIn('claude() { /opt/v/bin/undertone launch -- "$@"; }', out)
        self.assertIn("command claude", out)
        self.assertIn("add-zsh-hook precmd _undertone_claude_precmd", out)
        self.assertIn("add-zsh-hook -d precmd _undertone_claude_precmd", out)

    @unittest.skipUnless(shutil.which("zsh"), "zsh not installed")
    def test_function_survives_cmux_redefining_claude_at_first_prompt(self) -> None:
        with mock.patch.object(cli.sys, "argv", ["/opt/v/bin/undertone", "shell-init", "zsh"]):
            _, init = self._run("shell-init", "zsh")
        script = (
            "autoload -Uz add-zsh-hook\n"
            "_cmux_fix_path() {\n"
            "  claude() { _cmux_claude_wrapper_command \"$@\"; }\n"
            "  add-zsh-hook -d precmd _cmux_fix_path\n"
            "}\n"
            "add-zsh-hook precmd _cmux_fix_path\n"  # cmux loads before .zshrc
            f"{init}\n"
            "for f in $precmd_functions; do $f; done\n"
            "whence -f claude\n"
        )
        proc = subprocess.run(
            ["zsh", "-f", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "UNDERTONE_HOME": self._tmp.name},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("undertone launch", proc.stdout)
        self.assertNotIn("_cmux_claude_wrapper_command", proc.stdout)

    def test_only_zsh(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            cli.main(["shell-init", "bash"])


if __name__ == "__main__":
    unittest.main()

"""Smoke tests for undertone.cli."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from undertone import cli, paths


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


class HooksSnippetTest(CliTestCase):
    def test_prints_valid_json_with_both_hooks(self) -> None:
        rc, out = self._run("hooks-snippet")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        stop_hooks = payload["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(stop_hooks["command"], "undertone hook stop")
        self.assertTrue(stop_hooks["async"])
        prompt_hooks = payload["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertEqual(prompt_hooks["command"], "undertone hook prompt")


class OnOffStatusTest(CliTestCase):
    def test_on_creates_flag_off_removes_it(self) -> None:
        rc, _ = self._run("on")
        self.assertEqual(rc, 0)
        self.assertTrue(paths.enabled_flag_path().exists())

        rc, _ = self._run("off")
        self.assertEqual(rc, 0)
        self.assertFalse(paths.enabled_flag_path().exists())

    def test_status_reports_down_and_disabled_by_default(self) -> None:
        rc, out = self._run("status")
        self.assertEqual(rc, 0)
        self.assertIn("daemon: down", out)
        self.assertIn("enabled: no", out)

    def test_status_reports_enabled_after_on(self) -> None:
        self._run("on")
        rc, out = self._run("status")
        self.assertEqual(rc, 0)
        self.assertIn("enabled: yes", out)


class HookCommandTest(CliTestCase):
    def test_hook_stop_reads_stdin_and_returns_zero(self) -> None:
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"last_assistant_message": "hi"}))):
            rc = cli.main(["hook", "stop"])
        self.assertEqual(rc, 0)

    def test_hook_prompt_reads_stdin_and_returns_zero(self) -> None:
        with mock.patch("sys.stdin", io.StringIO("{}")):
            rc, _out = self._run_hook_prompt()
        self.assertEqual(rc, 0)

    def _run_hook_prompt(self) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["hook", "prompt"])
        return rc, buf.getvalue()


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
    def test_build_session_argv_exact(self) -> None:
        argv = cli.build_session_argv(Path("/h/channel.mcp.json"), ["--model", "opus"])
        self.assertEqual(
            argv,
            [
                "claude",
                "--mcp-config",
                "/h/channel.mcp.json",
                "--dangerously-load-development-channels",
                "server:undertone",
                "--allowedTools",
                "mcp__undertone__reply",
                "--model",
                "opus",
            ],
        )

    def test_build_session_argv_custom_command(self) -> None:
        argv = cli.build_session_argv(Path("/c.json"), [], claude_cmd="/x/claude2")
        self.assertEqual(argv[0], "/x/claude2")

    def test_channel_mcp_config_shape(self) -> None:
        cfg = cli.channel_mcp_config("/py", Path("/home/u"))
        self.assertEqual(
            cfg,
            {
                "mcpServers": {
                    "undertone": {
                        "command": "/py",
                        "args": ["-P", "-m", "undertone.cli", "channel"],
                        "env": {"UNDERTONE_HOME": "/home/u"},
                    }
                }
            },
        )

    def test_write_private_json_cleans_up_on_failure(self) -> None:
        target = Path(self._tmp.name) / "x.json"
        with self.assertRaises(TypeError):
            cli._write_private_json(target, {"bad": object()})
        self.assertEqual(list(Path(self._tmp.name).glob("x.json*")), [])

    def test_session_writes_0600_config_and_execs(self) -> None:
        with mock.patch.object(cli.os, "execvpe") as execvpe, mock.patch.dict(
            os.environ, {"UNDERTONE_CLAUDE_CMD": "fakeclaude"}
        ):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                cli.main(["session", "--model", "opus"])
        cfg = paths.channel_config_path()
        self.assertEqual(cfg.stat().st_mode & 0o777, 0o600)
        data = json.loads(cfg.read_text())
        self.assertEqual(data["mcpServers"]["undertone"]["env"]["UNDERTONE_HOME"], self._tmp.name)
        cmd, argv, env = execvpe.call_args.args
        self.assertEqual(cmd, "fakeclaude")
        self.assertEqual(argv[-2:], ["--model", "opus"])
        self.assertEqual(env["UNDERTONE_SPEAK"], "0")
        self.assertIn("undertone attach", err.getvalue())

    def test_new_commands_registered(self) -> None:
        parser = cli.build_parser()
        self.assertIs(parser.parse_args(["channel"]).func, cli._cmd_channel)
        self.assertIs(parser.parse_args(["session"]).func, cli._cmd_session)
        args = parser.parse_args(["attach", "--no-beep", "--follow-up", "0"])
        self.assertIs(args.func, cli._cmd_attach)
        self.assertTrue(args.no_beep)
        self.assertEqual(args.follow_up, 0.0)
        self.assertEqual(parser.parse_args(["attach"]).follow_up, 6.0)


if __name__ == "__main__":
    unittest.main()

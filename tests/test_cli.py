"""Smoke tests for undertone.cli."""

import contextlib
import io
import json
import os
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()

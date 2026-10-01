"""Tests for undertone.hooks.{stop_hook,prompt_hook}."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from undertone import hooks, paths, speaker


class HooksTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env_patch = mock.patch.dict(
            os.environ, {"UNDERTONE_HOME": self._tmp.name}, clear=False
        )
        self._env_patch.start()
        os.environ.pop("UNDERTONE_SPEAK", None)

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._tmp.cleanup()

    def _enable(self) -> None:
        paths.enabled_flag_path().touch()


class StopHookTest(HooksTestCase):
    def test_disabled_sends_nothing(self) -> None:
        with mock.patch.object(speaker, "send") as send:
            rc = hooks.stop_hook(json.dumps({"last_assistant_message": "Hello there."}))
        self.assertEqual(rc, 0)
        send.assert_not_called()

    def test_stop_hook_active_sends_nothing(self) -> None:
        self._enable()
        with mock.patch.object(speaker, "send") as send:
            rc = hooks.stop_hook(
                json.dumps(
                    {"stop_hook_active": True, "last_assistant_message": "Hello there."}
                )
            )
        self.assertEqual(rc, 0)
        send.assert_not_called()

    def test_empty_spoken_text_sends_nothing(self) -> None:
        self._enable()
        with mock.patch.object(speaker, "send") as send:
            rc = hooks.stop_hook(json.dumps({"last_assistant_message": "```\ncode\n```"}))
        self.assertEqual(rc, 0)
        send.assert_not_called()

    def test_enabled_sends_expected_text(self) -> None:
        self._enable()
        with mock.patch.object(speaker, "send", return_value=True) as send:
            rc = hooks.stop_hook(json.dumps({"last_assistant_message": "I fixed it. Done."}))
        self.assertEqual(rc, 0)
        send.assert_called_once()
        (msg,), _kwargs = send.call_args
        self.assertEqual(msg["op"], "say")
        self.assertIn("I fixed it.", msg["text"])

    def test_env_opt_in_without_flag_file(self) -> None:
        os.environ["UNDERTONE_SPEAK"] = "1"
        try:
            with mock.patch.object(speaker, "send", return_value=True) as send:
                rc = hooks.stop_hook(json.dumps({"last_assistant_message": "All set."}))
            self.assertEqual(rc, 0)
            send.assert_called_once()
        finally:
            os.environ.pop("UNDERTONE_SPEAK", None)

    def test_daemon_down_still_returns_zero(self) -> None:
        self._enable()
        with mock.patch.object(speaker, "send", return_value=False):
            rc = hooks.stop_hook(json.dumps({"last_assistant_message": "Still works."}))
        self.assertEqual(rc, 0)

    def test_malformed_stdin_returns_zero_and_logs(self) -> None:
        self._enable()
        rc = hooks.stop_hook("not valid json {{{")
        self.assertEqual(rc, 0)
        log_path = paths.log_dir() / "hook.log"
        self.assertTrue(log_path.exists())
        self.assertIn("stop_hook", log_path.read_text())

    def test_missing_fields_do_not_crash(self) -> None:
        self._enable()
        with mock.patch.object(speaker, "send") as send:
            rc = hooks.stop_hook("{}")
        self.assertEqual(rc, 0)
        send.assert_not_called()


class PromptHookTest(HooksTestCase):
    def test_disabled_prints_nothing(self) -> None:
        with mock.patch("builtins.print") as mock_print:
            rc = hooks.prompt_hook("{}")
        self.assertEqual(rc, 0)
        mock_print.assert_not_called()

    def test_enabled_prints_valid_hook_json(self) -> None:
        self._enable()
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hooks.prompt_hook("{}")
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(
            payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit"
        )
        self.assertIn("SPOKEN:", payload["hookSpecificOutput"]["additionalContext"])

    def test_malformed_stdin_does_not_crash(self) -> None:
        # prompt_hook ignores stdin entirely, so malformed input is a no-op --
        # it still prints the opt-in instruction when enabled; just assert rc.
        self._enable()
        with mock.patch("builtins.print"):
            rc = hooks.prompt_hook("not json at all")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()

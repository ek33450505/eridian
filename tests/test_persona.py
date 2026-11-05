import hashlib
import io
import json
import os
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from eridian import cli, mcp_channel, persona


class ResolveTest(unittest.TestCase):
    def test_off_values(self) -> None:
        for value in (None, "", "off", "OFF"):
            env = {} if value is None else {persona.PERSONA_ENV: value}
            self.assertEqual(persona.resolve(env), "")

    def test_rocky(self) -> None:
        self.assertEqual(persona.resolve({persona.PERSONA_ENV: "Rocky"}), "rocky")

    def test_invalid_fails_closed(self) -> None:
        with self.assertRaises(persona.PersonaError):
            persona.resolve({persona.PERSONA_ENV: "pirate"})


class InstructionsTest(unittest.TestCase):
    # SHA-256 of build_instructions(safe_phrase), as at HEAD 4c12c80 (before the persona existed) with the product-name rename applied.
    GOLDEN = {
        False: "514b75e112d2f5c359cedbc21a8e0cb9f5a5bacd5123dce12c7962759cd612f1",
        True: "87bc8aae967103a8ab8037d592fa208fa2ebf987853b4815c562227e7adfcdb6",
    }

    def test_off_is_byte_identical(self) -> None:
        for safe, digest in self.GOLDEN.items():
            for text in (
                mcp_channel.build_instructions(safe),
                mcp_channel.build_instructions(safe, persona=""),
            ):
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digest)
        self.assertEqual(mcp_channel.build_instructions(False), mcp_channel.INSTRUCTIONS)

    def test_unknown_persona_raises(self) -> None:
        with self.assertRaises(ValueError):
            mcp_channel.build_instructions(False, persona="pirate")

    def test_rocky_block_in_both_variants(self) -> None:
        for safe in (False, True):
            base = mcp_channel.build_instructions(safe)
            text = mcp_channel.build_instructions(safe, persona="rocky")
            self.assertTrue(text.startswith(base))
            self.assertIn(persona.ROCKY_BLOCK, text)
            self.assertIn(
                'This persona applies ONLY to replies with kind="answer" or kind="ack". ', text
            )
            self.assertIn(
                'A reply with kind="confirm" must use the exact plain confirm wording required '
                'above (the "confirm <word1> <word2>" or safe-phrase text), with no persona '
                'style: no "question" suffix and no tripled words. ',
                text,
            )
            self.assertIn(
                "If anything here conflicts with the rules above, the rules above win.", text
            )
            self.assertIn("the rules above win", text)
            self.assertIn("accuracy beats style", text)
            self.assertIn("Never say your own name or the wake phrase", text)

    def test_block_names_nothing_to_say(self) -> None:
        self.assertNotIn("rocky", persona.ROCKY_BLOCK.lower())


class PassthroughTest(unittest.TestCase):
    def _env(self, **extra: str) -> dict:
        with mock.patch.dict(os.environ, extra):
            cfg = cli.channel_mcp_config("py", Path("/h"), "tok")
            return cfg["mcpServers"]["eridian"]["env"]

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(persona.PERSONA_ENV, None)

    def test_unset_not_passed(self) -> None:
        self.assertNotIn(persona.PERSONA_ENV, self._env())

    def test_rocky_reaches_channel_env(self) -> None:
        env = self._env(ERIDIAN_PERSONA="rocky")
        self.assertEqual(env[persona.PERSONA_ENV], "rocky")
        json.dumps(env)

    def test_initialize_uses_env(self) -> None:
        os.environ[persona.PERSONA_ENV] = "rocky"
        out = io.StringIO()
        srv = mcp_channel.ChannelServer(io.StringIO(), out, Path("/nonexistent.sock"))
        srv._handle_initialize(1, {})
        self.assertIn(persona.ROCKY_BLOCK, json.loads(out.getvalue())["result"]["instructions"])

    def test_invalid_fails_closed_in_cli_and_channel(self) -> None:
        os.environ[persona.PERSONA_ENV] = "pirate"
        err = io.StringIO()
        with redirect_stderr(err):
            with mock.patch.object(cli.os, "execvpe") as execvpe:
                self.assertEqual(cli.main(["session"]), 1)
            execvpe.assert_not_called()
            self.assertEqual(mcp_channel.main(), 1)
        self.assertEqual(err.getvalue().count("ERIDIAN_PERSONA"), 2)


class LaunchGateTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(
            os.environ, {persona.PERSONA_ENV: "pirate", "ERIDIAN_CLAUDE_CMD": "fakeclaude"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_passthrough_ignores_invalid_persona(self) -> None:
        with (
            mock.patch.object(cli.os, "execvpe") as execvpe,
            mock.patch.object(cli, "_ensure_attach") as ensure,
        ):
            cli.main(["launch", "--", "mcp", "list"])
        execvpe.assert_called_once_with("fakeclaude", ["fakeclaude", "mcp", "list"], os.environ)
        ensure.assert_not_called()

    def test_voice_launch_refuses_before_attach(self) -> None:
        err = io.StringIO()
        with (
            mock.patch.object(cli, "launch_mode", return_value="voice"),
            mock.patch.object(cli, "_ensure_attach") as ensure,
            mock.patch.object(cli.os, "execvpe") as execvpe,
            redirect_stderr(err),
        ):
            self.assertEqual(cli.main(["launch", "--", "--model", "x"]), 1)
        ensure.assert_not_called()
        execvpe.assert_not_called()
        self.assertIn("ERIDIAN_PERSONA", err.getvalue())


if __name__ == "__main__":
    unittest.main()

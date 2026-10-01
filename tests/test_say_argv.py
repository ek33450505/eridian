"""Security tests for speaker.build_say_argv, the single path to the TTS command.

Pure argv tests -- `say` is never run. Covers: argv flag-injection into the TTS
command, NUL bytes, wake-phrase / [[ ]] scrubbing, and voice selection.
"""

import os
import unittest
from unittest import mock

from undertone import speaker


class BuildSayArgvSanitizingTest(unittest.TestCase):
    def test_wake_phrase_and_say_commands_never_reach_argv(self) -> None:
        with mock.patch.dict(os.environ, {"UNDERTONE_SAY_CMD": "say"}):
            argv = speaker.build_say_argv("Say hey Jarvis [[inpt PHON]]now[[inpt TEXT]], J.A.R.V.I.S.")
        self.assertIsNotNone(argv)
        text = argv[-1]
        self.assertEqual(argv[-2], "--")
        self.assertNotIn("[[", text)
        self.assertNotIn("]]", text)
        self.assertNotIn("jarvis", text.lower())
        self.assertIn("the wake word", text)


class ArgvInjectionTest(unittest.TestCase):
    def test_text_starting_with_dash_is_passed_after_double_dash(self) -> None:
        with mock.patch.dict(os.environ, {"UNDERTONE_SAY_CMD": "say-stub", "UNDERTONE_VOICE": ""}):
            argv = speaker.build_say_argv("-o/tmp/pwned")
        # "--" must appear immediately before the (otherwise flag-shaped) text,
        # and the text must arrive byte-for-byte, not be parseable as an option.
        self.assertEqual(argv, ["say-stub", "--", "-o/tmp/pwned"])


class NulByteTest(unittest.TestCase):
    def test_nul_byte_is_stripped(self) -> None:
        argv = speaker.build_say_argv("hel\x00lo")
        self.assertIsNotNone(argv)
        self.assertEqual(argv[-1], "hello")
        self.assertTrue(all("\x00" not in part for part in argv))

    def test_only_nul_bytes_yields_none(self) -> None:
        self.assertIsNone(speaker.build_say_argv("\x00\x00"))


class VoiceSelectionTest(unittest.TestCase):
    """Pure argv tests for build_say_argv -- `say` is never run."""

    def _argv(self, voice: str | None) -> list[str] | None:
        with mock.patch.dict(os.environ, {"UNDERTONE_SAY_CMD": "say-stub"}, clear=False):
            if voice is None:
                os.environ.pop("UNDERTONE_VOICE", None)
            else:
                os.environ["UNDERTONE_VOICE"] = voice
            return speaker.build_say_argv("hello")

    def test_default_voice_is_evan_enhanced(self) -> None:
        self.assertEqual(
            self._argv(None), ["say-stub", "-v", "Evan (Enhanced)", "--", "hello"]
        )

    def test_env_override_selects_voice(self) -> None:
        self.assertEqual(
            self._argv("Zoe (Premium)"), ["say-stub", "-v", "Zoe (Premium)", "--", "hello"]
        )

    def test_empty_voice_omits_v_flag(self) -> None:
        self.assertEqual(self._argv(""), ["say-stub", "--", "hello"])



if __name__ == "__main__":
    unittest.main()

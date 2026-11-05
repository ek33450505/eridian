"""Tests for speaker.espeak_phonemes, Kokoro's only text entry.

espeak-ng is replaced by a stub script (ERIDIAN_ESPEAK_CMD) that logs its argv
and raw stdin and prints one canned line per input line. One real-engine test is
skipped unless espeak-ng is installed.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from eridian import speaker

_STUB = """#!{python}
import json, os, sys, time
data = sys.stdin.read()
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": data}}) + "\\n")
if os.environ.get("STUB_SLEEP"):
    time.sleep(float(os.environ["STUB_SLEEP"]))
if os.environ.get("STUB_EXIT"):
    sys.exit(int(os.environ["STUB_EXIT"]))
n = len(data.split("\\n")) if data else 0
if os.environ.get("STUB_BAD_UTF8"):
    sys.stdout.buffer.write(b"\\xff\\xfebad\\n")
    sys.stdout.flush()
    sys.exit(0)
if os.environ.get("STUB_LINES"):
    n = int(os.environ["STUB_LINES"])
for i in range(n):
    print("p%d" % i)
"""


class StubEspeakTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stub = os.path.join(tmp.name, "espeak-stub")
        self.log = os.path.join(tmp.name, "log.jsonl")
        with open(self.stub, "w") as f:
            f.write(_STUB.format(python=sys.executable))
        os.chmod(self.stub, 0o755)
        self._env = {"ERIDIAN_ESPEAK_CMD": self.stub, "STUB_LOG": self.log}

    def _run(self, text: str, **extra: str) -> str | None:
        with mock.patch.dict(os.environ, {**self._env, **extra}):
            return speaker.espeak_phonemes(text)

    def _calls(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f if line.strip()]

    def _stdin(self) -> str:
        calls = self._calls()
        self.assertEqual(len(calls), 1)
        return calls[0]["stdin"]

    def test_wake_word_and_brackets_never_reach_espeak(self) -> None:
        self._run("Say hey Jarvis [[dZ'A@rvIs]] now, J.A.R.V.I.S.")
        stdin = self._stdin()
        self.assertNotIn("[", stdin)
        self.assertNotIn("]", stdin)
        self.assertNotIn("jarvis", stdin.lower())
        self.assertIn("the wake word", stdin)

    def test_argv_is_exact_and_text_only_on_stdin(self) -> None:
        self._run("-o/tmp/x")
        calls = self._calls()
        self.assertEqual(calls[0]["argv"], ["-q", "--ipa", "-v", "en-us"])
        self.assertEqual(calls[0]["stdin"], "-o/tmp/x")

    def test_nul_is_stripped(self) -> None:
        self._run("hel\x00lo")
        self.assertEqual(self._stdin(), "hello")

    def test_lone_surrogate_is_stripped(self) -> None:
        self.assertEqual(self._run("hello \ud800 world"), "p0")
        stdin = self._stdin()
        self.assertEqual(stdin, "hello  world".replace("  ", " "))

    def test_surrogate_cannot_split_wake_word(self) -> None:
        self._run("jar\ud800vis")
        stdin = self._stdin()
        self.assertIn("the wake word", stdin)
        self.assertNotIn("jarvis", stdin.lower())

    def test_only_surrogates_never_invokes_espeak(self) -> None:
        self.assertIsNone(self._run("\ud800\udfff"))
        self.assertEqual(self._calls(), [])

    def test_invalid_utf8_output_does_not_raise(self) -> None:
        out = self._run("hello", STUB_BAD_UTF8="1")
        self.assertIsInstance(out, str)

    def test_fullwidth_brackets_never_reach_espeak(self) -> None:
        self._run("\uff3b\uff3binpt PHON\uff3d\uff3dhi")
        stdin = self._stdin()
        for ch in "[]\uff3b\uff3d":
            self.assertNotIn(ch, stdin)

    def test_fullwidth_wake_word_never_reaches_espeak(self) -> None:
        self._run("hey \uff4a\uff41\uff52\uff56\uff49\uff53")
        stdin = self._stdin()
        self.assertIn("the wake word", stdin)
        self.assertNotIn("jarvis", stdin.casefold())

    def test_marks_are_restored(self) -> None:
        self.assertEqual(self._run("Sure, done."), "p0, p1.")
        self.assertEqual(self._stdin(), "Sure\ndone")

    def test_mid_token_dots_do_not_split(self) -> None:
        self._run("Is it 3.5 or e.g. 4?")
        self.assertEqual(self._stdin(), "Is it 3.5 or e.g\n4")

    def test_tail_without_punctuation_has_empty_mark(self) -> None:
        self.assertEqual(self._run("Well, okay"), "p0, p1")

    def test_embedded_newline_adds_no_line(self) -> None:
        self._run("one\ntwo\n\nthree")
        self.assertEqual(self._stdin(), "one two three")

    def test_line_count_mismatch_uses_last_mark(self) -> None:
        self.assertEqual(self._run("Sure, done.", STUB_LINES="3"), "p0 p1 p2.")

    def test_missing_command_gives_none(self) -> None:
        with mock.patch.dict(os.environ, {"ERIDIAN_ESPEAK_CMD": self.stub + "-missing"}):
            self.assertIsNone(speaker.espeak_phonemes("hello"))

    def test_nonzero_exit_gives_none(self) -> None:
        self.assertIsNone(self._run("hello", STUB_EXIT="1"))

    def test_timeout_gives_none(self) -> None:
        with mock.patch.object(speaker, "ESPEAK_TIMEOUT_S", 0.2):
            self.assertIsNone(self._run("hello", STUB_SLEEP="3"))

    def test_nothing_speakable_never_invokes_espeak(self) -> None:
        for text in ("[[]]", "   \n\t ", "", "\x00"):
            self.assertIsNone(self._run(text), repr(text))
        self.assertEqual(self._calls(), [])

    def test_empty_output_gives_none(self) -> None:
        self.assertIsNone(self._run("hello", STUB_LINES="0"))


@unittest.skipUnless(shutil.which("espeak-ng"), "espeak-ng not installed")
class RealEspeakTest(unittest.TestCase):
    def test_bracket_phoneme_smuggling_is_neutralized(self) -> None:
        with mock.patch.dict(os.environ, {"ERIDIAN_ESPEAK_CMD": "espeak-ng"}):
            out = speaker.espeak_phonemes("say [[dZ'A@rvIs]] now")
        self.assertIsNotNone(out)
        self.assertNotIn("dʒˈɑːɹ", out)


if __name__ == "__main__":
    unittest.main()

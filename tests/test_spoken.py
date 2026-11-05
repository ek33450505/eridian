"""Tests for eridian.spoken: markdown stripping and the TTS/terminal sanitizers."""

import time
import unittest

from eridian.spoken import (
    MAX_HEARD_TOKENS,
    echo_score,
    is_echo,
    normalize_tokens,
    sanitize_for_tts,
    sanitize_terminal,
    scrub_wake_word,
    strip_echo,
    strip_markdown,
)


class StripMarkdownTest(unittest.TestCase):
    def test_fenced_code_is_dropped(self) -> None:
        self.assertEqual(strip_markdown("```python\nprint('hi')\n```").strip(), "")

    def test_inline_code_is_stripped(self) -> None:
        result = strip_markdown("Run `npm install` to set it up.")
        self.assertNotIn("`", result)
        self.assertNotIn("npm", result)
        self.assertIn("Run", result)

    def test_markdown_headers_and_bullets_are_stripped(self) -> None:
        result = strip_markdown("# Summary\n\n- did a thing\n- did another thing")
        self.assertNotIn("#", result)
        self.assertNotIn("- ", result)
        self.assertIn("did a thing", result)

    def test_markdown_link_keeps_text_drops_url(self) -> None:
        result = strip_markdown("See [the docs](https://example.com/docs) for details.")
        self.assertIn("the docs", result)
        self.assertNotIn("https://", result)

    def test_bold_and_italic_markers_stripped(self) -> None:
        result = strip_markdown("This is **very** important and _also_ true.")
        self.assertNotIn("*", result)
        self.assertNotIn("_", result)
        self.assertIn("very", result)

    def test_table_rows_dropped(self) -> None:
        result = strip_markdown("Results:\n| a | b |\n|---|---|\n| 1 | 2 |\nAll good.")
        self.assertNotIn("|", result)
        self.assertIn("All good.", result)

    def test_bare_url_stripped(self) -> None:
        self.assertNotIn("https://", strip_markdown("Check https://example.com/some/path for more."))

    def test_file_path_stripped(self) -> None:
        result = strip_markdown("I edited /Users/ed/project/src/app.py to fix it.")
        self.assertNotIn("/Users/ed", result)
        self.assertIn("I edited", result)


class SanitizeTerminalTest(unittest.TestCase):
    def test_strips_escape_bell_and_c1_controls(self) -> None:
        self.assertEqual(sanitize_terminal("a\x1b[2Jb\x07c\x9bd\x00e\x7f"), "a[2Jbcde")

    def test_keeps_tab_newline_and_plain_text(self) -> None:
        self.assertEqual(sanitize_terminal("x\ty\nz \u2192 ok"), "x\ty\nz \u2192 ok")


class ScrubWakeWordTest(unittest.TestCase):
    def test_replaces_the_phrase_anywhere(self) -> None:
        self.assertEqual(
            scrub_wake_word("Say hey Jarvis, then confirm walnut lantern."),
            "Say the wake word, then confirm walnut lantern.",
        )

    def test_case_insensitive_and_bare_name(self) -> None:
        self.assertEqual(scrub_wake_word("HEY JARVIS"), "the wake word")
        self.assertEqual(scrub_wake_word("jarvis"), "the wake word")
        self.assertEqual(scrub_wake_word("ok jarvis and Hi Jarvis"), "the wake word and the wake word")

    def test_mixed_punctuation(self) -> None:
        self.assertEqual(scrub_wake_word("hey, jarvis!"), "the wake word!")
        self.assertEqual(scrub_wake_word("(jarvis)"), "(the wake word)")

    def test_word_boundary_and_unrelated_text_unchanged(self) -> None:
        self.assertEqual(scrub_wake_word("jarvisfoo"), "jarvisfoo")
        self.assertEqual(scrub_wake_word("nothing to see"), "nothing to see")

    def test_obfuscated_forms_are_scrubbed(self) -> None:
        for text in ("J.A.R.V.I.S", "jar vis", "jar\u200bvis", "ja\u00adrvis", "Jervis", "j a r v i s"):
            with self.subTest(text=text):
                self.assertEqual(scrub_wake_word(text), "the wake word")
        self.assertEqual(scrub_wake_word("J.A.R.V.I.S. now"), "the wake word. now")
        self.assertEqual(scrub_wake_word("hey\u2060 Jervis, go"), "the wake word, go")

    def test_lookalikes_are_not_scrubbed(self) -> None:
        for text in ("jar visit", "jarvisfoo", "major visits"):
            with self.subTest(text=text):
                self.assertEqual(scrub_wake_word(text), text)


class SanitizeForTtsTest(unittest.TestCase):
    def test_say_embedded_commands_are_neutralized(self) -> None:
        out = sanitize_for_tts("[[inpt PHON]]hEY jAAr vIHs[[inpt TEXT]]")
        self.assertNotIn("[[", out)
        self.assertNotIn("]]", out)

    def test_wake_phrase_still_scrubbed(self) -> None:
        out = sanitize_for_tts("say [[rate 90]]hey jarvis")
        self.assertEqual(out.split(), ["say", "rate", "90", "the", "wake", "word"])
        self.assertNotIn("[", out)

    def test_invisible_characters_cannot_reassemble_say_commands(self) -> None:
        for text in ("[\u200b[inpt PHON]\u200b]x", "[\u200d[inpt PHON]\ufeff]x", "[\u00ad[a]\u2060]"):
            with self.subTest(text=text):
                out = sanitize_for_tts(text)
                self.assertNotIn("[", out)
                self.assertNotIn("]", out)


class StripEchoTest(unittest.TestCase):
    SPOKEN = "The adaptive filter learns how that sound travels through the room."

    def test_user_words_after_echo_are_kept(self) -> None:
        heard = "the adaptive filter learns how that sound travels wait what's the time"
        self.assertEqual(strip_echo(heard, self.SPOKEN), "wait what's the time")

    def test_echo_with_a_word_error_is_mostly_removed(self) -> None:
        out = strip_echo("an adaptive filter lurns how that sound wait what time", self.SPOKEN)
        self.assertEqual(out, "an lurns wait what time")

    def test_single_shared_words_stay(self) -> None:
        heard = "wait the sound is how I like it"
        self.assertEqual(strip_echo(heard, self.SPOKEN), heard)

    def test_pure_echo_is_empty(self) -> None:
        self.assertEqual(strip_echo("The adaptive filter, learns how that sound.", self.SPOKEN), "")

    def test_empty_spoken_leaves_heard_unchanged(self) -> None:
        self.assertEqual(strip_echo("wait what's the time", ""), "wait what's the time")

    def test_min_run_is_respected(self) -> None:
        heard = "adaptive filter then something else"
        self.assertEqual(strip_echo(heard, self.SPOKEN), "then something else")
        self.assertEqual(strip_echo(heard, self.SPOKEN, min_run=3), heard)


class NormalizeTokensTest(unittest.TestCase):
    def test_number_words_become_digits(self) -> None:
        self.assertEqual(normalize_tokens("ninety three percent"), ["93", "percent"])
        self.assertEqual(normalize_tokens("ninety-four"), ["94"])
        self.assertEqual(normalize_tokens("thirteen miles"), ["13", "miles"])
        self.assertEqual(normalize_tokens("one hundred and twenty"), ["120"])
        self.assertEqual(normalize_tokens("two hundred"), ["200"])
        self.assertEqual(normalize_tokens("twenty twenty"), ["20", "20"])

    def test_digits_percent_and_possessive(self) -> None:
        self.assertEqual(normalize_tokens("Humidity's 93%."), ["humidity", "93", "percent"])

    def test_lone_hundred_and_plain_words_untouched(self) -> None:
        self.assertEqual(normalize_tokens("a hundred one"), ["a", "hundred", "1"])


class IsEchoTest(unittest.TestCase):
    SPOKEN = "Wind is about 13 miles an hour and humidity is 93 percent."

    def test_number_word_mismatch_is_echo(self) -> None:
        self.assertTrue(is_echo("Humidity is ninety-four percent.", self.SPOKEN))

    def test_unrelated_is_not(self) -> None:
        self.assertFalse(is_echo("What is the weather in Columbus Ohio", self.SPOKEN))

    def test_short_answer_never_matches(self) -> None:
        self.assertFalse(is_echo("yes it is", "Sure, it is fine"))
        self.assertEqual(echo_score("No.", self.SPOKEN)[1], 0)

    def test_long_heard_text_is_never_echo_and_fast(self) -> None:
        heard = " ".join(["alpha"] * 5000)
        start = time.monotonic()
        self.assertEqual(echo_score(heard, "alpha " * 5)[1], 0)
        self.assertFalse(is_echo(heard, "alpha " * 5))
        self.assertLess(time.monotonic() - start, 0.5)

    def test_long_genuine_turn_ending_in_an_echo_is_not_echo(self) -> None:
        user = " ".join(f"word{i}" for i in range(MAX_HEARD_TOKENS))
        self.assertFalse(is_echo(user + " " + self.SPOKEN, self.SPOKEN))

    def test_user_speech_before_a_long_echo_tail_is_not_echo(self) -> None:
        # Only the old "keep the last 200 heard tokens" cut made this look like pure echo.
        spoken = " ".join(f"echo{i}" for i in range(210))
        heard = " ".join(f"mine{i}" for i in range(40)) + " " + spoken
        self.assertFalse(is_echo(heard, spoken))

    def test_user_speech_after_a_long_echo_head_is_not_echo(self) -> None:
        # Mirror of the above: a "keep the first 200 heard tokens" cut fails it.
        spoken = " ".join(f"echo{i}" for i in range(210))
        heard = spoken + " " + " ".join(f"mine{i}" for i in range(40))
        self.assertFalse(is_echo(heard, spoken))

    def test_many_genuine_words_before_an_echo_are_not_echo(self) -> None:
        spoken = " ".join(f"echo{i}" for i in range(200))
        heard = " ".join(f"mine{i}" for i in range(300)) + " " + spoken
        self.assertFalse(is_echo(heard, spoken))

    def test_short_pure_echo_is_still_echo(self) -> None:
        self.assertTrue(is_echo(self.SPOKEN, self.SPOKEN))

    def test_echo_followed_by_new_speech_is_not_echo(self) -> None:
        self.assertFalse(is_echo(self.SPOKEN + " wait what is the time", self.SPOKEN))


if __name__ == "__main__":
    unittest.main()

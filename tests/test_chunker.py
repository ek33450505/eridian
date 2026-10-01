"""Behavioral tests for undertone.chunker.SentenceChunker (pure, no I/O)."""

import unittest

from undertone.chunker import CODE_PLACEHOLDER, SentenceChunker


def feed_all(chunker: SentenceChunker, deltas: list[str]) -> list[str]:
    out: list[str] = []
    for d in deltas:
        out += chunker.feed(d)
    return out


class SentenceChunkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.c = SentenceChunker()

    def test_fragmented_deltas_wait_for_trailing_whitespace(self) -> None:
        self.assertEqual(self.c.feed("Hel"), [])
        self.assertEqual(self.c.feed("lo."), [])  # whitespace not arrived yet
        self.assertEqual(self.c.feed(" Wor"), ["Hello."])
        self.assertEqual(self.c.feed("ld"), [])
        self.assertEqual(self.c.flush(), ["World"])

    def test_multiple_sentences_in_one_delta(self) -> None:
        self.assertEqual(self.c.feed("One. Two! Three? Fo"), ["One.", "Two!", "Three?"])
        self.assertEqual(self.c.flush(), ["Fo"])

    def test_bullets_split_on_newline(self) -> None:
        out = self.c.feed("- one\n- two\n")
        self.assertEqual(out, ["one", "two"])

    def test_numbered_list(self) -> None:
        self.assertEqual(self.c.feed("1. First item\n2. Second\n"), ["First item", "Second"])

    def test_decimals_do_not_split(self) -> None:
        self.assertEqual(self.c.feed("It costs 3.5 dollars. Next"), ["It costs 3.5 dollars."])
        self.assertEqual(self.c.feed("3"), [])
        self.assertEqual(self.c.feed(".5 more. "), ["Next3.5 more."])

    def test_abbreviations_do_not_split(self) -> None:
        text = "Use e.g. tools, i.e. helpers, etc. today vs. later. Ask Dr. Smith and Mrs. Jones. Done. "
        self.assertEqual(
            self.c.feed(text),
            [
                "Use e.g. tools, i.e. helpers, etc. today vs. later.",
                "Ask Dr. Smith and Mrs. Jones.",
                "Done.",
            ],
        )

    def test_abbreviation_is_case_insensitive(self) -> None:
        self.assertEqual(self.c.feed("Hello MR. Smith. "), ["Hello MR. Smith."])

    def test_closing_quotes_and_parens(self) -> None:
        self.assertEqual(
            self.c.feed('He said "stop." Then (it ended.) Fine”. '),
            ['He said "stop."', "Then (it ended.)", "Fine”."],
        )

    def test_code_fence_split_across_deltas(self) -> None:
        out = feed_all(self.c, ["Look here.\n", "``", "`py", "thon\nx = 1\n", "print(x)\n``", "`\n", "After. "])
        self.assertEqual(out, ["Look here.", CODE_PLACEHOLDER, "After."])

    def test_code_never_emitted_and_text_resumes(self) -> None:
        out = self.c.feed("Intro.\n```\nfoo. bar.\n```\nOutro here.\n")
        self.assertEqual(out, ["Intro.", CODE_PLACEHOLDER, "Outro here."])

    def test_indented_fence(self) -> None:
        out = self.c.feed("   ```\nsecret.\n   ```\nBack.\n")
        self.assertEqual(out, [CODE_PLACEHOLDER, "Back."])

    def test_two_code_blocks_one_placeholder(self) -> None:
        out = self.c.feed("```\na\n```\nMid.\n```\nb\n```\nEnd.\n")
        self.assertEqual(out, [CODE_PLACEHOLDER, "Mid.", "End."])

    def test_reset_rearms_placeholder(self) -> None:
        self.assertEqual(self.c.feed("```\na\n```\n"), [CODE_PLACEHOLDER])
        self.c.reset()
        self.assertEqual(self.c.feed("```\na\n```\n"), [CODE_PLACEHOLDER])

    def test_unterminated_fence_dropped_on_flush(self) -> None:
        self.assertEqual(self.c.feed("```\nnever closed"), [CODE_PLACEHOLDER])
        self.assertEqual(self.c.flush(), [])

    def test_markdown_cleaned(self) -> None:
        out = self.c.feed("Run `ls -la` and see [the docs](http://x.io) for **bold** news. ")
        self.assertEqual(out, ["Run and see the docs for bold news."])

    def test_no_letters_dropped(self) -> None:
        self.assertEqual(self.c.feed("---\n1.\n42\nReal text.\n"), ["Real text."])

    def test_table_rows_dropped(self) -> None:
        out = self.c.feed("| a | b. c |\n|---|---|\n| 1 | 2 |\nAfter table.\n")
        self.assertEqual(out, ["After table."])

    def test_soft_cap_cuts_long_run_on(self) -> None:
        words = " ".join(["word"] * 70)  # 349 chars, no boundary
        out = self.c.feed(words)
        self.assertEqual(len(out), 1)
        self.assertGreaterEqual(len(out[0]), 120)
        self.assertLessEqual(len(out[0]), 240)
        self.assertEqual(" ".join(out + self.c.flush()), words)

    def test_soft_cap_prefers_comma(self) -> None:
        text = "a" * 130 + ", " + "b" * 130
        out = self.c.feed(text)
        self.assertEqual(out, ["a" * 130 + ","])

    def test_flush_emits_tail(self) -> None:
        self.c.feed("Done. And a tail")
        self.assertEqual(self.c.flush(), ["And a tail"])

    def test_flush_on_empty(self) -> None:
        self.assertEqual(self.c.flush(), [])

    def test_first_sentence_emitted_immediately(self) -> None:
        self.assertEqual(self.c.feed("Hi. "), ["Hi."])


if __name__ == "__main__":
    unittest.main()

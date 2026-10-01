"""Text hygiene for speech and the terminal: markdown stripping and sanitizers.

Pure, stdlib-only, no I/O — easy to test in isolation (see tests/test_spoken.py).
"""

from __future__ import annotations

import difflib
import re

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MD_BOLD_ITALIC_RE = re.compile(r"(\*{1,3}|_{1,3})(.+?)\1")
_MD_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_MD_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_URL_RE = re.compile(r"https?://\S+")
_FILE_PATH_RE = re.compile(r"(?<!\w)(?:~|\.{1,2})?/[\w./\-]+")
# C0/C1 control characters except \t and \n: model text, tool names and error
# strings are untrusted, and a raw ESC/BEL would let them drive the terminal
# (clear screen, OSC 52 clipboard writes, ...).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


# Letter-spaced / punctuated "jarvis" ("J.A.R.V.I.S.", "jar vis"), plus the
# common mishearing "jervis", with the optional hey/hi/okay lead-in. Matched
# anywhere (unlike listener._WAKE_PHRASE_RE). Alphanumeric look-arounds rather
# than \b so "jarvisfoo" and "jar visit" are left alone while separators
# between the letters are free. A false positive only costs a substitution.
_SEP = r"[\W_]*"
_WAKE_WORD_RE = re.compile(
    r"(?<![^\W_])(?:(?:hey|hi|okay|ok)[\W_]+)?"
    + _SEP.join(["j", "[ae]", "r", "v", "[iy]", "s"])
    + r"(?![^\W_])",
    re.IGNORECASE,
)
# Zero-width characters and the soft hyphen: invisible to a reader, but they
# break up a regex match while `say` still pronounces the word.
_INVISIBLE_RE = re.compile("[\u200b-\u200d\u2060\ufeff\u00ad]")
# `say` honors inline [[ ... ]] commands ([[inpt PHON]] spells a word out in
# phonemes with no letters for the regex to catch), so neutralize the markers.
# Every bracket goes, not just pairs: brackets are never needed for speech.
_SAY_CMD_RE = re.compile(r"[\[\]]")


def scrub_wake_word(text: str) -> str:
    """Replace any spoken form of the wake phrase with "the wake word"."""
    return _WAKE_WORD_RE.sub("the wake word", _INVISIBLE_RE.sub("", text))


def sanitize_for_tts(text: str) -> str:
    """Everything undertone must not say aloud: `say` embedded commands (all brackets) and the wake phrase."""
    # Order matters: strip invisible characters FIRST, or "[<ZWSP>[inpt PHON]"
    # survives the bracket step and is reassembled into a live command after.
    text = _INVISIBLE_RE.sub("", text)
    return scrub_wake_word(_SAY_CMD_RE.sub(" ", text))


def sanitize_terminal(text: str) -> str:
    """Strip terminal control characters (keeps \\t and \\n) from untrusted text."""
    return _CONTROL_RE.sub("", text)


def strip_markdown(text: str) -> str:
    """Drop fenced code, inline code, and common markdown punctuation/noise."""
    text = _FENCED_CODE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _MD_TABLE_ROW_RE.sub(" ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_HEADER_RE.sub("", text)
    text = _MD_BULLET_RE.sub("", text)
    text = _MD_BOLD_ITALIC_RE.sub(r"\2", text)
    text = _URL_RE.sub(" ", text)
    text = _FILE_PATH_RE.sub(" ", text)
    return text


_WORD_RE = re.compile(r"[a-z0-9]+")


def looks_like_echo(
    heard: str, spoken: str, *, threshold: float = 0.6, min_words: int = 3
) -> bool:
    """True when `heard` (an STT transcript) is mostly our own `spoken` reply.

    Guards talk-over against imperfect echo cancellation: the fraction of
    `heard` words that line up (in order) with `spoken` must reach `threshold`.
    Short transcripts are never treated as echo -- "stop" is a real interruption.
    """
    heard_words = _WORD_RE.findall(heard.lower())
    spoken_words = _WORD_RE.findall(spoken.lower())
    if len(heard_words) < min_words or not spoken_words:
        return False
    blocks = difflib.SequenceMatcher(
        None, heard_words, spoken_words, autojunk=False
    ).get_matching_blocks()
    return sum(b.size for b in blocks) / len(heard_words) >= threshold

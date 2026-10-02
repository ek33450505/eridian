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


def strip_echo(heard: str, spoken: str, *, min_run: int = 2) -> str:
    """Remove the words of `heard` that repeat our own `spoken` reply.

    A talk-over transcript can start with residual echo of the reply (the
    pre-roll and detection delay overlap the speech), followed by what the user
    actually said. Heard words inside a run of at least `min_run` consecutive
    words that also appear, in order, in `spoken` are dropped; isolated common
    words ("the") stay. Returns the remaining words joined by single spaces,
    minus leading/trailing punctuation-only tokens.
    """
    words = heard.split()
    if not _WORD_RE.search(spoken.lower()):
        return heard
    flat: list[str] = []
    owner: list[int] = []
    for i, word in enumerate(words):
        for token in _WORD_RE.findall(word.lower()):
            flat.append(token)
            owner.append(i)
    spoken_tokens = _WORD_RE.findall(spoken.lower())
    matched = [False] * len(flat)
    blocks = difflib.SequenceMatcher(None, flat, spoken_tokens, autojunk=False)
    for block in blocks.get_matching_blocks():
        if block.size >= min_run:
            for k in range(block.a, block.a + block.size):
                matched[k] = True
    removed = {
        i
        for i in set(owner)
        if all(m for m, o in zip(matched, owner) if o == i)
    }
    kept = [w for i, w in enumerate(words) if i not in removed]
    while kept and not _WORD_RE.search(kept[0].lower()):
        kept.pop(0)
    while kept and not _WORD_RE.search(kept[-1].lower()):
        kept.pop()
    return " ".join(kept)

"""Turn a Claude Code reply into a short, speakable summary.

Pure, stdlib-only, no I/O — easy to test in isolation (see tests/test_spoken.py).
"""

from __future__ import annotations

import re

_SPOKEN_LINE_RE = re.compile(r"^\s*SPOKEN:\s*(.+?)\s*$", re.IGNORECASE)
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MD_BOLD_ITALIC_RE = re.compile(r"(\*{1,3}|_{1,3})(.+?)\1")
_MD_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_MD_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_URL_RE = re.compile(r"https?://\S+")
_FILE_PATH_RE = re.compile(r"(?<!\w)(?:~|\.{1,2})?/[\w./\-]+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE_RE = re.compile(r"\s+")
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


def _extract_spoken_line(reply: str) -> str | None:
    """Return the text of the last `SPOKEN: ...` line, or None if there isn't one.

    Fenced code blocks are stripped FIRST, so a `SPOKEN:`-looking line quoted
    inside one (e.g. pasted file content, or a prompt-injection attempt) is
    never mistaken for the real spoken-summary line.
    """
    code_free = _FENCED_CODE_RE.sub(" ", reply)
    match = None
    for line in code_free.splitlines():
        candidate = _SPOKEN_LINE_RE.match(line)
        if candidate:
            match = candidate
    return match.group(1) if match else None


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


def _first_n_sentences(text: str, n: int) -> str:
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s]
    return " ".join(sentences[:n])


def _cap_at_word_boundary(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.rstrip()


def spoken_text(reply: str, max_chars: int = 300) -> str:
    """Reduce a full Claude reply to a short line suitable for TTS.

    If `reply` contains one or more `SPOKEN: ...` lines (case-insensitive),
    the last one's text is used verbatim (still capped at max_chars). Otherwise
    the reply is stripped of code/markdown/URLs/paths, collapsed to whitespace,
    and the first two sentences are taken, capped at max_chars on a word
    boundary. Returns "" if nothing speakable remains.
    """
    spoken_line = _extract_spoken_line(reply)
    if spoken_line is not None:
        collapsed = _WHITESPACE_RE.sub(" ", spoken_line).strip()
        return _cap_at_word_boundary(collapsed, max_chars)

    stripped = strip_markdown(reply)
    collapsed = _WHITESPACE_RE.sub(" ", stripped).strip()
    if not collapsed:
        return ""
    excerpt = _first_n_sentences(collapsed, 2)
    return _cap_at_word_boundary(excerpt, max_chars)

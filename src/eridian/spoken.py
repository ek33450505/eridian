"""Text hygiene for speech and the terminal: markdown stripping and sanitizers.

Pure, stdlib-only, no I/O — easy to test in isolation (see tests/test_spoken.py).
"""

from __future__ import annotations

import difflib
import re

from . import wake

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


# Zero-width characters and the soft hyphen: invisible to a reader, but they
# break up a regex match while `say` still pronounces the word.
_INVISIBLE_RE = re.compile("[\u200b-\u200d\u2060\ufeff\u00ad]")
# `say` honors inline [[ ... ]] commands ([[inpt PHON]] spells a word out in
# phonemes with no letters for the regex to catch), so neutralize the markers.
# Every bracket goes, not just pairs: brackets are never needed for speech.
_SAY_CMD_RE = re.compile(r"[\[\]]")


def scrub_wake_word(text: str) -> str:
    """Replace any spoken form of the wake phrase with "the wake word"."""
    # The pattern comes from the configured wake word (see wake.scrub_pattern);
    # it matches the phrase anywhere, letter-spaced and with common homophones.
    return wake.scrub_pattern(wake.current()).sub("the wake word", _INVISIBLE_RE.sub("", text))


def sanitize_for_tts(text: str) -> str:
    """Everything eridian must not say aloud: `say` embedded commands (all brackets) and the wake phrase."""
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


_UNITS = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
    )
}
_TENS = {
    w: 10 * (i + 2)
    for i, w in enumerate(
        "twenty thirty forty fifty sixty seventy eighty ninety".split()
    )
}


def normalize_tokens(text: str) -> list[str]:
    """Lowercase word tokens with spoken numbers as digits, for echo matching.

    "Humidity's ninety-three percent" and "humidity is 93%" both come out with
    a "93" token: possessive 's is dropped, "%" becomes "percent", and runs of
    number words up to 999 ("ninety three", "one hundred twenty") collapse to
    one digit token. Anything else is kept as a plain word.
    """
    text = re.sub(r"['’]s\b", "", text.lower().replace("%", " percent "))
    text = text.replace("'", "").replace("’", "").replace("-", " ")
    raw = _WORD_RE.findall(text)
    out: list[str] = []
    i = 0
    while i < len(raw):
        value, j = _read_number(raw, i)
        if j > i:
            out.append(str(value))
            i = j
        else:
            out.append(raw[i])
            i += 1
    return out


def _read_number(tokens: list[str], i: int) -> tuple[int, int]:
    """Parse a spoken number (< 1000) at tokens[i]; return (value, end) or (0, i)."""
    j, total, hundreds = i, 0, 0
    if tokens[j] in _UNITS and j + 1 < len(tokens) and tokens[j + 1] == "hundred":
        hundreds, j = _UNITS[tokens[j]] * 100, j + 2
        if j < len(tokens) and tokens[j] == "and":
            j += 1
    elif tokens[j] == "hundred":
        return 0, i
    start = j
    if j < len(tokens) and tokens[j] in _TENS:
        total, j = _TENS[tokens[j]], j + 1
        if j < len(tokens) and 1 <= _UNITS.get(tokens[j], 0) <= 9:
            total, j = total + _UNITS[tokens[j]], j + 1
    elif j < len(tokens) and tokens[j] in _UNITS:
        total, j = _UNITS[tokens[j]], j + 1
    if j == start and not hundreds:
        return 0, i
    return hundreds + total, j


# Turns over 60 tokens are never treated as echo. Trade-off: a talk-over or
# follow-up turn lasts at most 15 s (ListenerConfig.max_utterance_s), so a
# continuous leak of fast speech (~4 words/s) can reach ~60 tokens and pass
# through as a turn; we accept that, because truncating instead drops genuine
# long turns. It also bounds the LCS table (60 x 800) on the main thread.
MAX_HEARD_TOKENS = 60


def echo_score(heard: str, spoken: str) -> tuple[float, int, int]:
    """How much of `heard` is our own `spoken` text.

    Returns (coverage, matched tokens, longest run of unmatched heard tokens).
    Matching is the longest common subsequence of normalised tokens (not a
    contiguous run), so it survives the STT dropping or swapping a word of
    leaked speech. Coverage is matched / heard tokens. The unmatched run tells
    a swapped word ("94" for "93", run 1) from real speech appended to an echo
    (a whole new phrase, run 3+). More than MAX_HEARD_TOKENS heard tokens
    scores as no match at all.
    """
    h = normalize_tokens(heard)
    s = normalize_tokens(spoken)[-800:]
    if not h or not s or len(h) > MAX_HEARD_TOKENS:
        return 0.0, 0, len(h)
    table = [[0] * (len(s) + 1) for _ in range(len(h) + 1)]
    for i, a in enumerate(h, 1):
        for k, b in enumerate(s, 1):
            table[i][k] = (
                table[i - 1][k - 1] + 1 if a == b else max(table[i - 1][k], table[i][k - 1])
            )
    matched = [False] * len(h)
    i, k = len(h), len(s)
    while i and k:
        if h[i - 1] == s[k - 1]:
            matched[i - 1] = True
            i, k = i - 1, k - 1
        elif table[i - 1][k] >= table[i][k - 1]:
            i -= 1
        else:
            k -= 1
    run = longest = 0
    for m in matched:
        run = 0 if m else run + 1
        longest = max(longest, run)
    total = sum(matched)
    return total / len(h), total, longest


def is_echo(
    heard: str,
    spoken: str,
    *,
    min_coverage: float = 0.6,
    min_tokens: int = 3,
    max_novel_run: int = 2,
) -> bool:
    """True when `heard` is, with high probability, our own speech picked up again.

    Needs enough coverage, at least `min_tokens` matched tokens (a short real
    answer like "yes it is" never matches on one or two common words), and no
    run of more than `max_novel_run` unmatched words (that is the user talking
    after or over the echo, which `strip_echo` separates instead).
    """
    coverage, matched, novel = echo_score(heard, spoken)
    return matched >= min_tokens and coverage >= min_coverage and novel <= max_novel_run

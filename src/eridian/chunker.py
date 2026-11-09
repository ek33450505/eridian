"""Split a stream of text deltas into speakable, sentence-sized chunks.

Pure, stdlib-only, no I/O (see tests/test_chunker.py). The conversation loop streams
Claude's reply as arbitrary text fragments; SentenceChunker buffers them and
emits each sentence the moment its trailing whitespace arrives, so the first
sentence can be spoken while the rest is still generating.

Rules, in short:
  * A boundary is `.`/`!`/`?` (plus optional closing quotes/parens) followed by
    whitespace, or a newline. The whitespace must have ARRIVED -- "3" then
    ".5" must never split, and neither must "e.g." before its space is seen.
  * Fenced code (a line starting with ```) is dropped; the first fence of a
    turn emits CODE_PLACEHOLDER so the listener knows code was skipped.
  * Markdown table rows are dropped; every other chunk goes through
    spoken.strip_markdown, and chunks with no letters ("1.", "---") vanish.
  * Soft cap: a long run-on is cut at a comma/space so speech starts sooner.
"""

from __future__ import annotations

import re

from .spoken import strip_markdown

CODE_PLACEHOLDER = "Code's on screen."

_SOFT_CAP = 240
_SOFT_CAP_MIN = 120
_FENCE = "```"

# Punctuation + optional closers, then the whitespace that confirms the boundary;
# or a bare newline.
_BOUNDARY_RE = re.compile(r"[.!?][\"')”’]*\s|\n")
_ABBREV_RE = re.compile(r"(?<![\w.])(?:e\.g|i\.e|etc|vs|mr|mrs|ms|dr|st)\.$", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_HAS_LETTER_RE = re.compile(r"[^\W\d_]")


def _clean(chunk: str) -> str:
    cleaned = _WHITESPACE_RE.sub(" ", strip_markdown(chunk)).strip()
    return cleaned if _HAS_LETTER_RE.search(cleaned) else ""


class SentenceChunker:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Start a new turn: clear the buffer and re-arm the code placeholder."""
        self._buf = ""
        self._in_code = False
        self._line_start = True  # does the buffer begin at the start of a line?
        self._placeholder_sent = False

    def feed(self, delta: str) -> list[str]:
        """Add a delta; return zero or more complete, cleaned chunks."""
        self._buf += delta
        return self._drain(final=False)

    def flush(self) -> list[str]:
        """Emit whatever is buffered (end of turn, or before a tool call)."""
        out = self._drain(final=True)
        # An unterminated fence is dropped silently; the next text starts fresh.
        self._buf = ""
        self._in_code = False
        self._line_start = True
        return out

    def _drain(self, final: bool) -> list[str]:
        out: list[str] = []

        def emit(raw: str) -> None:
            cleaned = _clean(raw)
            if cleaned:
                out.append(cleaned)

        while self._buf:
            if self._in_code:
                # Only whole lines can close a fence, so inspect line by line.
                nl = self._buf.find("\n")
                if nl < 0:
                    if not final:
                        # Keep just the partial line; the rest of the code is dropped.
                        break
                    self._buf = ""
                    break
                line, self._buf = self._buf[:nl], self._buf[nl + 1:]
                if line.lstrip().startswith(_FENCE):
                    self._in_code = False
                    self._line_start = True
                continue

            if self._line_start:
                stripped = self._buf.lstrip(" \t")
                has_nl = "\n" in self._buf
                if not has_nl and not final and (stripped == "" or _FENCE.startswith(stripped[:3])):
                    break  # can't yet tell whether this line is a fence
                if stripped.startswith(_FENCE) or stripped.startswith("|"):
                    # A fence or table row is decided as a whole line.
                    nl = self._buf.find("\n")
                    if nl < 0:
                        if not final:
                            break
                        self._buf = ""
                        break
                    self._buf = self._buf[nl + 1:]
                    if stripped.startswith(_FENCE):
                        self._in_code = True
                        if not self._placeholder_sent:
                            self._placeholder_sent = True
                            out.append(CODE_PLACEHOLDER)
                    continue

            cut = self._find_boundary()
            if cut is not None:
                end, consumed, at_line_start = cut
                chunk, self._buf = self._buf[:end], self._buf[consumed:]
                self._line_start = at_line_start
                emit(chunk)
                continue

            if len(self._buf) > _SOFT_CAP:
                comma = self._buf.rfind(", ", _SOFT_CAP_MIN, _SOFT_CAP)
                space = self._buf.rfind(" ", _SOFT_CAP_MIN, _SOFT_CAP)
                end = max(comma + 1 if comma >= 0 else -1, space)
                if end > 0:
                    chunk, self._buf = self._buf[:end], self._buf[end:].lstrip(" ")
                    self._line_start = False
                    emit(chunk)
                    continue

            if final:
                emit(self._buf)
                self._buf = ""
            break

        return out

    def _find_boundary(self) -> tuple[int, int, bool] | None:
        """Return (chunk_end, consumed_upto, next_is_line_start) for the first real boundary."""
        for m in _BOUNDARY_RE.finditer(self._buf):
            text = m.group()
            if text == "\n":
                return m.start(), m.end(), True
            if text[0] == "." and text[1:-1] == "" and _ABBREV_RE.search(self._buf[: m.start() + 1]):
                continue
            punct_end = m.end() - 1  # exclude the confirming whitespace
            return punct_end, m.end(), text[-1] == "\n"
        return None

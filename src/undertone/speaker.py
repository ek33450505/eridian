"""The `say` argv builder: the single, security-critical path to the TTS command.

Everything undertone speaks goes through build_say_argv (via player.SpeechQueue).
The command comes from UNDERTONE_SAY_CMD (default "say"); tests MUST override
this to a stub that never produces real audio.
"""

from __future__ import annotations

import os

from .spoken import sanitize_for_tts


def build_say_argv(text: str) -> list[str] | None:
    """NUL-strip and sanitize_for_tts text; return None if empty; else [say_cmd, (-v voice), "--", text]."""
    # subprocess.Popen raises ValueError on an embedded NUL byte -- strip it
    # rather than letting one malformed reply break the speech worker.
    text = text.replace("\x00", "")
    # Hard control, not model compliance: the wake detector stays live while
    # we speak and there is no echo cancellation yet, so TTS saying the wake
    # phrase could self-trigger it (cut off speech, or capture the tail as a
    # command and confirm its own action). This is the single choke point for
    # all undertone speech (SpeechQueue goes through here).
    text = sanitize_for_tts(text)
    if not text:
        return None
    say_cmd = os.environ.get("UNDERTONE_SAY_CMD", "say")
    voice = os.environ.get("UNDERTONE_VOICE", "Evan (Enhanced)")
    cmd = [say_cmd]
    if voice:
        cmd += ["-v", voice]
    # "--" stops `say` (and any well-behaved stub) from parsing `text` as
    # flags -- without it, a reply containing e.g. "-o/path/to/overwrite"
    # is argv flag injection into the TTS command. This is the ONE place
    # that builds the say argv; player.py's SpeechQueue uses it, and cli.py
    # never shells out itself.
    cmd += ["--", text]
    return cmd

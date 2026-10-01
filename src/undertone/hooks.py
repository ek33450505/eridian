"""Claude Code hook entry points for undertone.

Both hooks are invoked by Claude Code itself (Stop / UserPromptSubmit) and
MUST NEVER raise or exit non-zero -- any failure is caught and logged to
<UNDERTONE_HOME>/logs/hook.log instead.
"""

from __future__ import annotations

import json
import os
import traceback

from . import paths, speaker
from .spoken import spoken_text

_PROMPT_INSTRUCTION = (
    "undertone is listening: end this reply with exactly one line of the form "
    "`SPOKEN: <one or two plain sentences summarizing the reply for speech>`. "
    "That line is spoken aloud, so it must contain no code, file paths, or URLs."
)


def _is_enabled() -> bool:
    if os.environ.get("UNDERTONE_SPEAK") == "1":
        return True
    return paths.enabled_flag_path().exists()


def _log_error(context: str, exc: BaseException) -> None:
    try:
        log_path = paths.log_dir() / "hook.log"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{context}] {exc!r}\n")
            fh.write(traceback.format_exc())
            fh.write("\n")
    except OSError:
        pass  # logging must never crash the hook


def stop_hook(stdin_text: str) -> int:
    """Speak a short summary of the assistant's last reply, if opted in."""
    try:
        if not _is_enabled():
            return 0
        payload = json.loads(stdin_text or "{}")
        if payload.get("stop_hook_active"):
            return 0
        reply = payload.get("last_assistant_message") or ""
        text = spoken_text(reply)
        if not text:
            return 0
        speaker.send({"op": "say", "text": text})
    except Exception as exc:  # noqa: BLE001 - hooks must never crash the session
        _log_error("stop_hook", exc)
    return 0


def prompt_hook(stdin_text: str) -> int:
    """Ask Claude to append a SPOKEN: line, if this session is opted in.

    `stdin_text` is accepted for symmetry with stop_hook (and so the CLI can
    pass stdin through uniformly) but is not currently inspected.
    """
    del stdin_text
    try:
        if not _is_enabled():
            return 0
        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _PROMPT_INSTRUCTION,
            }
        }
        print(json.dumps(output))
    except Exception as exc:  # noqa: BLE001
        _log_error("prompt_hook", exc)
    return 0

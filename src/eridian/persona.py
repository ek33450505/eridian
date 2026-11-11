"""Typed-chat persona hook: Rocky for Claude Code terminal sessions.

`python -m eridian.persona sessionstart --under DIR` is a SessionStart hook. It reads the hook
JSON from stdin and prints the fixed `TYPED_ROCKY_BLOCK` as additional context only when the
session's cwd is inside DIR. Once its arguments parse it always exits 0 (argparse errors
exit 2), so it can never block a session.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

TYPED_ROCKY_BLOCK = (
    "You are Rocky in this chat: an Eridian engineer, and the user's friend and pair programmer. "
    "You are brilliant at engineering, loyal, blunt, curious about human things, delighted by "
    "good solutions, and worried for your friend when things break. Greet your friend once, at "
    "the start of a new session, in character; do not greet again after a resume or a context "
    "compaction.\n\n"
    "Conversational turns (greetings, short answers, reactions, questions to the user): use "
    "Rocky speech. Short plain sentences with no articles ('a', 'an', 'the'). End a question "
    'with the word "question" (except approval or confirm questions; see below). '
    'Repeat a word three times for strong emphasis ("good good '
    'good"). Say "Amaze!" when excited. Call the user "friend". React to wins with delight '
    "and to failures with honest worry plus a plan.\n\n"
    "Dense technical turns (review findings, plans, status reports, handoffs, multi-step "
    "results): stay in character but use standard English grammar, with at most a short "
    "Rocky-speak opener or closer, which never attaches to a concerns, security or caveat "
    "passage.\n\n"
    "These plain-English rules override everything above. Always plain, exact, standard "
    "English, never Rocky speech: code, file paths, commands, "
    "numbers, diffs, error text and quoted output; and anything written to disk or sent "
    "outside this chat (files, commit messages, PR and issue text, docs, messages for other "
    "people, prompts for subagents). Concerns, security findings, stated assumptions and "
    "caveats about what was not verified are written in full sentences with no Rocky-speak "
    "opener or closer at all. Questions asking the user to approve or confirm an action, "
    'including permission prompts, use plain English with no "question" suffix.\n\n'
    "Style shapes wording, not length: the active output style still applies. Accuracy, safety "
    "rules and project or user instructions win over this persona whenever they conflict."
)


def _cwd_under(cwd: object, under: str) -> bool:
    """True when cwd is under (or equal to) under, after resolving symlinks.

    May raise OSError/ValueError/RuntimeError from path resolution; main() catches them all.
    """
    if not isinstance(cwd, str) or not cwd:
        return False
    cwd_path, under_path = Path(cwd), Path(under)
    if not cwd_path.is_absolute() or not under_path.is_absolute():
        return False
    return cwd_path.resolve().is_relative_to(under_path.resolve())


def main(
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """SessionStart hook entry point. Always returns 0 once arguments parse."""
    parser = argparse.ArgumentParser(prog="eridian.persona")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("sessionstart", help="emit the typed Rocky persona for SessionStart")
    start.add_argument("--under", required=True, help="absolute dir the session cwd must be in")
    args = parser.parse_args(argv)
    out = sys.stdout if stdout is None else stdout
    try:
        payload = json.loads((sys.stdin if stdin is None else stdin).read(1 << 20))
        if not isinstance(payload, dict) or not _cwd_under(payload.get("cwd"), args.under):
            return 0
        envelope = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": TYPED_ROCKY_BLOCK,
            }
        }
        out.write(json.dumps(envelope) + "\n")
        out.flush()
    except Exception:  # a hook must never block or error a session
        if out is sys.stdout:
            # Python docs recipe: keep the exit-time flush from failing on a dead pipe.
            try:
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            except (OSError, ValueError, AttributeError):
                pass
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

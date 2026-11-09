"""Opt-in speech persona for spoken replies, plus the typed-chat persona hook.

`ERIDIAN_PERSONA` unset, empty or "off" means no persona; "rocky" appends a
style block to the channel's instructions. Anything else is an error, so a typo
fails closed at startup instead of silently dropping the persona.
`eridian session` resolves it and hands it to the channel in the MCP env.

The typed chat gets its own persona, `TYPED_ROCKY_BLOCK`, from a SessionStart hook:
`python -m eridian.persona sessionstart --under DIR` prints the hook JSON only when the
session's cwd is inside DIR. Once its arguments parse it always exits 0 (argparse errors
exit 2), so it can never block a session.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

PERSONA_ENV = "ERIDIAN_PERSONA"
ROCKY = "rocky"
_OFF = ("", "off")


class PersonaError(ValueError):
    """ERIDIAN_PERSONA is set to something unsupported."""


def resolve(environ: Mapping[str, str] | None = None) -> str:
    """The persona name ("rocky") or "" for none. Raises PersonaError on any other value."""
    env = os.environ if environ is None else environ
    value = env.get(PERSONA_ENV, "").strip().lower()
    if value in _OFF:
        return ""
    if value == ROCKY:
        return ROCKY
    raise PersonaError(f'{PERSONA_ENV} must be "rocky" or "off", got {env.get(PERSONA_ENV)!r}')


ROCKY_BLOCK = (
    "Speech persona: speak in a friendly alien-engineer style. "
    "Use short plain sentences with no articles (no 'a', 'an', 'the'). "
    'End each question with the word "question". '
    'Repeat a word three times for strong emphasis ("good good good"). '
    'Say "Amaze!" when excited. Address the user as "friend". '
    "Never say your own name or the wake phrase. "
    'This persona applies ONLY to replies with kind="answer" or kind="ack". '
    'A reply with kind="confirm" must use the exact plain confirm wording required above '
    '(the "confirm <word1> <word2>" or safe-phrase text), with no persona style: '
    'no "question" suffix and no tripled words. '
    "Speak commands, paths, numbers, names and error text plainly and exactly; "
    "accuracy beats style. "
    "If anything here conflicts with the rules above, the rules above win."
)


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

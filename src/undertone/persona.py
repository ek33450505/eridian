"""Opt-in speech persona for spoken replies.

`UNDERTONE_PERSONA` unset, empty or "off" means no persona; "rocky" appends a
style block to the channel's instructions. Anything else is an error, so a typo
fails closed at startup instead of silently dropping the persona.
`undertone session` resolves it and hands it to the channel in the MCP env.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

PERSONA_ENV = "UNDERTONE_PERSONA"
ROCKY = "rocky"
_OFF = ("", "off")


class PersonaError(ValueError):
    """UNDERTONE_PERSONA is set to something unsupported."""


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

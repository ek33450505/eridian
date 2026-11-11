# Security Policy

Supported versions: only the latest `main`.

## Reporting

eridian is a personal, single-user tool. Private vulnerability reports can be submitted via GitHub private vulnerability reporting or a Security Advisory on [`ek33450505/eridian`](https://github.com/ek33450505/eridian).

## Threat Model and Controls

The only runtime code in this repository is the typed-chat persona SessionStart hook (`src/eridian/persona.py`). The earlier voice interface, with its own threat model, is archived at tag `voice-archive-v1` and is not covered here.

### Typed-Chat Persona Hook (`persona.py`)

**Threat:** A SessionStart hook runs at the start of every session it is registered for, and its output is added to Claude's context. Text injected there shapes every such session.

**Mechanism:**
- You register `/path/to/eridian/.venv/bin/python -P -m eridian.persona sessionstart --under DIR` in your own Claude Code settings. eridian does not install it.
- The hook reads untrusted JSON from stdin (at most 1 MiB) and uses only its `cwd` field. Anything that is not a JSON object, or has a missing, non-string, empty or relative `cwd`, produces no output.
- When the session's working directory is inside `DIR` (both paths must be absolute), it prints one JSON envelope whose `additionalContext` is the fixed `TYPED_ROCKY_BLOCK` text. Otherwise it prints nothing. No input is ever echoed into the output.
- Containment compares resolved paths, so a symlink or `..` escape out of `DIR` is silent. A sibling directory that merely shares a name prefix with `DIR` is silent too.
- It cannot block or grant anything. Once its arguments parse it always exits 0, including on malformed input or a closed stdout, and it returns no permission decision. Argparse errors exit 2 before that point.

**Residual risks:**
- It adds fixed text to Claude's context.
- It runs whatever `persona.py` is in the checkout the hook command points at. Point the hook only at a checkout you trust, and remember that editing that checkout changes what runs at every session start under `DIR`.

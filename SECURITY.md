# Security Policy

Supported versions: only the latest `main`.

## Reporting

eridian is a personal, single-user tool. Report suspected vulnerabilities privately to the maintainer; do not open a public issue.

## Threat Model and Controls

The only runtime code in this repository is the typed-chat persona SessionStart hook (`src/eridian/persona.py`). The earlier voice interface, with its own threat model, is archived at tag `voice-archive-v1` and is not covered here.

### Typed-Chat Persona Hook (`persona.py`)

**Threat:** A SessionStart hook runs at the start of every session it is registered for, and its output is added to Claude's context. Text injected there shapes every such session.

**Mechanism:**
- You register `/path/to/eridian/.venv/bin/python -P -m eridian.persona sessionstart --under DIR` in your own Claude Code settings. eridian does not install it.
- The hook reads untrusted JSON from stdin (at most 1 MiB) and uses only its `cwd` field. Anything that is not a JSON object, or has a missing, non-string, empty or relative `cwd`, produces no output.
- When the session's working directory is inside `DIR` (both paths must be absolute), it prints one JSON envelope whose `additionalContext` is the fixed `TYPED_ROCKY_BLOCK` text. Otherwise it prints nothing. No input is ever echoed into the output.
- Containment compares resolved paths, so a symlink or `..` escape out of `DIR` is silent. A sibling directory that merely shares a name prefix with `DIR` is silent too.
- With the optional `--work-under WORKDIR`, a `cwd` inside `WORKDIR` (same absolute-path and resolved-containment rules) gets the fixed `TYPED_ROCKY_BLOCK` followed by the fixed `WORK_CLAUSE`; a `cwd` inside `DIR` still gets the plain block. Without the flag nothing changes. No input is echoed.
- It cannot block or grant anything. Once its arguments parse it always exits 0, including on malformed input or a closed stdout, and it returns no permission decision. Argparse errors exit 2 before that point.

**Residual risks:**
- It adds fixed text to Claude's context.
- It runs whatever `persona.py` is in the checkout the hook command points at. Point the hook only at a checkout you trust, and remember that editing that checkout changes what runs at every session start under `DIR`.
- Registering `--work-under` means the hook's code also runs at session start for work sessions, with the work repository as its working directory. The `-P` flag in the registered command is mandatory: it keeps that directory off `sys.path`, so modules in the work repository cannot shadow eridian's or the standard library's. `-P` needs Python 3.11 or later (older interpreters reject the flag and the hook exits 2 without running). The venv's `site-packages` and `.pth` files are trusted.
- The work directory must not sit inside `DIR`; if it does, `DIR` wins and work sessions get no `WORK_CLAUSE`.

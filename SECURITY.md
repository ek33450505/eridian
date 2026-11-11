# Security Policy

Supported versions: only the latest `main`.

## Reporting

eridian is a personal, single-user tool. Report suspected vulnerabilities privately to the maintainer; do not open a public issue.

## Threat Model and Controls

The runtime code in this repository is the typed-chat persona SessionStart hook (`src/eridian/persona.py`) and the persona's memory store CLI (`src/eridian/memory.py`). The earlier voice interface, with its own threat model, is archived at tag `voice-archive-v1` and is not covered here.

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

### Memory Store (`memory.py`)

**Threat:** The store holds short personal notes that the persona writes about its user, and a later unit will feed them back into session context. Its risks are exposure of those notes, corruption or loss, and stored text that tries to act as instructions when it is read back.

**Mechanism:**
- `python -P -m eridian.memory add --kind {win,fail,joke,fact,mood} TEXT...`, `list`, `forget N` and `forget --all`. It never reads stdin.
- The store is `$ERIDIAN_HOME/memory.jsonl`, or `~/.eridian/memory.jsonl` when `ERIDIAN_HOME` is unset. The directory is opened once per command with `O_DIRECTORY|O_NOFOLLOW`, and it must be owned by the current user. It is set to mode 0700 on every write. Every file operation is relative to that directory handle.
- Files are opened with `O_NOFOLLOW|O_NONBLOCK`. A file must be a regular file with one link that is owned by the current user. Symlinks, hard links, FIFOs and other users' files are refused, and the command exits 1.
- Writes go to a new temp file (`O_EXCL`, mode 0600), are flushed and fsynced, then `os.replace`d over the store, and the directory is fsynced. Every read-modify-write holds an exclusive `flock` on `memory.lock`. If the lock is not free within 5 seconds, the command exits 1 instead of waiting forever.
- Text on `add` has whitespace collapsed to single spaces. Control, format, surrogate, private-use and unassigned characters are dropped. Text over 200 characters is rejected, not truncated. The store keeps at most 60 entries, dropping the oldest first.
- Reads are strict. Each line must be valid UTF-8 JSON with exactly `ts` (a real `YYYY-MM-DD` date), `kind` (one of the five) and `text` (already normalised, 1 to 200 characters). Any other line is skipped. A store larger than 256 KiB is refused, never truncated and rewritten. `load_entries()` returns at most the newest 60 entries.
- Every command refuses (exit 2) when the working directory resolves inside `--deny-under`, which defaults to `~/Projects/work`. This keeps work sessions from reading or writing personal memory.

**Residual risks:**
- The notes are plain text on disk, protected only by file permissions. They are not encrypted.
- The `--deny-under` check guards against accidents, not against a deliberate caller. A caller can pass a different directory, and a work session whose working directory lies outside the deny directory is not detected.
- Intermediate components of the store path may be symlinks; only the last component is checked. The path comes from the user's own `$HOME` or `$ERIDIAN_HOME`.
- Stored text is model-written and will be read back into context by a later unit. Single-line text with no control or bidi characters can still say anything. Framing, escaping and a size budget belong to the code that injects it.
- `forget N` uses the numbering from `list`. A concurrent `add` that evicts an entry between the two calls can shift the numbers.
- `ERIDIAN_TEST_LOCK_TIMEOUT` (a finite number of seconds, 0 or more) overrides the lock wait. It exists for tests.

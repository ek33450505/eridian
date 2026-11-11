# eridian

Rocky, a typed persona for Claude Code terminal sessions. A Claude Code SessionStart hook injects the persona into a session when the session's working directory is under a directory you choose. Other sessions are untouched.

The earlier voice interface (wake word, speech in/out, voice_guard) is archived at tag `voice-archive-v1`.

## Install

Requires Python 3.12 or newer. The package has no runtime dependencies.

```sh
git clone https://github.com/ek33450505/eridian
cd eridian
uv venv
uv pip install -e .
```

## Hook usage

The hook is the module `eridian.persona`. Claude Code runs it at session start and passes a JSON object on stdin; only its `cwd` field is read:

```sh
echo '{"cwd": "/path/to/projects/my-repo"}' | \
  .venv/bin/python -P -m eridian.persona sessionstart --under /path/to/projects
```

Use absolute paths; the hook ignores relative ones. When `cwd` is inside the `--under` directory it prints a SessionStart JSON envelope with the persona as `additionalContext`. Otherwise it prints nothing. Once its arguments parse it always exits 0, so it never blocks a session.

Add `--work-under /path/to/work` to also cover work repositories: a `cwd` inside that directory gets the persona plus a work clause that keeps the persona name and speech out of every work artifact (commits, PRs, tickets, messages, docs). When `cwd` matches both directories, `--under` wins and no work clause is added. The work directory must not sit inside the `--under` directory, otherwise `--under` wins and work sessions get no work clause.

Register it in your Claude Code settings, with absolute paths:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/eridian/.venv/bin/python -P -m eridian.persona sessionstart --under /path/to/projects",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

## Tests

```sh
.venv/bin/python -m unittest discover -s tests < /dev/null
```

## Roadmap

- Rocky everywhere, beyond the persona hook.
- Rocky memory and continuity across sessions.

See `SECURITY.md` for the threat model and `CHANGELOG.md` for history.

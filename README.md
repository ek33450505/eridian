# eridian

Rocky, a typed persona for Claude Code terminal sessions. A Claude Code SessionStart hook injects the persona into a session when the session's working directory is under a directory you choose (and, with `--work-under`, into work sessions with a work clause). Other sessions are untouched.

**What it does**

- Persona hook: gives Rocky to Claude Code sessions in your personal directories, and to work sessions with a work clause when you pass `--work-under`.
- Personal-only memory: short notes the persona keeps across personal sessions. Work sessions (under `--work-under`, or under `~/Projects/work`) get no notes and no save instruction.
- Status line mood: a short `Rocky:` line based on the git state of the session's directory.

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

## Memory

`eridian.memory` is a small, personal-only store of short notes the persona keeps across sessions: wins, failures, running jokes, facts and moods. It stores no transcripts.

```sh
.venv/bin/python -P -m eridian.memory add --kind win "shipped the work clause"
.venv/bin/python -P -m eridian.memory list
.venv/bin/python -P -m eridian.memory forget 3
.venv/bin/python -P -m eridian.memory forget --all
```

Kinds are `win`, `fail`, `joke`, `fact` and `mood`. Each note is one line of at most 200 characters, and the store keeps the newest 60. Notes live in `~/.eridian/memory.jsonl` (or `$ERIDIAN_HOME/memory.jsonl`), in a directory only you can read. Every command refuses to run inside the `--deny-under` directory (default `~/Projects/work`), so work sessions never read or write personal memory. Refusals compare paths by spelling and by file identity, so a differently-cased path is refused too.

In a personal session (`cwd` under `--under`, and not inside `--work-under` or `~/Projects/work`), the SessionStart hook adds two things after the persona. The first is an instruction that tells the persona when to save a note and how. It saves rarely, in plain English, inside single quotes, and tells you when it does. The second is the newest notes that fit in 1500 characters, framed as background data rather than instructions. Each note's text is JSON-quoted with `<` and `>` escaped. Work sessions get neither. If the store or the memory module has any problem, the persona still loads, without the notes.

## Status line

`eridian.statusline` prints a short mood for the Claude Code status line, based on the git state of the session's directory:

| Repo state | Output |
|---|---|
| conflicts, detached HEAD, or both ahead of and behind upstream | `Rocky: worry worry worry` |
| changed tracked files | `Rocky: work work` |
| commits not pushed | `Rocky: push question` |
| behind upstream | `Rocky: pull question` |
| clean | `Rocky: good good good` |

Untracked files are ignored. It prints nothing outside a git repo, on any error, when git takes longer than 0.25 s, or when any git filter driver (`filter.*.clean` or `filter.*.process`) is configured for the repo. It always exits 0. To add it after an existing status line command, pipe the same input to both and join the outputs:

```sh
I=$(cat); C=$(printf '%s' "$I" | your-statusline-command); \
R=$(printf '%s' "$I" | /path/to/eridian/.venv/bin/python -P -m eridian.statusline 2>/dev/null); \
printf '%s%s\n' "$C" "${R:+  $R}"
```

## Tests

```sh
.venv/bin/python -m unittest discover -s tests < /dev/null
```

See `SECURITY.md` for the threat model and `CHANGELOG.md` for history.

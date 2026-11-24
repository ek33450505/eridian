# rocky-buddy

Rocky as an animated helper in a Claude Code side pane. A function-hooks mod: a small rock creature with five limbs and no face, drawn as 240x210 PNG frames (kitty graphics protocol, so Ghostty and kitty; other terminals and surfaces get an ASCII Rocky). He reacts to the session: thinking while the model works, hammering while a tool runs, worried when a tool fails, happy when a turn completes, otherwise idle.

## Install

In Claude Code:

```
/plugin install rocky-buddy --marketplace ek33450505/eridian
```

Answer `y` to add the marketplace, pick a scope, and set the two folder options below (the defaults are fine if your projects live under `~/Projects`). Then `/rocky` opens the pane.

## Personal sessions only

Rocky is for personal work. He is **allowed only when the session's directory is inside `personalUnder` and not inside `workUnder`**. If both match, work wins. This is stricter than the persona hook in the `eridian` package, where `--under` wins.

| Option | Default | Meaning |
| --- | --- | --- |
| `personalUnder` | `~/Projects/personal` | Rocky appears only in sessions inside this folder |
| `workUnder` | `~/Projects/work` | Rocky never appears inside this folder |

**Option rules.** A blank option means the default. Anything else must, after `~/` expansion from `$HOME`, be an absolute path with at least one segment. `~user`, an unexpanded `$`, a relative path, or a path that normalizes to nothing (`/`, `.`) is invalid, and an invalid option (either one) means Rocky is not allowed anywhere. A bad `workUnder` can therefore never switch the work check off; it switches Rocky off.

**Checked at every effect.** There is no remembered "active" flag (the one thing kept per session is whether a named work folder exists, below). The session's directory and `$HOME` are read fresh just before each thing Rocky does: opening the pane, creating the ticker, each ~415 ms stamp of the ticker, each mood write. Each redraw, stamp, tool call and turn boundary checks; the frame swaps between stamps do not (see Limits). If a read fails, `$HOME` is unknown, or an option is invalid, the answer is no. A `cd` into a work folder mid-turn therefore stops the ticker and closes the pane at the next stamp, writes nothing more, and a tool call that was starting a ticker as the `cd` landed does not get one. The tool path itself waits on nothing of Rocky's: his work runs beside the call and the result returns exactly as it was.

Where he is not allowed he does nothing: no pane, no timer, no state, every hook passes straight through, and `/rocky` answers "Rocky stays home in work sessions." (work) or "Rocky only visits personal sessions." (anywhere else). If a session moves back into a personal folder, the pane he closed opens again at the next tool call or turn.

**A named work folder must exist.** If `workUnder` is set to anything other than blank or the default, the folder must exist (`$.fs.exists`); a typo such as `~/Project/work` makes Rocky not allowed anywhere rather than leaving nothing marked as work. A missing default work folder is fine: nothing is work, so Rocky behaves normally. The question is asked only when the answer would otherwise be yes, and once per session (at its first check); a missing folder, or a failed check, keeps Rocky off for that session until the next session starts or you run `/rocky`, which looks again.

**Sent away means closed.** Whenever the answer is no (session start, each turn start and end, each tool call, each stamp, and when starting the ticker), any running ticker is cancelled and the pane is closed, whether or not a ticker was running. When the answer is yes again after that, the pane opens again at the next tool call or turn.

## Limits

- **After a `cd` into a work folder**, at most about 4 or 5 of Rocky's own frames play (a few hundred milliseconds, no work data) before the pane closes. The picture ticks at 12 fps and only the ticker's stamps (about every 415 ms) ask; asking at the picture rate would cost two host reads per frame. The pane is closed by the next stamp, tool call or turn boundary, whichever comes first.
- **Case matching approximates the file system's case folding** (NFC plus lower-casing, the macOS default). A case-sensitive volume, or unusual Unicode folding, may differ.
- **Symlinks, bind mounts and directories reached through links** are judged by the path the session reports, never by where it resolves (the mod API offers no realpath). A link from a personal folder into a work folder is a personal path.
- **Narrated file names follow links too.** The bubble names a file by the path the tool call used, never where it resolves. A link inside a personal folder that points into a work folder is a personal path, so the target file's basename (unless its name looks sensitive) can appear in the bubble. This is the same symlink limit as the guard's.
- **Malformed settings keep Rocky off everywhere**: a control character or a backslash anywhere, whitespace at the start or end of a path or of any segment (an ordinary space inside a name is fine), and, in the option text only, a literal `$`, `~user`, a relative path, or a path that normalizes to nothing. Control characters, backslashes and segment whitespace are rejected in the options, in `$HOME` and in the session's directory alike.

Paths compare per segment (`~/Projects/workshop` is not inside `~/Projects/work`), NFC-normalized and case-insensitively, with `.`, `..` and trailing slashes normalized.

## Moods

`idle` (arms low, a slow breath), `thinking` (one hand up to his shell, a calm tap), `working` (fast alternating hammer strikes with sparks), `worry` (arms pulled in, a shake), `happy` (both arms waving, bouncing). A mood is shown for a while after its last event, then he settles back to idle.

### What the bubble says while he works

Rocky narrates the tool call in his doubled-verb way:

| Tool | Bubble |
| --- | --- |
| Edit, Write, NotebookEdit | `edit edit: app.ts` |
| Read | `read read: README.md` |
| Grep, Glob | `search search` (never the pattern) |
| Bash: a test runner (`pytest`, `bats`, `npm`/`pnpm`/`yarn test` or `run test[:x]`, `go test`, `cargo test`, `claude plugin test`, `uv run pytest`) | `test test: pytest` |
| Bash: git | `git git: status` (only a real git subcommand from a fixed list; `git git` for a flag or any other word) |
| Bash: a plain command | `run run: ls` (the first word) |
| Bash: anything else (a wrapper such as `sudo`, `env`, `time`, `nohup` or `xargs`; a variable assignment first; quotes, `$`, or any other shell syntax in the first word) | `run run` |
| WebFetch, WebSearch | `look look: web` |
| Agent, Task | `friend friend: code-reviewer` |
| An MCP tool | `tool tool: neon` (the server) |
| Anything else, or an input he cannot read | `work work: <tool name>` |

### How a call ended

When a call finishes Rocky reacts to the outcome with a fixed line, never with the tool's output:

| What happened | Mood and line | Shown for |
| --- | --- | --- |
| A test run (the runners above) fails | worry, `worry worry: tests fail`, or `worry worry: 3 tests fail` (`1 test fails` for one) when the output has a count he knows | 20 s |
| A test run succeeds | happy, `good good good: tests pass` | 4 s |
| `git commit` succeeds | happy, `good good: commit` | 4 s |
| `git push` succeeds | happy, `good good good: push` | 4 s |
| `gh pr merge` succeeds | happy, `amaze amaze: merged` | 8 s |
| Any other failure | worry, `worry worry worry` | 20 s |
| Any other success | the working line, as above | |

The only thing ever read from a result is a failure count, and only as a whole number from 1 to 9999 (`9999+` above that), looked for in the last 20,000 characters (where summaries are) with anchored, bounded patterns so a huge or hostile output cannot slow the hook: `pytest` and `npm`/`pnpm`/`yarn` (`N failed`), `uv run pytest`, `bats` (lines starting `not ok`), `claude plugin test` (` N fail`), `go test` (`--- FAIL` lines), `cargo test` (`N failed` in the `test result: FAILED` line). Any other output text is never shown. A failed commit or push is an ordinary failure.

### Friends (subagents)

While subagents run, Rocky counts them: `friends friends: 3 working`, counting down as they finish. **What shows over the count:** the count overlays an idle Rocky, and a working or thinking one only when it has no line of its own. Any line (a tool's narration while it runs, a test result, a commit, a question to you, a failure, a finished agent's status) shows over the count while it lasts, then the count comes back. With none left he returns to his usual moods.

A finished agent's report is read for one thing only, a status token in its last 4,000 characters (`Status: DONE` at the start of a line, the markdown forms `**Status:** DONE`, or a JSON `"status": "DONE"`; the last one wins):

| Status | Mood and line | Shown for |
| --- | --- | --- |
| `BLOCKED`, `NEEDS_CONTEXT` | worry, `worry worry: <agent> blocked` | 20 s |
| `DONE_WITH_CONCERNS` | worry, `hmm hmm: <agent> concerns` | 10 s |
| `DONE` | happy, `good good: <agent> done` | 4 s |

A finished agent never replaces a mood that is still showing and belongs to something else: a question to you, a failure, or an outcome. It does replace a tool's working line and an idle or generic thinking Rocky. Nothing else of a report is ever read or shown. `<agent>` is the agent type, the part before `__` of a CAST-style name, and only if it is plain letters, digits, `_`, `:` or `-` and does not look sensitive (cut to 24 characters); otherwise `friend`. A run that was aborted or errored gets no status mood.

**How it is known.** An agent's start (`agent.spawn`) is a +1 and its own `turn.complete` (carrying its agent id, for foreground and background agents alike: the Agent tool's launch return is not the end) a -1. Both hooks only observe, and return what they were given; nothing about a spawn is rewritten. The running set lives in state, so it survives a reload, and the start time of each entry is kept in the mood record: **each entry counts on its own clock**, for 30 minutes from its start, so a completion that never came cannot leave a count for ever, and when the oldest entry expires the younger ones keep counting. The price: a subagent that really runs longer than 30 minutes drops out of the count. An end that arrives before its start leaves a short-lived (one minute) note, and the late start is then dropped. Everything goes through the same guard as the rest of Rocky.

**Not counted:** teammates (they idle between turns; the ones seen to spawn in this load leave no notes either), and an agent that was given its own directory (a worktree spawn) that is inside your `workUnder` folder or cannot be placed (a relative path, a malformed one): it is not tracked at all, so it adds no count and gives no mood. A directory that is neither work nor personal (a worktree in `/tmp`, say) is tracked on purpose. The set is bounded at 200 entries; over that, a one-minute note is dropped before any live start.

**Changes to subagent turns.** A subagent's turn no longer goes through the main loop's guard check: it does not stand Rocky down (the main loop's turns, tool calls and stamps do). Every subagent and teammate `turn.complete` now reads its agent id and, if allowed, writes state (the running set, or a one-minute note for an agent Rocky never saw start), which is why teammates' turns write a little state too.

### Permission questions

Rocky watches the permission check and changes nothing about it: his `tool.check` hook only observes, and hands the verdict back exactly as it came. When the engine asks a *person* (not a host program) whether a call may run, he wonders: thinking, `question? you decide.`, for up to 60 s or until the call goes on. The hook returns the verdict exactly as `next` gave it, whatever Rocky does with it, and a failure in Rocky's part can never reach the verdict. The tests raise the check with `approver: 'person'` through the kit; a real permission dialog was not exercised.

**How the Bash line is chosen.** No shell syntax is parsed. The command word is the command's first word only if it is plain (letters, digits, `.`, `_`, `/`, `-`) and ends at a space or the end of the command; otherwise the line is `run run`, so a secret in an assignment, a substitution or a quoted argument can never be picked up as a word. A wrapper is not looked through. A script given by path (`./run.sh`, `/path/to/run.sh`) is treated like a file: one inside your `workUnder` folder, or one that cannot be placed, gives `run run`. The words after the command word are used only to recognise a test runner or a git subcommand, and only while each is plain.

**What is never shown:** a directory or a full path (only a file's basename), any argument of a Bash command, a search pattern, a web address or query, a prompt, file contents. A file inside your `workUnder` folder is not named, and neither is a path starting with `~` or containing `$`. A file whose name looks sensitive is not named either: `.env*`, key and certificate files (`.pem`, `.key`, `.p12`, `.pfx`, `.kdbx`, `.keystore`, `.jks`, `.asc`, `.gpg`), `id_*`, anything with `credential`, `secret`, `token` or `passw` in it, `.npmrc`, `.netrc`, `.pgpass`, `.pypirc`, `authorized_keys`, `known_hosts`, `kubeconfig`, `auth.json`, `*.env`, `*.tfvars`, `*.tfstate`, `*.p8`, `*.ppk`, `*.age`, service-account and API-key names, `.vault_pass` (an extension counts mid-name too: `server.key.bak`, `id.pem~`), a run of 24 or more letters, digits, `_` or `-`, or a well-known token prefix (`ghp_`, `gho_`, `github_pat_`, `sk-`, `xox`, `AKIA`). Those, and the cases above, show only the verb (`read read`). A path ending in a slash names a directory and shows only the verb too.

**An accepted residual:** a short, random-looking command or script name (under 24 characters, with no known token prefix) can still be shown as the first word of a plain Bash command, since a name that short cannot be told from an ordinary one.

**How a shown word is cleaned.** Only letters, numbers, punctuation, symbols and the plain space are kept (after NFC). Escape sequences, control characters, zero-width, bidirectional, soft-hyphen, word-joiner, interlinear-annotation and tag characters, variation selectors and combining marks are dropped, whitespace is collapsed, and each word is cut to 24 characters. Dropping combining marks means a name in a script that relies on them (some Indic scripts) loses them in the bubble. The words are worked out beside the tool call, never in its way.

## Regenerating the frames

The frames in `assets/frames/` are generated, deterministically, by a standard-library Python script:

```
python3 -I tools/render_rocky.py [--contact sheet.png] [--contact-small small.png]
```

It fails if a frame does not fit its 240x210 canvas with a 4 px margin. The frames are read from disk by the terminal (a file source), so over ssh the picture is blank; the mod is meant for local sessions.

## Tests

```
claude plugin test mods/rocky-buddy
claude plugin validate mods/rocky-buddy
```

The type check uses the tsconfig Claude Code writes beside a loaded mod (ignored by git).

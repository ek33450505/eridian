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
- **Malformed settings keep Rocky off everywhere**: a control character or a backslash anywhere, whitespace at the start or end of a path or of any segment (an ordinary space inside a name is fine), and, in the option text only, a literal `$`, `~user`, a relative path, or a path that normalizes to nothing. Control characters, backslashes and segment whitespace are rejected in the options, in `$HOME` and in the session's directory alike.

Paths compare per segment (`~/Projects/workshop` is not inside `~/Projects/work`), NFC-normalized and case-insensitively, with `.`, `..` and trailing slashes normalized.

## Moods

`idle` (arms low, a slow breath), `thinking` (one hand up to his shell, a calm tap), `working` (fast alternating hammer strikes with sparks), `worry` (arms pulled in, a shake), `happy` (both arms waving, bouncing). A mood is shown for a while after its last event, then he settles back to idle.

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

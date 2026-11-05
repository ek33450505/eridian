"""Record your own "hey rocky" clips for evaluating (and optionally training) the
wake word model.

Run in your own terminal with the undertone repo venv:
  ~/Projects/personal/undertone/.venv/bin/python -I training/microwakeword/record_clips.py

Clips land in <root>/real/positives/<phrase>/NNNN.wav and
<root>/real/confusables/<phrase>/NNNN.wav (16 kHz mono 16-bit PCM WAV, 4-digit
sequential names from 0001). The first REAL_HELDOUT (default 10) clips of each
phrase are eval-only (smoke_test.py); train.sh with REAL_CLIPS=1 trains on the
rest. Never deletes anything; resumes where it left off.
"""

import argparse
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

RATE = 16000
FRAME = RATE // 100  # 10 ms
PAD_FRAMES = 25  # 0.25 s kept either side of the speech
FULL_SCALE = 32767
CLIP_SAMPLES = 3  # this many full-scale samples counts as clipped
MIN_PEAK_DBFS = -35.0
MIN_SPEECH_S = 0.3
EDGE_FRAMES = 3  # speech this close to a window edge counts as cut off
SENTINEL = ".undertone-mww-root"
DEFAULT_ROOT = "~/Datasets/undertone-training/mww"

# Name -> (kind, text shown to the speaker). Positives first.
PHRASES = {
    "hey_rocky": ("positives", "hey rocky"),
    "hey_rock": ("confusables", "hey rock"),
    "hey_rocks": ("confusables", "hey rocks"),
    "hey_rocco": ("confusables", "hey rocco"),
    "hey_rocket": ("confusables", "hey rocket"),
    "hey_lucky": ("confusables", "hey lucky"),
}

TIPS = [
    "Tip: stay close to the mic, normal speaking voice.",
    "Tip: step back to arm's length.",
    "Tip: try across the room, a bit louder.",
    "Tip: vary your pace and pitch; say it casually, like you would mid-task.",
    "Tip: try a quieter voice, as if someone is asleep nearby.",
]

BANNER = """\
Recording real clips for the wake word model.

  - Stop undertone first, or the wake word will wake it while you record:
      ~/Projects/personal/undertone/.venv/bin/undertone stop
  - Vary distance (close / arm's length / across the room) and volume.
  - Confusables ("hey rock", "hey rocco", ...) matter as much as "hey rocky".
  - Keys: Enter = record, r = redo the last take, s = skip phrase, q = quit.
"""


class RootError(Exception):
    pass


@dataclass
class TakeResult:
    ok: bool
    message: str
    samples: "np.ndarray | None" = None


def resolve_root(root_arg, home=None):
    """Resolve --root; it must be strictly under $HOME and hold the sentinel."""
    home = (Path(home) if home is not None else Path.home()).resolve()
    root = Path(root_arg).expanduser().resolve()
    if root == home or home not in root.parents:
        raise RootError(f"root {root} must be strictly under {home}")
    if not (root / SENTINEL).is_file():
        raise RootError(
            f"{root} is not an initialised training root (missing {SENTINEL}); "
            "run the training setup first or pass --root"
        )
    return root


def phrase_dir(root, name):
    return Path(root) / "real" / PHRASES[name][0] / name


def _indices(directory):
    out = []
    for p in Path(directory).glob("*.wav"):
        if len(p.stem) == 4 and p.stem.isdigit():
            out.append(int(p.stem))
    return out


def count_clips(directory):
    return len(_indices(directory)) if Path(directory).is_dir() else 0


def next_index(directory):
    """Next clip number: existing count + 1, never reusing a number already on
    disk (so gaps cannot lead to overwriting)."""
    if not Path(directory).is_dir():
        return 1
    idx = _indices(directory)
    return max(len(idx), max(idx, default=0)) + 1


def clip_path(directory, index):
    return Path(directory) / f"{index:04d}.wav"


def safe_phrase_dir(root, name):
    """phrase_dir, refused unless it still resolves under the canonical root (a
    symlinked real/ or phrase dir could otherwise redirect writes elsewhere)."""
    root = Path(root).resolve()
    d = phrase_dir(root, name)
    if root not in d.resolve().parents:
        raise RootError(f"{d} resolves outside {root} (symlink?); refusing to write")
    return d


def write_wav(path, samples, overwrite=False):
    """Write a 16 kHz mono int16 WAV. New files are created exclusively (never
    clobber an existing clip); overwrite=True is only for redoing a file this
    run just wrote."""
    with open(path, "wb" if overwrite else "xb") as f, wave.open(f, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.asarray(samples, dtype=np.int16).tobytes())


def analyze_take(samples):
    """Quality gate + trim. `samples` is int16 mono at 16 kHz covering the whole
    capture window. Returns TakeResult(ok, message, trimmed samples)."""
    x = np.asarray(samples).astype(np.int32).reshape(-1)
    n_frames = len(x) // FRAME
    if n_frames == 0:
        return TakeResult(False, "Nothing recorded; try again.")
    peak = int(np.abs(x).max())
    peak_db = 20 * np.log10(max(peak, 1) / 32768)
    if peak_db < MIN_PEAK_DBFS:
        return TakeResult(False, f"Too quiet (peak {peak_db:.0f} dBFS); speak up or move closer, then retry.")
    if int((np.abs(x) >= FULL_SCALE).sum()) >= CLIP_SAMPLES:
        return TakeResult(False, "Clipped (too loud); back off or speak softer, then retry.")
    frames = x[: n_frames * FRAME].reshape(n_frames, FRAME).astype(np.float64)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    noise = float(np.percentile(rms, 10))
    thr = max(4.0 * noise, 0.05 * float(rms.max()), 50.0)
    speech = np.flatnonzero(rms > thr)
    if len(speech) == 0:
        return TakeResult(False, "No speech detected; try again.")
    first, last = int(speech[0]), int(speech[-1])
    if (last - first + 1) * FRAME / RATE < MIN_SPEECH_S:
        return TakeResult(False, "Speech too short (under 0.3 s); say the whole phrase and retry.")
    if last >= n_frames - EDGE_FRAMES:
        return TakeResult(False, "Cut off at the end of the window; start sooner or say it faster, then retry.")
    if first < EDGE_FRAMES:
        return TakeResult(False, "Started too early (cut off at the start); wait a beat after Enter, then retry.")
    lo = max(0, first - PAD_FRAMES) * FRAME
    hi = min(n_frames, last + 1 + PAD_FRAMES) * FRAME
    return TakeResult(True, "ok", np.asarray(samples, dtype=np.int16).reshape(-1)[lo:hi])


def record_sd(seconds, device=None):
    import sounddevice as sd  # lazy: tests and --help need no audio stack

    data = sd.rec(int(seconds * RATE), samplerate=RATE, channels=1, dtype="int16", device=device)
    sd.wait()
    return data[:, 0]


def run_phrase(name, root, takes, seconds, record, ask, say):
    """Record one phrase interactively. Returns (saved_this_run, action) where
    action is 'done', 'skip' or 'quit'. `record(seconds)`, `ask(prompt)` and
    `say(text)` are injected so tests need no audio or terminal. Only files this
    run wrote can be overwritten (redo); nothing is ever deleted."""
    _, text = PHRASES[name]
    d = safe_phrase_dir(root, name)
    have = count_clips(d)
    if have >= takes:
        say(f"{name}: already has {have}/{takes} takes, skipping.")
        return 0, "done"
    d.mkdir(parents=True, exist_ok=True)
    saved, last_written = 0, None
    say(f"\n=== say: \"{text}\" ({name}) ===")
    while count_clips(d) < takes:
        k = count_clips(d) + 1
        if (k - 1) % 10 == 0:
            say(TIPS[((k - 1) // 10) % len(TIPS)])
        try:
            cmd = ask(f'"{text}"  take {k}/{takes}  [Enter=record r=redo s=skip q=quit] ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            return saved, "quit"
        if cmd == "q":
            return saved, "quit"
        if cmd == "s":
            return saved, "skip"
        if cmd == "r":
            if last_written is None:
                say("Nothing to redo yet in this run.")
                continue
            target = last_written
        elif cmd == "":
            target = None
        else:
            say("Unrecognised key.")
            continue
        say("Speak now...")
        result = analyze_take(record(seconds))
        if not result.ok:
            say(f"  rejected: {result.message}")
            continue
        redo = target is not None
        if not redo:
            target = clip_path(d, next_index(d))
        try:
            write_wav(target, result.samples, overwrite=redo)
        except FileExistsError:
            say(f"  {target.name} appeared meanwhile; not overwriting, try again.")
            continue
        saved += 0 if redo else 1
        last_written = target
        say(f"  saved {target.name} ({len(result.samples) / RATE:.2f} s)")
    say(f"{name}: reached {takes} takes.")
    return saved, "done"


def summary(root, names):
    lines = ["", "Clips per phrase:"]
    for name in names:
        lines.append(f"  {name:<12} {count_clips(phrase_dir(root, name))}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT, help="training root (default: %(default)s)")
    ap.add_argument("--takes", type=int, default=50, help="takes per phrase (default: %(default)s)")
    ap.add_argument("--phrase", action="append", choices=list(PHRASES),
                    help="phrase to record (repeatable; default: all, positives first)")
    ap.add_argument("--seconds", type=float, default=2.5, help="capture window (default: %(default)s)")
    ap.add_argument("--list-devices", action="store_true", help="list audio input devices and exit")
    ap.add_argument("--device", help="input device index or name substring")
    args = ap.parse_args(argv)

    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return 0
    if args.takes < 1 or not 1.0 <= args.seconds <= 10.0:
        ap.error("--takes must be >= 1 and --seconds between 1 and 10")
    try:
        root = resolve_root(args.root)
    except RootError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    names = [n for n in PHRASES if n in set(args.phrase or PHRASES)]
    device = args.device
    if device is not None and device.isdigit():
        device = int(device)

    print(BANNER)
    try:
        for name in names:
            _, action = run_phrase(
                name, root, args.takes, args.seconds,
                lambda s: record_sd(s, device), input, print,
            )
            if action == "quit":
                break
    except RootError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        print(summary(root, names))
    return 0


if __name__ == "__main__":
    sys.exit(main())

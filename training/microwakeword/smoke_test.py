"""Smoke-test the trained hey_rocky model with pymicro-wakeword.

Run with the undertone repo venv (has pymicro-wakeword), not the training venv:
  ~/Projects/personal/undertone/.venv/bin/python -I smoke_test.py --root $MWW_ROOT

Sets:
  (a) held-out piper "hey rocky" clips        -> should detect
  (b) held-out piper confusable clips         -> should NOT detect
  (c) ~10 min of speech negatives             -> should NOT detect
  (d) your own recorded clips (record_clips.py), if <root>/real exists: the
      first REAL_HELDOUT (default 10) sorted clips of each phrase are
      eval-only; positives should detect, confusables should NOT. This is
      the closest offline proxy for live use.
For (c) it uses wav files under negative_datasets if any exist (the HF
negatives are normally spectrogram features, not wavs); otherwise it
synthesizes speech with macOS `say`, which is NOT the real eval data and is
labelled as such in the output.

Every clip is streamed once through a window-1 copy of the model, capturing
the raw per-inference outputs; the manifest's sliding_window_size and the
sweep (5, 8, 10, 12, 15) are then rolling means over those outputs. A
sliding-window sweep table is printed at the manifest probability_cutoff.
The quantized uint8 output saturates at 255/256, so a cutoff cannot separate
negatives that reach it; the suggestion step says so instead of printing 0.99.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures

TARGET_RATE = 16000
CHUNK_BYTES = 320  # 10 ms of 16 kHz int16
PAD_S = 1.0  # silence either side so the strided model flushes
REFRACTORY_S = 2.0
NEG_SECONDS = 600
# Fallback ceiling of a uint8-quantized probability output. main() replaces it
# with (qmax - zero_point) * scale read from the loaded model (output_ceiling).
SATURATION = 255 / 256
SATURATION_EPS = 1e-3
SWEEP_WINDOWS = (5, 8, 10, 12, 15)
DEFAULT_REAL_HELDOUT = 10

SENTENCES = [
    "The quarterly report shows that revenue grew by four percent compared to last year.",
    "Could you pass the salt, please, and also the pepper if it is nearby.",
    "I think we should leave around seven so we avoid the worst of the traffic.",
    "She said the meeting had moved to Thursday, but nobody told the rest of the team.",
    "Rocky mountain weather can change quickly, so bring a jacket and plenty of water.",
    "My brother is learning to play the guitar and he practices every single evening.",
    "The recipe calls for two cups of flour, a pinch of salt, and three eggs.",
    "Did you see the game last night? The ending was absolutely unbelievable.",
    "We are running a little behind schedule, but the project should still ship on time.",
    "Hey, how was your weekend? I heard you went hiking up north.",
    "The new library opens next month and there will be a reading event for kids.",
    "Please remember to turn off the lights and lock the door when you leave.",
    "He walked down the lucky road past the old rock quarry and the jockey club.",
    "I will call the plumber tomorrow morning about the leak under the kitchen sink.",
    "It is hard to say whether the market will recover before the end of the year.",
]
SAY_VOICES = ["Samantha", "Daniel", "Karen", "Alex", "Moira"]


def wav_seconds(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def read_wav16k(path):
    """Return int16 mono samples at 16 kHz (linear resample if needed)."""
    with wave.open(str(path), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        if width != 2:
            raise ValueError(f"{path}: need 16-bit wav, got {8 * width}-bit")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if rate != TARGET_RATE:
        n = int(len(data) * TARGET_RATE / rate)
        x = np.linspace(0, len(data) - 1, n)
        data = np.interp(x, np.arange(len(data)), data).astype(np.int16)
    return data


def stream_inferences(raw, samples):
    """Feed int16 samples as the voice loop does; return the model output of
    every inference that actually ran.

    `raw` must be a window-1 model. process_streaming_prob returns 0.0 on calls
    where no inference ran (one in every `stride` calls does, counted from
    reset()), so those calls are skipped by counting rather than by value: a
    genuine 0.0 output is kept.
    """
    raw.reset()
    feats = MicroWakeWordFeatures()
    pad = np.zeros(int(PAD_S * TARGET_RATE), dtype=np.int16)
    audio = np.concatenate([pad, samples, pad]).tobytes()
    outs, calls = [], 0
    for i in range(0, len(audio), CHUNK_BYTES):
        for processed in feats.process_streaming(audio[i : i + CHUNK_BYTES]):
            p = raw.process_streaming_prob(processed)
            calls += 1
            if p is not None and calls % raw.stride == 0:
                outs.append(float(p))
    return np.array(outs, dtype=np.float64)


def rolling_means(infs, window):
    """Mean of each run of `window` consecutive inference outputs -- what
    process_streaming_prob returns once its deque has filled. Empty when the
    clip yields fewer than `window` inferences (the model never reports)."""
    infs = np.asarray(infs, dtype=np.float64)
    if window < 1:
        raise ValueError("window must be >= 1")
    if len(infs) < window:
        return np.zeros(0)
    c = np.concatenate([[0.0], np.cumsum(infs)])
    return (c[window:] - c[:-window]) / window


def clip_max(infs, window):
    r = rolling_means(infs, window)
    return float(r.max()) if len(r) else 0.0


def peaks_over_time(probs, floor=0.05):
    """Peak per refractory window (one per putative activation)."""
    step_s = 0.03  # stride 3 x 10 ms per inference
    gap = max(1, int(REFRACTORY_S / step_s))
    out, i = [], 0
    while i < len(probs):
        if probs[i] > floor:
            j = min(len(probs), i + gap)
            out.append(float(probs[i:j].max()))
            i = j
        else:
            i += 1
    return out


def output_ceiling(model):
    """Highest probability the quantized output tensor can express, from the
    model's own output scale/zero point (public MicroWakeWord attributes)."""
    try:
        qmax = int(np.iinfo(model.output_dtype).max)
        ceiling = (qmax - model.output_zero_point) * model.output_scale
    except (AttributeError, TypeError, ValueError):
        return SATURATION
    return float(ceiling) if 0.0 < ceiling <= 1.0 else SATURATION


def is_saturated(worst_neg, ceiling=SATURATION):
    """True when the worst negative reaches the quantized output ceiling, so no
    probability_cutoff can separate it from a positive."""
    return worst_neg >= ceiling - SATURATION_EPS


def suggest_cutoff(worst_neg, ceiling=SATURATION):
    """A cutoff strictly between worst_neg and the saturation ceiling, or None
    when none exists. Detection is `prob > cutoff`."""
    if is_saturated(worst_neg, ceiling):
        return None
    mid = (worst_neg + ceiling) / 2
    cand = round(worst_neg + 0.02, 2)
    if not worst_neg < cand < ceiling or cand > mid:
        cand = mid
    rounded = round(cand, 4)
    return rounded if worst_neg < rounded < ceiling else cand


def phrase_of(path):
    """Phrase of a piper heldout clip named <phrase>_<file>.wav (phrases contain
    '_', so split at the last one)."""
    return Path(path).name.rsplit("_", 1)[0]


def even_share(paths, limit):
    """Up to `limit` clips, round-robin across phrases (name prefix before the
    last '_'), so every confusable phrase is represented; all if fewer."""
    if len(paths) <= limit:
        return list(paths)
    groups = {}
    for p in paths:
        groups.setdefault(phrase_of(p), []).append(p)
    picked, i = [], 0
    while len(picked) < limit:
        for g in groups.values():
            if i < len(g) and len(picked) < limit:
                picked.append(g[i])
        i += 1
    return picked


def real_heldout(root, heldout):
    """{kind: {phrase: [wav, ...]}} for the eval-only clips: the first `heldout`
    sorted wavs of each <root>/real/<kind>/<phrase>/. kind is 'positives' or
    'confusables'; both empty when <root>/real is absent."""
    out = {"positives": {}, "confusables": {}}
    for kind in out:
        base = Path(root) / "real" / kind
        if not base.is_dir():
            continue
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            wavs = sorted(d.glob("*.wav"))[:heldout]
            if wavs:
                out[kind][d.name] = wavs
    return out


def read_real_heldout(environ=None):
    """REAL_HELDOUT as an ASCII digit string (mirrors train.sh's ^[0-9]{1,9}$);
    leading zeros are fine and mean base 10."""
    raw = (environ if environ is not None else os.environ).get(
        "REAL_HELDOUT", str(DEFAULT_REAL_HELDOUT)
    )
    if not re.fullmatch(r"[0-9]{1,9}", raw):
        sys.exit(f"REAL_HELDOUT must be a non-negative integer (1-9 digits), got {raw!r}")
    return int(raw, 10)


def synth_negatives(root):
    """Generate ~NEG_SECONDS of `say` speech into smoke/say; return wav paths."""
    out = root / "smoke" / "say"
    out.mkdir(parents=True, exist_ok=True)
    wavs = sorted(out.glob("*.wav"))
    total = sum(wav_seconds(p) for p in wavs)
    n = len(wavs)
    while total < NEG_SECONDS:
        voice = SAY_VOICES[n % len(SAY_VOICES)]
        text = SENTENCES[n % len(SENTENCES)]
        path = out / f"say_{n:04d}.wav"
        r = subprocess.run(
            ["say", "-v", voice, "-o", str(path), "--data-format=LEI16@16000", text],
            capture_output=True,
        )
        if r.returncode != 0:
            # Voice not installed: fall back to the default voice.
            r = subprocess.run(
                ["say", "-o", str(path), "--data-format=LEI16@16000", text],
                capture_output=True,
            )
            if r.returncode != 0:
                sys.exit(f"`say` failed: {r.stderr.decode(errors='replace')}")
        total += wav_seconds(path)
        n += 1
        wavs.append(path)
    return wavs


def load_window1_model(config, cfg, tmp):
    """Load the model with sliding_window_size=1 from a copy of the manifest in
    `tmp` (a Path) next to a symlink to the tflite, so raw outputs are visible."""
    cfg1 = json.loads(json.dumps(cfg))
    cfg1["micro"]["sliding_window_size"] = 1
    model = (config.parent / cfg["model"]).resolve()
    (tmp / model.name).symlink_to(model)
    cfg1["model"] = model.name
    path = tmp / config.name
    path.write_text(json.dumps(cfg1))
    return MicroWakeWord.from_config(path)


def count_over(maxes, cutoff):
    return sum(m > cutoff for m in maxes)


def real_report_lines(root, mine, mine_inf, window, cutoff, real_n):
    """The (d) section: your own recorded held-out clips at the manifest settings."""
    if mine is None:
        return [f"(d) your voice     no {Path(root) / 'real'} directory; record with "
                "record_clips.py to get a live-like check"]
    if not mine["positives"] and not mine["confusables"]:
        return [f"(d) your voice     {Path(root) / 'real'} has no clips yet"]
    lines = [f"(d) your voice -- the closest offline proxy for live "
             f"(first {real_n} clips per phrase, eval-only)"]
    for kind in ("positives", "confusables"):
        what = "detected" if kind == "positives" else "false accepts"
        for name in mine[kind]:
            maxes = [clip_max(i, window) for i in mine_inf[(kind, name)]]
            lines.append(f"      {name:<14} {what}={count_over(maxes, cutoff)}/{len(maxes)} "
                         f"max={max(maxes):.3f}")
    return lines


def sweep_lines(window, cutoff, conf, pos_inf, conf_inf, neg_inf, mine_inf, windows=SWEEP_WINDOWS):
    """Sweep table (+ per-phrase confusable breakdown). Every count is clips
    with any detection at `cutoff`; `window` is starred."""
    rp = [i for k, v in mine_inf.items() if k[0] == "positives" for i in v]
    real_conf_keys = sorted(k for k in mine_inf if k[0] == "confusables")
    rc_all = [i for k in real_conf_keys for i in mine_inf[k]]
    have_real = bool(rp or rc_all)

    def n_over(infs, w):
        return count_over([clip_max(i, w) for i in infs], cutoff)

    cols = ["window", "(a) pos det", "(b) conf FA", "(c) neg clips w/ det"]
    if have_real:
        cols += ["(d) pos det", "(d) conf FA"]
    lines = ["  " + " | ".join(cols)]
    phrases = sorted({phrase_of(p) for p in conf})
    per_phrase = {w: {} for w in windows}
    for w in windows:
        cells = [f"{w:>5}{'*' if w == window else ' '}", f"{n_over(pos_inf, w)}/{len(pos_inf)}",
                 f"{n_over(conf_inf, w)}/{len(conf_inf)}", f"{n_over(neg_inf, w)}/{len(neg_inf)}"]
        if have_real:
            cells += [f"{n_over(rp, w)}/{len(rp)}", f"{n_over(rc_all, w)}/{len(rc_all)}"]
        lines.append("  " + " | ".join(cells))
        for ph in phrases:
            per_phrase[w][ph] = n_over([i for p, i in zip(conf, conf_inf) if phrase_of(p) == ph], w)
        for k in real_conf_keys:
            per_phrase[w][f"real:{k[1]}"] = n_over(mine_inf[k], w)
    if per_phrase[windows[0]]:
        lines.append("  confusable false accepts per phrase (window: counts)")
        for ph in per_phrase[windows[0]]:
            lines.append(f"      {ph:<20} " + "  ".join(f"{w}:{per_phrase[w][ph]}" for w in windows))
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True, help="MWW_ROOT")
    ap.add_argument("--config", help="model json (default: <root>/out/hey_rocky.json)")
    args = ap.parse_args()
    root = Path(args.root).expanduser().resolve()
    config = Path(args.config).expanduser().resolve() if args.config else root / "out" / "hey_rocky.json"
    real_n = read_real_heldout()

    cfg = json.loads(config.read_text())
    cutoff = cfg["micro"]["probability_cutoff"]
    window = cfg["micro"]["sliding_window_size"]
    print(f"model: {config} (cutoff {cutoff}, window {window})")

    pos = sorted((root / "heldout" / "positives").glob("*.wav"))[:20]
    conf = even_share(sorted((root / "heldout" / "confusables").glob("*.wav")), 20)
    if not pos or not conf:
        sys.exit("no held-out clips; run the positives and negatives steps first")

    real = sorted((root / "negative_datasets").rglob("*.wav"))
    if real:
        neg_src = "negative_datasets wavs"
        neg_wavs, total = [], 0.0
        for p in real:
            neg_wavs.append(p)
            total += wav_seconds(p)
            if total >= NEG_SECONDS:
                break
    else:
        neg_src = "SYNTHETIC macOS `say` speech (NOT the real eval set)"
        neg_wavs = synth_negatives(root)

    mine = real_heldout(root, real_n) if (root / "real").is_dir() else None

    with tempfile.TemporaryDirectory() as td:
        raw = load_window1_model(config, cfg, Path(td))
        ceiling = output_ceiling(raw)

        def infer(paths):
            return [stream_inferences(raw, read_wav16k(p)) for p in paths]

        pos_inf = infer(pos)
        conf_inf = infer(conf)
        neg_inf = infer(neg_wavs)
        mine_inf = {}
        if mine:
            for kind, phrases in mine.items():
                for name, wavs in phrases.items():
                    mine_inf[(kind, name)] = infer(wavs)
        raw.close()

    pos_max = [clip_max(i, window) for i in pos_inf]
    conf_max = [clip_max(i, window) for i in conf_inf]
    neg_peaks, neg_s = [], 0.0
    for p, infs in zip(neg_wavs, neg_inf):
        neg_s += wav_seconds(p)
        r = rolling_means(infs, window)
        neg_peaks += peaks_over_time(r if len(r) else np.array([0.0]))
    neg_max = max(neg_peaks, default=0.0)

    det = count_over(pos_max, cutoff)
    conf_fa = count_over(conf_max, cutoff)
    neg_fa = count_over(neg_peaks, cutoff)
    print(f"(a) hey rocky      n={len(pos_max):3d} max={max(pos_max):.3f} "
          f"min={min(pos_max):.3f} detected={det}/{len(pos_max)} "
          f"({100 * det / len(pos_max):.0f}%)")
    print(f"(b) confusables    n={len(conf_max):3d} max={max(conf_max):.3f} "
          f"false accepts={conf_fa}")
    print(f"(c) negatives      {neg_s / 60:.1f} min from {neg_src}: "
          f"max={neg_max:.3f} false accepts={neg_fa}")

    real_pos_max, real_conf_max = [], []
    for line in real_report_lines(root, mine, mine_inf, window, cutoff, real_n):
        print(line)
    for (kind, _), clips in mine_inf.items():
        (real_pos_max if kind == "positives" else real_conf_max).extend(
            clip_max(i, window) for i in clips)

    print(f"\nsliding-window sweep at cutoff {cutoff} (* = manifest window; "
          "counts are clips with any detection)")
    for line in sweep_lines(window, cutoff, conf, pos_inf, conf_inf, neg_inf, mine_inf):
        print(line)

    # Cutoff suggestion
    worst_neg = max([*conf_max, neg_max, *real_conf_max])
    print()
    suggested = suggest_cutoff(worst_neg, ceiling)
    if suggested is None:
        print(f"no probability_cutoff can separate the negatives: the worst "
              f"negative reaches {worst_neg:.3f}, the quantized output ceiling "
              f"({ceiling:.5f}). Try a larger sliding_window_size (see the "
              "sweep above) or retrain.")
    else:
        all_pos = pos_max + real_pos_max
        rate = sum(m > suggested for m in all_pos) / len(all_pos)
        print(f"suggested probability_cutoff: {suggested:g} "
              f"(clears worst negative {worst_neg:.3f}; detects {100 * rate:.0f}% "
              f"of the positives)")
        if rate < 0.8:
            print("warning: suggested cutoff detects under 80% of held-out positives; "
                  "positives and negatives overlap, consider more training or a "
                  "different negative_class_weight")
        print("edit probability_cutoff in hey_rocky.json to apply; no retraining needed")


if __name__ == "__main__":
    main()

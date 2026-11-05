"""Smoke-test the trained hey_rocky model with pymicro-wakeword.

Run with the undertone repo venv (has pymicro-wakeword), not the training venv:
  ~/Projects/personal/undertone/.venv/bin/python smoke_test.py --root $MWW_ROOT

Sets:
  (a) held-out piper "hey rocky" clips        -> should detect
  (b) held-out piper confusable clips         -> should NOT detect
  (c) ~10 min of speech negatives             -> should NOT detect
For (c) it uses wav files under negative_datasets if any exist (the HF
negatives are normally spectrogram features, not wavs); otherwise it
synthesizes speech with macOS `say`, which is NOT the real eval data and is
labelled as such in the output.
"""

import argparse
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures

TARGET_RATE = 16000
CHUNK_BYTES = 320  # 10 ms of 16 kHz int16
PAD_S = 1.0  # silence either side so the strided model flushes
REFRACTORY_S = 2.0
NEG_SECONDS = 600

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


def stream_probs(mww, samples):
    """Feed int16 samples as the voice loop does; return one prob per inference."""
    mww.reset()
    feats = MicroWakeWordFeatures()
    pad = np.zeros(int(PAD_S * TARGET_RATE), dtype=np.int16)
    audio = np.concatenate([pad, samples, pad]).tobytes()
    probs = []
    for i in range(0, len(audio), CHUNK_BYTES):
        for processed in feats.process_streaming(audio[i : i + CHUNK_BYTES]):
            p = mww.process_streaming_prob(processed)
            if p is not None:
                probs.append(p)
    return np.array(probs or [0.0])


def clip_max_probs(mww, paths):
    return [float(stream_probs(mww, read_wav16k(p)).max()) for p in paths]


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


def even_share(paths, limit):
    """Up to `limit` clips, round-robin across phrases (name prefix before the
    last '_'), so every confusable phrase is represented; all if fewer."""
    if len(paths) <= limit:
        return list(paths)
    groups = {}
    for p in paths:
        groups.setdefault(p.name.rsplit("_", 1)[0], []).append(p)
    picked, i = [], 0
    while len(picked) < limit:
        for g in groups.values():
            if i < len(g) and len(picked) < limit:
                picked.append(g[i])
        i += 1
    return picked


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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True, help="MWW_ROOT")
    ap.add_argument("--config", help="model json (default: <root>/out/hey_rocky.json)")
    args = ap.parse_args()
    root = Path(args.root).expanduser().resolve()
    config = Path(args.config) if args.config else root / "out" / "hey_rocky.json"

    mww = MicroWakeWord.from_config(config)
    cutoff = mww.probability_cutoff
    print(f"model: {config} (cutoff {cutoff}, window {mww.sliding_window_size})")

    pos = sorted((root / "heldout" / "positives").glob("*.wav"))[:20]
    conf = even_share(sorted((root / "heldout" / "confusables").glob("*.wav")), 20)
    if not pos or not conf:
        sys.exit("no held-out clips; run the positives and negatives steps first")

    pos_max = clip_max_probs(mww, pos)
    conf_max = clip_max_probs(mww, conf)

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
    neg_peaks, neg_s = [], 0.0
    for p in neg_wavs:
        s = read_wav16k(p)
        neg_s += len(s) / TARGET_RATE
        neg_peaks += peaks_over_time(stream_probs(mww, s))
    neg_max = max(neg_peaks, default=0.0)

    det = sum(m > cutoff for m in pos_max)
    conf_fa = sum(m > cutoff for m in conf_max)
    neg_fa = sum(m > cutoff for m in neg_peaks)
    print(f"(a) hey rocky      n={len(pos_max):3d} max={max(pos_max):.3f} "
          f"min={min(pos_max):.3f} detected={det}/{len(pos_max)} "
          f"({100 * det / len(pos_max):.0f}%)")
    print(f"(b) confusables    n={len(conf_max):3d} max={max(conf_max):.3f} "
          f"false accepts={conf_fa}")
    print(f"(c) negatives      {neg_s / 60:.1f} min from {neg_src}: "
          f"max={neg_max:.3f} false accepts={neg_fa}")

    worst_neg = max(max(conf_max), neg_max)
    suggested = min(0.99, round(worst_neg + 0.02, 2))
    rate = sum(m > suggested for m in pos_max) / len(pos_max)
    print(f"suggested probability_cutoff: {suggested:.2f} "
          f"(clears worst negative {worst_neg:.3f}; detects {100 * rate:.0f}% of (a))")
    if rate < 0.8:
        print("warning: suggested cutoff detects under 80% of held-out positives; "
              "positives and negatives overlap, consider more training or a "
              "different negative_class_weight")
    print("edit probability_cutoff in hey_rocky.json to apply; no retraining needed")


if __name__ == "__main__":
    main()

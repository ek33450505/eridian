# microWakeWord training setup

Unit A of training a custom "hey rocky" wake word model for
`pymicro-wakeword` (`MicroWakeWord.from_config`). `setup.sh` builds the
training environment and downloads the datasets, following the official
[basic training notebook](https://github.com/kahrendt/microWakeWord/blob/main/notebooks/basic_training_notebook.ipynb).

Everything lands under `MWW_ROOT` (default
`~/Datasets/undertone-training/mww`), never in this repo. Caches (HF, uv,
torch) live in `$MWW_ROOT/cache`, not `~/.cache`.

## Run

    bash training/microwakeword/setup.sh

- Idempotent: rerun to resume; finished steps are skipped.
- Needs `uv`, `git`, `curl`, `unzip`; refuses to start under 60 GB free.
  `MWW_ROOT` must be absolute and strictly under `$HOME` (not `$HOME` itself).
  `setup.sh` alone creates the root and its `.undertone-mww-root` sentinel,
  and only for a new or empty dir; a non-empty dir without the sentinel is
  refused, and `train.sh` requires the sentinel. All deletes go through
  `lib.sh` and need it. `setup.sh --check-root` / `train.sh --check-root`
  validate the root and exit without creating anything.
- Disk use: about 25-40 GB.
- Log: `$MWW_ROOT/logs/setup.log` (includes the microWakeWord commit).
- Optional: `MWW_REF=<git ref>` checks out that ref of microWakeWord and
  redoes its pip install if the existing clone differs. Unset, the clone is
  left as is (it is not pinned).
- Audio decoding uses `soundfile` and `datasets<4`; no ffmpeg needed.

## Licenses

- microWakeWord: Apache-2.0.
- HF negatives (kahrendt/microwakeword): CC-BY-NC-4.0.
- AudioSet: mixed per-clip licenses, YouTube-sourced.
- FMA: mixed Creative Commons licenses.
- MIT RIRs: license unknown per its dataset card.
- So the trained model is for personal use only. Do not redistribute it.

## Deviations from the notebook

- AudioSet: one parquet shard (`data/bal_train/09.parquet`), since the
  notebook's `bal_train09.tar` URL no longer resolves.
- 5000 "hey rocky" positives (notebook: 1000), plus phonetic variants and
  confusable negatives ("hey ricky", "rocky road", ...) as an extra feature set.
- `negative_class_weight` 60 (notebook: 20), to push down false wakes on
  near-miss phrases.
- `minimization_metric` is null, as in the notebook. The alternative,
  `ambient_false_positives_per_hour`, needs a `validation_ambient` split that
  `dinner_party_eval` is not confirmed to provide, so it is not used.
- Audio decoding uses `soundfile` and `datasets<4` instead of ffmpeg/torchcodec.
- `pymicro-features==2.0.2` from PyPI (universal2 wheel) replaces the notebook's
  macOS fork, which no longer builds with current clang; it matches undertone's
  runtime frontend.
- `tensorboard>=2.18` is installed explicitly: TF 2.21 no longer bundles it, but
  microWakeWord's training calls `tf.summary`.

## Train

    bash training/microwakeword/train.sh

Run `setup.sh` first. Steps, in order: `positives` (5000 "hey rocky" plus
phonetic variants), `negatives` (5000 confusables over 10 phrases, 500 each:
"hey ricky", "hey rock", "hey rocks", "hey rocket", "hey rocco", "hey lucky",
"rocky road", ...), `features`, `yaml`, `train`, `install`, `smoke`.

- Resume: finished steps are skipped on rerun. `train.sh --from <step>` forces
  that step and every later one (e.g. `--from train` after editing
  `training.yaml.in`; `--from positives` after changing sample counts).
  Any forced or follow-on run of `features` deletes the old feature mmaps
  first, so stale features are never reused.
- `hey_rocky.json` is written only if missing, so a tuned cutoff survives
  reruns; `--force-manifest` rewrites it.
- Tunables (env): `MWW_ROOT`, `PIPER_BATCH` (default 50), `POSITIVE_COUNT`,
  `VARIANT_COUNT`, `CONFUSABLE_COUNT` (default 5000), `HELDOUT` (held-out
  clips per set, default 20), `REAL_CLIPS`, `REAL_REPEAT`, `REAL_HELDOUT`
  (see "Your own clips").
- The default manifest uses `sliding_window_size` 10 (not 5). Inference runs
  every 30 ms (stride 3 x 10 ms), so 5 more window slots add about 150 ms of
  wake latency. In an offline sweep on 2026-11-05 over synthetic training-set
  clips (`smoke_test.py` now prints this sweep), the larger window kept 20/20
  held-out positives and cut false wakes (hey rock 58/60 to 21/60, hey rocco
  7/60 to 0, hey lucky 2/60 to 0). `--force-manifest` resets a hand-tuned
  cutoff and window.
- Retrain v2: `bash training/microwakeword/train.sh --from negatives` for the
  first v2 run, which never used real clips. Whenever `REAL_CLIPS` changed since
  the last run (on to off included), use `--from positives` instead so stale
  `real_*` copies are dropped; it is cheap because existing generated clips are
  reused.
- Log: `$MWW_ROOT/logs/train.log`.
- Outputs: `$MWW_ROOT/out/hey_rocky.tflite` and `hey_rocky.json`.
- Held-out clips for the smoke test: `$MWW_ROOT/heldout/`.

### Tune the cutoff without retraining

`smoke_test.py` prints the max probability per set, the detection rate on
held-out "hey rocky", false accepts on confusables and speech, and a suggested
`probability_cutoff`. Rerun it any time:

    ~/Projects/personal/undertone/.venv/bin/python -I \
        training/microwakeword/smoke_test.py --root ~/Datasets/undertone-training/mww

Edit `micro.probability_cutoff` in `hey_rocky.json` (higher = fewer false
accepts, more misses) and rerun. If the speech set is synthetic `say` audio the
output says so; treat that number as a rough check only.

### Your own clips

Synthetic voices miss how you actually say the phrases. Record real ones and
train on them:

1. Stop undertone first, or saying the wake word 50 times wakes it:
   `~/Projects/personal/undertone/.venv/bin/undertone stop`
2. Record (about 50 takes per phrase):

       ~/Projects/personal/undertone/.venv/bin/python -I training/microwakeword/record_clips.py

   Clips land in `$MWW_ROOT/real/positives/hey_rocky/` and
   `$MWW_ROOT/real/confusables/<phrase>/` (hey_rock, hey_rocks, hey_rocco,
   hey_rocket, hey_lucky) as 16 kHz mono int16 `NNNN.wav`.
3. Evaluate with `smoke_test.py`: it reports your held-out real clips and a
   sliding-window sweep. The first `REAL_HELDOUT` (default 10) clips of each
   phrase are eval-only; `train.sh` never copies them.
4. Train with them:

       REAL_CLIPS=1 bash training/microwakeword/train.sh --from positives

   Each non-held-out clip is added `REAL_REPEAT` times (default 10; augmentation
   randomises every copy). Changing `REAL_CLIPS` needs `--from positives` so
   positives and confusables are rebuilt. With `REAL_CLIPS=1`, a kind with no
   usable clips only warns (e.g. positives recorded, confusables not);
   `train.sh` stops with a pointer to `record_clips.py` only if neither kind
   has any. Without `--from`, finished steps are skipped and real clips are not
   added (it warns).

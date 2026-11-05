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
- `negative_class_weight` 30 (notebook: 20).
- `minimization_metric` is null, as in the notebook. The alternative,
  `ambient_false_positives_per_hour`, needs a `validation_ambient` split that
  `dinner_party_eval` is not confirmed to provide, so it is not used.
- Audio decoding uses `soundfile` and `datasets<4` instead of ffmpeg/torchcodec.

## Train

    bash training/microwakeword/train.sh

Run `setup.sh` first. Steps, in order: `positives` (5000 "hey rocky" plus
phonetic variants), `negatives` (about 2000 confusables such as "hey ricky",
"rocky road"), `features`, `yaml`, `train`, `install`, `smoke`.

- Resume: finished steps are skipped on rerun. `train.sh --from <step>` forces
  that step and every later one (e.g. `--from train` after editing
  `training.yaml.in`; `--from positives` after changing sample counts).
  Any forced or follow-on run of `features` deletes the old feature mmaps
  first, so stale features are never reused.
- `hey_rocky.json` is written only if missing, so a tuned cutoff survives
  reruns; `--force-manifest` rewrites it.
- Tunables (env): `MWW_ROOT`, `PIPER_BATCH` (default 50), `POSITIVE_COUNT`,
  `VARIANT_COUNT`, `CONFUSABLE_COUNT`, `HELDOUT` (held-out clips per set,
  default 20).
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

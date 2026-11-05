# Custom Wake-Phrase Training

## Why It's Optional

The default "hey jarvis" wake phrase ships pretrained in pymicro-wakeword (built-in model, Apache-2.0 licensed code). **No training is required** to use eridian.

If you want to train a custom phrase (e.g., "hey eridian", "alexa", "hey claude"), the `training/` Docker image provides an optional environment for that work.

## What the Image Is

A Docker image for training custom wake-word models using openWakeWord 0.6.0 on CPU (linux/aarch64 only).

**Provides:**
- Synthetic training data generation via piper-sample-generator v2.0.0 (100% synthetic TTS clips, no voice recordings of you)
- Custom wake-word model training with openWakeWord
- ONNX export for trained models

**Image specs:**
- Architecture: linux/aarch64 (ARM64; will not work on x86_64 without multi-platform setup)
- Size: **3.58 GB** (PyTorch + dependencies dominate)
- Compute: CPU-only (no CUDA/GPU support; training will be slow)
- Base OS: Python 3.10, Debian Bookworm

## Build and Smoke Test

### Build the Image

```bash
docker build -t eridian-oww:latest training/
```

**Time:** 5–10 minutes (first build; subsequent builds use layer cache)

### Run the Smoke Test

```bash
bash training/smoke_test.sh
```

This script will:
1. Build the image if not present
2. Test Python imports (torch, piper-phonemize, audiomentations, datasets, etc.)
3. Generate 10 synthetic audio clips with text "hey eridian"
4. Verify all 10 .wav files were created

**Expected result:** ~30–60 seconds, exit code 0, 10 .wav files in `/tmp/smoke_test/` inside the container.

## Data Locations

Example from Ed's machine (2026-10-01):

| Item | Location | Size |
|---|---|---|
| Negative features (adversarial background) | `~/Datasets/openwakeword/` | ~17 GB |
| Training work directory | `~/Datasets/eridian-training/` | varies |
| Docker TTS model | (downloaded at build, cached in image) | ~600 MB |
| Feature models (embedding + melspectrogram) | (downloaded at build, cached in image) | ~50 MB |

Mount your data directory at `/work/data` and output directory at `/work/models` when running the image:

```bash
docker run --rm -it \
  -v ~/Datasets/eridian-training:/work/data \
  -v ~/Datasets/eridian-training/models:/work/models \
  eridian-oww:latest \
  bash
```

Inside the container:
- openWakeWord code: `/opt/openwakeword` (editable install)
- TTS model: `/opt/piper-sample-generator/models/en_US-libritts_r-medium.pt`
- Feature models: `/opt/openwakeword/openwakeword/resources/models/` (nested per package structure)
- Training entrypoint: `python /opt/openwakeword/openwakeword/train.py`

## License Warning: NON-COMMERCIAL

🔴 **IMPORTANT:** openWakeWord's code is Apache-2.0, but **any trained model is CC BY-NC-SA 4.0 (non-commercial).**

CC BY-NC-SA 4.0 permits non-commercial use, such as a personal assistant, and forbids commercial use. It also requires attribution and share-alike. Read the license text before using a trained model anywhere beyond personal use.

This applies only to models you train with this image. eridian's default "hey jarvis" model comes from pymicro-wakeword, not openWakeWord.

## Network

This image is the only part of the repo that downloads training data. It does so at two points.

**At build time:**
- Git clones openWakeWord v0.6.0 and piper-sample-generator v2.0.0 from GitHub
- Wget downloads TTS model (en_US-libritts_r-medium.pt, ~600 MB) from GitHub releases
- Wget downloads feature models (embedding_model.onnx, melspectrogram.onnx, ~50 MB) from GitHub releases

**During training:** datasets and phonemizer data are downloaded, as your training script specifies.

eridian's runtime (`eridian chat`, `listen`, `serve`) never downloads training data.

## Known Nits

### Unpinned Dependencies

The Dockerfile pins most dependencies but installs `scipy` and `pyyaml` unpinned (`training/Dockerfile` lines 58–59), so a rebuild picks up whatever versions are latest. That hasn't caused problems so far. If a future rebuild breaks, pin both to the versions in a known-good image.

### Model Path Changes (Fixed)

Earlier versions had models in `/opt/openwakeword/resources/models/` (flat), but the package structure requires `/opt/openwakeword/openwakeword/resources/models/` (nested). The Dockerfile corrects this; the smoke test verifies the paths.

### Image Size

3.58 GB is large (PyTorch + dependencies dominate). For production inference of a trained model, extract the ONNX file to a minimal inference container.

## Custom Model Integration: Bring Your Own

A custom wake phrase trained in this image remains separate from eridian's default. To use it:

1. Export the trained model as ONNX (openWakeWord handles this)
2. Wire it into eridian. **This is not integrated, and it isn't on the current roadmap.** Phase 4 only mentions an optional second-stage verifier model, if "hey jarvis" turns out to false-accept.

Currently, eridian uses pymicro-wakeword's built-in "Hey Jarvis" model. A custom model would need:
- Integration point in `eridian/cli.py:_make_wake_factory()`
- ONNX runtime setup (currently only pymicro-wakeword is loaded)
- Model path configuration (via env var or config file)

The training image works today. Wiring a trained model into eridian is not yet planned.

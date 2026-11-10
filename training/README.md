# OpenWakeWord Training Docker Image

A Docker image for training custom wake-word models (optional — eridian's default "hey jarvis" ships pretrained with pymicro-wakeword) using [openWakeWord](https://github.com/dscripka/openWakeWord) v0.6.0 on CPU (ARM64/aarch64 architecture).

## What This Is

This image provides a complete, CPU-only environment for:
- Generating synthetic training data using [piper-sample-generator](https://github.com/rhasspy/piper-sample-generator) v2.0.0
- Training custom wake-word detection models with openWakeWord
- Exporting trained models to ONNX format (TFLite conversion intentionally omitted)

The image is optimized for ARM64 (linux/aarch64) and runs on machines with no GPU (CPU-only inference).

## Building

```bash
docker build -t eridian-oww:latest training/
```

**Build time:** ~5-10 minutes (first build; subsequent builds use layer caching)
**Image size:** **3.58 GB** (significantly reduced via dependency reordering — see Deviations below)

**Trust:** the image pins the openWakeWord and piper-sample-generator git checkouts to commits (`training/Dockerfile:19-24`) and checks the piper model and the openWakeWord feature models against SHA-256 (`sha256sum -c`). These pins are trust-on-first-use from 2026-11-09, not training-proven. pip packages are not hash-pinned, and `scipy` and `pyyaml` are unpinned (`training/Dockerfile:65-66`). The Docker training scripts' own runtime downloads were not reviewed. (The separate microWakeWord path, `training/microwakeword/`, SHA-256 checks its dataset downloads.)

## Running the Smoke Test

Verify the image is working:

```bash
bash training/smoke_test.sh
```

This will:
1. Build the image if not present
2. Test Python imports (torch, piper-phonemize, audiomentations, datasets, etc.)
3. Generate 10 synthetic audio clips with text "hey eridian"
4. Verify all 10 .wav files were created

Expected output: ~30-60 seconds, exit code 0, 10 .wav files in `/tmp/smoke_test/` inside the container.

## Using the Image for Training

```bash
docker run --rm -it \
  -v /path/to/training/data:/work/data \
  -v /path/to/output/models:/work/models \
  eridian-oww:latest \
  bash
```

Inside the container:
- Code is at `/opt/openwakeword` (editable install)
- TTS model at `/opt/piper-sample-generator/models/en_US-libritts_r-medium.pt`
- Feature models at `/opt/openwakeword/openwakeword/resources/models/` (note: nested path due to package structure)
- Training entrypoint: `python /opt/openwakeword/openwakeword/train.py`

## Pinned Versions & Deviations from Upstream

### Why These Pins

| Package | Version | Reason |
|---------|---------|--------|
| **torch** | 2.1.2 | CPU-only, confirmed aarch64 wheel; pinned first in Dockerfile to prevent re-download |
| **torchaudio** | 2.1.2 | Matches torch; CPU-only; installed alongside torch to prevent transitive downloads |
| **numpy** | <2 | Required by piper-sample-generator v2.0.0 and torch 2.1.2 |
| **piper-phonemize** | 1.1.0 | Last version with aarch64 manylinux_2_28 wheels for cp310 |
| **webrtcvad** | 2.0.10 | Pinned available version (v0.4.4 never existed on PyPI) |
| **deep-phonemizer** | 0.0.19 | Required by openWakeWord's data augmentation pipeline (data.py line ~941) |
| **datasets** | 2.14.6 | Upstream Colab pin; newer may introduce incompatibilities |
| **pyarrow** | 14.0.2 | Compatible with datasets 2.14.6 on aarch64 |
| **fsspec** | 2023.10.0 | datasets 2.14.6 requires fsspec <=2023.10.0 |

### Omissions (by design)

- **TensorFlow** — Skipped entirely (TF 2.8.1 has no linux/aarch64 wheels; TFLite conversion disabled per spec)
- **tensorflow_probability** — Only in openWakeWord's 'full' extras; not needed for ONNX export
- **onnx_tf** — TensorFlow dependency; unnecessary

### Deviations from Upstream Colab

| Package | Upstream | Image | Why |
|---------|----------|-------|-----|
| webrtcvad | 0.4.4 | 2.0.10 | v0.4.4 never existed on PyPI; using latest available |
| deep-phonemizer | optional | 0.0.19 | Now required: data augmentation in training calls `dp.phonemizer.Phonemizer` |
| Dockerfile structure | N/A | reordered | **Critical fix**: torch/torchaudio installed FIRST (before piper requirements) to prevent re-downloading latest PyTorch. Reduced image size from 12.3 GB → 3.58 GB. |
| Model paths | N/A | nested | **Critical fix**: embedding_model.onnx and melspectrogram.onnx moved to `/opt/openwakeword/openwakeword/resources/models/` (nested path per package structure). Old path caused ONNXRuntimeError in AudioFeatures(). |

## System Dependencies

Minimal `apt-get` installation:
- `git` — for cloning repos
- `curl`, `wget` — downloading model files
- `libsndfile1` — audio I/O (via librosa)
- `ffmpeg` — audio codec support
- `build-essential`, `python3-dev` — compiling webrtcvad and other C extensions

All Python dependencies installed via pip (no system python packages).

## Notes

- **Docker platform:** Built for `linux/arm64` (ARM64/aarch64). Will not work on x86_64 without multi-platform build setup.
- **Non-production:** 3.58 GB image size is dominated by PyTorch + large dependency tree. Not suitable for production inference; for deployment, extract trained ONNX models to a minimal inference container.
- **CPU-only:** No CUDA/GPU support. Training will be slow; suitable for development and small datasets.
- **Permissions:** Running container as root (pip install --no-cache-dir in Dockerfile). For production, add a non-root user.
- **Model downloads:** TTS model (~600 MB) and feature models (~50 MB total) are downloaded at build time. Slow internet will extend build time.
- **Smoke test coverage:** Hardened smoke test now verifies (1) AudioFeatures instantiation, (2) feature extraction via embed_clips(), and (3) deep-phonemizer import. These tests catch model path errors and missing dependencies that would only fail during actual training.

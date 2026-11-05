#!/usr/bin/env bash
# Unit A: set up a microWakeWord training environment and datasets for a custom
# "hey rocky" wake word. Mirrors kahrendt/microWakeWord's basic_training_notebook.
# Idempotent: every step skips if its output exists, so a rerun resumes.
#
# Env overrides:
#   MWW_ROOT  install root; absolute, strictly under $HOME (see lib.sh). `--check-root` validates it and exits.
#            setup.sh is the only script that creates the root/sentinel, and only
#            for a new or empty dir; a non-empty dir without the sentinel is refused.
#   MWW_REF   git ref of kahrendt/microWakeWord to check out. If the existing
#             clone is at a different commit, it is checked out and the core pip
#             step is redone. Unset: the existing clone is left as is.
#
# Decode path: no ffmpeg/torchcodec. `datasets<4` (only used to stream the MIT
# RIRs, as the notebook does) and soundfile (libsndfile >= 1.1 reads mp3/flac)
# for AudioSet parquet and FMA mp3 decoding.
#
# Data licensing: the downloaded audio is mixed-license; any model trained with
# it is for non-commercial personal use only.
set -euo pipefail

MWW_REF="${MWW_REF:-}"
MIN_FREE_KB=$((60 * 1024 * 1024))

# Root guard, sentinel and safe_rm_* helpers live in lib.sh.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"
mww_init setup "$@"

NOTEBOOK_URL="https://raw.githubusercontent.com/kahrendt/microWakeWord/main/notebooks/basic_training_notebook.ipynb"
MWW_REPO="https://github.com/kahrendt/microWakeWord"
PIPER_REPO="https://github.com/kahrendt/piper-sample-generator"
PIPER_MODEL_URL="https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt"
# Trust-on-first-use: hashed from the official rhasspy v2.0.0 release on 2026-11-04,
# because upstream publishes no checksum. train.sh unpickles this file, so it is
# verified here and again before every generation run.
PIPER_MODEL_SHA256="e95ee53770bf598c354a6e6dbfc95ccb259aeeb501d35a86be8a767429ab0ff6"
# The PyPI universal2 wheel replaces the notebook's macOS fork, which no longer builds with current clang; it is the version pymicro-wakeword pulls in for eridian's runtime.
PYMICRO_FEATURES_SPEC="pymicro-features==2.0.2"
AUDIO_METADATA_SPEC="git+https://github.com/whatsnowplaying/audio-metadata@d4ebb238e6a401bb1a5aaaac60c9e2b3cb30929f"
AUDIOSET_URL="https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/bal_train/09.parquet"
FMA_URL="https://huggingface.co/datasets/mchl914/fma_xsmall/resolve/main/fma_xs.zip"
NEG_URL_ROOT="https://huggingface.co/datasets/kahrendt/microwakeword/resolve/main"
NEG_FILES=(dinner_party.zip dinner_party_eval.zip no_speech.zip speech.zip)

VENV="$MWW_ROOT/venv"
PY="$VENV/bin/python"
LOG="$MWW_ROOT/logs/setup.log"

mkdir -p "$MWW_ROOT/logs" "$MWW_ROOT/cache"

# Keep every cache under MWW_ROOT; nothing lands in ~/.cache.
export HF_HOME="$MWW_ROOT/cache/hf"
export HF_DATASETS_CACHE="$MWW_ROOT/cache/hf/datasets"
export UV_CACHE_DIR="$MWW_ROOT/cache/uv"
export TORCH_HOME="$MWW_ROOT/cache/torch"
mkdir -p "$HF_HOME" "$HF_DATASETS_CACHE" "$UV_CACHE_DIR" "$TORCH_HOME"

log() {
	local line
	line="$(date '+%Y-%m-%dT%H:%M:%S%z') $*"
	printf '%s\n' "$line" | tee -a "$LOG"
}

die() {
	log "ERROR: $*"
	exit 1
}

# Download to a .part file (resumable), then rename.
fetch() {
	local url="$1" dest="$2"
	if [[ -s "$dest" ]]; then
		log "skip download (exists): $dest"
		return 0
	fi
	log "download: $url"
	curl -fL --retry 3 -C - -o "$dest.part" "$url"
	mv "$dest.part" "$dest"
}

# Download a zip into downloads/ and validate it; a corrupt one is removed and
# fetched once more before giving up. $2 is a bare filename.
fetch_zip() {
	local url="$1" name="$2" dest="$MWW_ROOT/downloads/$2"
	fetch "$url" "$dest"
	if ! unzip -tq "$dest" >/dev/null 2>&1; then
		log "corrupt zip, refetching: $name"
		safe_rm_download "$name"
		fetch "$url" "$dest"
		unzip -tq "$dest" >/dev/null 2>&1 || die "zip still corrupt after refetch: $name"
	fi
}

pip_install() {
	uv pip install --python "$PY" "$@" 2>&1 | tee -a "$LOG"
}

# --- 0. preflight ---------------------------------------------------------
log "MWW_ROOT=$MWW_ROOT"
for tool in uv git curl unzip df awk; do
	command -v "$tool" >/dev/null 2>&1 || die "missing required tool: $tool"
done

df_out="$(df -k "$MWW_ROOT")" || die "df failed for $MWW_ROOT; cannot check free disk"
free_kb="$(printf '%s\n' "$df_out" | awk 'NR==2 {print $4}')"
[[ "$free_kb" =~ ^[0-9]+$ ]] || die "could not parse free disk space from df output for $MWW_ROOT"
if ((free_kb < MIN_FREE_KB)); then
	die "only $((free_kb / 1024 / 1024)) GB free under $MWW_ROOT; need at least 60 GB"
fi
log "free disk: $((free_kb / 1024 / 1024)) GB"

# --- 1. venv --------------------------------------------------------------
if [[ -x "$PY" ]]; then
	log "skip venv (exists): $VENV"
else
	log "create venv (python 3.11)"
	uv venv --python 3.11 "$VENV"
fi

# --- 2. notebook copy -----------------------------------------------------
if [[ -s "$MWW_ROOT/notebook.ipynb" ]]; then
	log "skip notebook copy (exists)"
else
	log "save notebook copy"
	curl -fsSL --retry 3 -o "$MWW_ROOT/notebook.ipynb.part" "$NOTEBOOK_URL"
	mv "$MWW_ROOT/notebook.ipynb.part" "$MWW_ROOT/notebook.ipynb"
fi

# --- 3. microWakeWord code ------------------------------------------------
MWW_DIR="$MWW_ROOT/microWakeWord"
if [[ ! -d "$MWW_DIR/.git" ]]; then
	log "clone microWakeWord"
	git clone "$MWW_REPO" "$MWW_DIR" 2>&1 | tee -a "$LOG"
	if [[ -n "$MWW_REF" ]]; then
		log "checkout MWW_REF=$MWW_REF"
		git -C "$MWW_DIR" checkout "$MWW_REF" 2>&1 | tee -a "$LOG"
		safe_rm_subdir .done-pip-core
	fi
else
	log "skip clone (exists): $MWW_DIR"
	if [[ -n "$MWW_REF" ]]; then
		want="$(git -C "$MWW_DIR" rev-parse --verify --quiet "$MWW_REF^{commit}" || true)"
		if [[ -z "$want" ]]; then
			git -C "$MWW_DIR" fetch --all --tags 2>&1 | tee -a "$LOG"
			want="$(git -C "$MWW_DIR" rev-parse --verify --quiet "$MWW_REF^{commit}" || true)"
		fi
		[[ -n "$want" ]] || die "MWW_REF '$MWW_REF' not found in $MWW_DIR"
		have="$(git -C "$MWW_DIR" rev-parse HEAD)"
		if [[ "$want" != "$have" ]]; then
			log "MWW_REF differs (have $have, want $want); checking out and redoing core pip step"
			git -C "$MWW_DIR" checkout "$MWW_REF" 2>&1 | tee -a "$LOG"
			safe_rm_subdir .done-pip-core
		fi
	fi
fi
log "microWakeWord commit: $(git -C "$MWW_DIR" rev-parse HEAD)"

if [[ -e "$MWW_ROOT/.done-pip-core" ]]; then
	log "skip core pip install (marker exists)"
else
	log "pip install: tensorflow, numpy, notebook forks, microWakeWord"
	# TF >= 2.18 (arm64 wheel) with numpy 2+; installed before microWakeWord
	# so its requirements resolve against them.
	pip_install 'tensorflow>=2.18' 'numpy>=2'
	# TF 2.21 (and possibly earlier) no longer pulls in tensorboard, but microWakeWord's training calls tf.summary.
	pip_install 'tensorboard>=2.18'
	# pymicro-features from PyPI (see above); audio-metadata fork as in the notebook.
	pip_install "$PYMICRO_FEATURES_SPEC"
	pip_install "$AUDIO_METADATA_SPEC"
	pip_install -e "$MWW_DIR"
	# Data-conversion deps. datasets<4: v4 decodes audio via torchcodec/ffmpeg.
	pip_install 'datasets<4' scipy tqdm pyarrow soundfile
	touch "$MWW_ROOT/.done-pip-core"
fi

# --- 4. piper-sample-generator -------------------------------------------
PIPER_DIR="$MWW_ROOT/piper-sample-generator"
if [[ ! -d "$PIPER_DIR/.git" ]]; then
	log "clone piper-sample-generator (mps-support)"
	git clone -b mps-support "$PIPER_REPO" "$PIPER_DIR" 2>&1 | tee -a "$LOG"
else
	log "skip clone (exists): $PIPER_DIR"
fi
log "piper-sample-generator commit: $(git -C "$PIPER_DIR" rev-parse HEAD)"
mkdir -p "$PIPER_DIR/models"
PIPER_MODEL="$PIPER_DIR/models/en_US-libritts_r-medium.pt"
fetch "$PIPER_MODEL_URL" "$PIPER_MODEL"
# Checked on every run, including when the file was already present.
piper_sum="$(shasum -a 256 "$PIPER_MODEL")" || die "cannot hash $PIPER_MODEL"
if [[ "${piper_sum%% *}" != "$PIPER_MODEL_SHA256" ]]; then
	safe_rm_subdir "piper-sample-generator/models/en_US-libritts_r-medium.pt"
	die "piper model sha256 mismatch (got ${piper_sum%% *}); removed it, rerun to refetch"
fi
log "piper model sha256 ok"

if [[ -e "$MWW_ROOT/.done-pip-piper" ]]; then
	log "skip piper pip install (marker exists)"
else
	log "pip install: torch torchaudio piper-phonemize-cross"
	pip_install torch torchaudio piper-phonemize-cross==1.2.1
	touch "$MWW_ROOT/.done-pip-piper"
fi

# --- 5. data --------------------------------------------------------------
DATA_TMP="$MWW_ROOT/downloads"
mkdir -p "$DATA_TMP"

# MIT RIRs: the notebook pulls these via HF datasets and writes 16-bit wavs.
if [[ -e "$MWW_ROOT/.done-mit_rirs" ]]; then
	log "skip mit_rirs (done)"
else
	log "build mit_rirs"
	mkdir -p "$MWW_ROOT/mit_rirs"
	"$PY" -I - "$MWW_ROOT/mit_rirs" <<'PYEOF' 2>&1 | tee -a "$LOG"
import os, sys
import datasets, scipy.io.wavfile
import numpy as np
from tqdm import tqdm

out = sys.argv[1]
ds = datasets.load_dataset("davidscripka/MIT_environmental_impulse_responses", split="train", streaming=True)
ds = ds.cast_column("audio", datasets.Audio(sampling_rate=16000))
n = 0
for row in tqdm(ds):
    name = row["audio"]["path"].split("/")[-1]
    pcm = (np.clip(row["audio"]["array"], -1.0, 1.0) * 32767).astype(np.int16)
    scipy.io.wavfile.write(os.path.join(out, name), 16000, pcm)
    n += 1
if n == 0:
    sys.exit("mit_rirs: no rows written")
print(f"mit_rirs: wrote {n} files")
PYEOF
	touch "$MWW_ROOT/.done-mit_rirs"
fi

# Shared helper source: decode bytes/file with soundfile -> mono 16 kHz int16.
read -r -d '' PY_COMMON <<'PYEOF' || true
import io, math, os, sys
import numpy as np
import scipy.io.wavfile
import scipy.signal
import soundfile as sf

def to_16k_int16(src):
    data, sr = sf.read(src, dtype="float32", always_2d=True)
    data = data.mean(axis=1)
    if sr != 16000:
        g = math.gcd(int(sr), 16000)
        data = scipy.signal.resample_poly(data, 16000 // g, int(sr) // g)
    return (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
PYEOF

# AudioSet: one shard (bal_train/09.parquet), audio bytes -> 16 kHz wav.
if [[ -e "$MWW_ROOT/.done-audioset_16k" ]]; then
	log "skip audioset_16k (done)"
else
	fetch "$AUDIOSET_URL" "$DATA_TMP/audioset_bal_train_09.parquet"
	log "convert audioset parquet -> audioset_16k"
	mkdir -p "$MWW_ROOT/audioset_16k"
	"$PY" -I -c "$PY_COMMON
import pyarrow as pa
import pyarrow.parquet as pq

src, out = sys.argv[1], sys.argv[2]
pf = pq.ParquetFile(src)
print('audioset parquet columns:', [(f.name, str(f.type)) for f in pf.schema_arrow])

audio_col = None
for f in pf.schema_arrow:
    if pa.types.is_struct(f.type) and 'bytes' in [sub.name for sub in f.type]:
        audio_col = f.name
        break
if audio_col is None:
    sys.exit('audioset: no audio-bytes struct column found; see column list above')
print('audioset: using audio column', audio_col)

n = bad = 0
for batch in pf.iter_batches(batch_size=64, columns=[audio_col]):
    for cell in batch.column(0).to_pylist():
        raw = cell.get('bytes') if cell else None
        if not raw:
            bad += 1
            continue
        name = os.path.basename(cell.get('path') or '') or f'audioset_{n}'
        name = os.path.splitext(name)[0] + '.wav'
        try:
            pcm = to_16k_int16(io.BytesIO(raw))
        except (sf.SoundFileError, RuntimeError, ValueError) as exc:
            bad += 1
            print('skip undecodable clip:', exc)
            continue
        scipy.io.wavfile.write(os.path.join(out, name), 16000, pcm)
        n += 1
print(f'audioset_16k: wrote {n} files, skipped {bad}')
if n == 0:
    sys.exit('audioset: nothing converted')
" "$DATA_TMP/audioset_bal_train_09.parquet" "$MWW_ROOT/audioset_16k" 2>&1 | tee -a "$LOG"
	touch "$MWW_ROOT/.done-audioset_16k"
fi

# FMA xsmall: mp3 -> 16 kHz wav, decoded with soundfile (no ffmpeg).
if [[ -e "$MWW_ROOT/.done-fma_16k" ]]; then
	log "skip fma_16k (done)"
else
	fetch_zip "$FMA_URL" fma_xs.zip
	if [[ ! -d "$MWW_ROOT/fma/fma_small" ]]; then
		log "extract fma"
		safe_rm_subdir .tmp-extract-fma
		mkdir -p "$MWW_ROOT/.tmp-extract-fma"
		unzip -q -o "$DATA_TMP/fma_xs.zip" -d "$MWW_ROOT/.tmp-extract-fma"
		[[ -d "$MWW_ROOT/.tmp-extract-fma/fma_small" ]] || die "fma_xs.zip did not contain fma_small/"
		safe_rm_subdir fma
		mv "$MWW_ROOT/.tmp-extract-fma" "$MWW_ROOT/fma"
	fi
	log "convert fma -> fma_16k"
	mkdir -p "$MWW_ROOT/fma_16k"
	"$PY" -I -c "$PY_COMMON
from pathlib import Path
from tqdm import tqdm

src, out = sys.argv[1], sys.argv[2]
files = sorted(Path(src).glob('**/*.mp3'))
n = bad = 0
for p in tqdm(files):
    try:
        pcm = to_16k_int16(str(p))
    except (sf.SoundFileError, RuntimeError, ValueError) as exc:
        bad += 1
        print('skip undecodable clip:', p.name, exc)
        continue
    scipy.io.wavfile.write(os.path.join(out, p.stem + '.wav'), 16000, pcm)
    n += 1
print(f'fma_16k: wrote {n} files, skipped {bad} of {len(files)}')
if n == 0:
    sys.exit('fma: nothing converted')
" "$MWW_ROOT/fma/fma_small" "$MWW_ROOT/fma_16k" 2>&1 | tee -a "$LOG"
	touch "$MWW_ROOT/.done-fma_16k"
fi

# HF negatives (pre-generated spectrogram features). *_background zips skipped.
mkdir -p "$MWW_ROOT/negative_datasets"
for fname in "${NEG_FILES[@]}"; do
	marker="$MWW_ROOT/negative_datasets/.done-$fname"
	if [[ -e "$marker" ]]; then
		log "skip negatives (done): $fname"
		continue
	fi
	fetch_zip "$NEG_URL_ROOT/$fname" "$fname"
	log "unzip: $fname"
	unzip -q -o "$DATA_TMP/$fname" -d "$MWW_ROOT/negative_datasets"
	touch "$marker"
done

# --- 6. verification ------------------------------------------------------
log "verify imports"
"$PY" -I - <<'PYEOF' 2>&1 | tee -a "$LOG"
import importlib

for mod in ("tensorflow", "tensorboard", "microwakeword", "pymicro_features", "torch"):
    m = importlib.import_module(mod)
    print(f"{mod}: {getattr(m, '__version__', 'ok (no __version__)')}")
PYEOF

log "data sizes"
for d in mit_rirs audioset_16k fma_16k negative_datasets piper-sample-generator; do
	du -sh "$MWW_ROOT/$d" 2>&1 | tee -a "$LOG"
done

log "setup complete"

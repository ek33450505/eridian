#!/usr/bin/env bash
# Unit B: generate samples, build features, train and install a "hey rocky"
# microWakeWord model. Follows kahrendt/microWakeWord's basic_training_notebook
# (saved by setup.sh at $MWW_ROOT/notebook.ipynb). Run setup.sh first.
# Idempotent: each step writes a .done-train-<step> marker; rerun resumes.
#
# Usage: train.sh [--from <step>] [--force-manifest]
#   --force-manifest  rewrite out/hey_rocky.json (default: only if missing, so a
#                     tuned probability_cutoff survives reruns)
#   steps: positives negatives features yaml train install smoke
#
# Env overrides:
#   MWW_ROOT         install root, strictly under $HOME (default: $HOME/Datasets/undertone-training/mww)
#                    `train.sh --check-root` validates it and exits
#   PIPER_BATCH      piper batch size (default: 50, sized for MPS)
#   POSITIVE_COUNT   "hey rocky" samples (default: 5000)
#   VARIANT_COUNT    samples per phonetic variant (default: 500)
#   CONFUSABLE_COUNT total confusable samples (default: 2000)
#   HELDOUT          clips per set kept out of training for smoke_test.py (default: 20)
#
# Data licensing: see README.md; the model is for non-commercial personal use.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Root guard, sentinel and safe_rm_* helpers live in lib.sh.
# shellcheck source=lib.sh
source "$HERE/lib.sh"
mww_init train "$@"
PIPER_BATCH="${PIPER_BATCH:-50}"
POSITIVE_COUNT="${POSITIVE_COUNT:-5000}"
VARIANT_COUNT="${VARIANT_COUNT:-500}"
CONFUSABLE_COUNT="${CONFUSABLE_COUNT:-2000}"
HELDOUT="${HELDOUT:-20}"

PY="$MWW_ROOT/venv/bin/python"
PIPER_DIR="$MWW_ROOT/piper-sample-generator"
LOG="$MWW_ROOT/logs/train.log"
OUT="$MWW_ROOT/out"
STEPS=(positives negatives features yaml train install smoke)

# Phonetic variants of the wake word (name|text), as the notebook suggests.
POSITIVE_VARIANTS=("hey_rah_kee|hey rah-kee" "hey_rock_ee|hey rock-ee")
# Confusable negatives (name|text); CONFUSABLE_COUNT is split evenly.
CONFUSABLES=(
	"hey_ricky|hey ricky" "hey_rocco|hey rocco" "hey_lucky|hey lucky"
	"rocky_road|rocky road" "hey_rock|hey rock" "hey_jockey|hey jockey"
	"hi_rocky|hi rocky" "rocky|rocky"
)

mkdir -p "$MWW_ROOT/logs" "$OUT"

log() {
	printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" | tee -a "$LOG"
}

die() {
	log "ERROR: $*"
	exit 1
}

# Any unhandled failure (set -e) is logged with the command, status and line;
# -E carries the trap into functions and subshells.
set -E
trap 'log "ERROR: command failed (exit $?) at line $LINENO: $BASH_COMMAND"' ERR

# --- args -----------------------------------------------------------------
FROM="${STEPS[0]}"
EXPLICIT=0
FORCE_MANIFEST=0
FORCE_FEATURES=0
while (($# > 0)); do
	case "$1" in
	--from)
		[[ $# -ge 2 ]] || die "--from needs a step (${STEPS[*]})"
		FROM="$2"
		EXPLICIT=1
		shift 2
		;;
	--force-manifest)
		FORCE_MANIFEST=1
		shift
		;;
	-h | --help)
		sed -n '2,22p' "${BASH_SOURCE[0]}"
		exit 0
		;;
	*) die "unknown argument: $1" ;;
	esac
done

from_idx=-1
for i in "${!STEPS[@]}"; do
	[[ "${STEPS[$i]}" == "$FROM" ]] && from_idx="$i"
done
((from_idx >= 0)) || die "unknown step '$FROM' (${STEPS[*]})"

# --- preflight ------------------------------------------------------------
[[ -x "$PY" ]] || die "no venv at $MWW_ROOT/venv; run setup.sh first"
[[ -d "$MWW_ROOT/microWakeWord" ]] || die "microWakeWord clone missing; run setup.sh first"
[[ -s "$PIPER_DIR/models/en_US-libritts_r-medium.pt" ]] || die "piper model missing; run setup.sh first"
[[ -e "$MWW_ROOT/negative_datasets/.done-speech.zip" ]] || die "negative datasets missing; run setup.sh first"
# Keep in sync with setup.sh (trust-on-first-use from the rhasspy v2.0.0 release,
# 2026-11-04). gen() unpickles this file, so refuse any other content.
PIPER_MODEL_SHA256="e95ee53770bf598c354a6e6dbfc95ccb259aeeb501d35a86be8a767429ab0ff6"
piper_sum="$(shasum -a 256 "$PIPER_DIR/models/en_US-libritts_r-medium.pt")" || die "cannot hash piper model"
[[ "${piper_sum%% *}" == "$PIPER_MODEL_SHA256" ]] || die "piper model sha256 mismatch (got ${piper_sum%% *}); rerun setup.sh"
log "MWW_ROOT=$MWW_ROOT from=$FROM"

# The notebook uses cwd-relative paths; keep them.
cd "$MWW_ROOT"

count_wavs() {
	# find exits 1 on a missing dir and pipefail would carry that through,
	# silently killing the script under set -e; a missing dir counts as 0.
	{ find "$1" -maxdepth 1 -name '*.wav' 2>/dev/null || true; } | wc -l | tr -d ' '
}

# gen <name> <text> <count> <dir>: piper generate_samples.py, exactly the
# notebook's invocation. Skips when the dir already has enough clips.
gen() {
	local name="$1" text="$2" count="$3" dir="$4"
	local have
	have="$(count_wavs "$dir/$name")"
	if ((have >= count)); then
		log "skip generate (have $have): $name"
		return 0
	fi
	log "generate $count x '$text' -> $dir/$name"
	safe_rm_subdir "${dir#"$MWW_ROOT/"}/$name"
	# torch >= 2.6 defaults torch.load to weights_only=True, which rejects piper's
	# pickled checkpoint. Ed approved the full unpickle on 2026-11-04 for this
	# hash-pinned file only: set on this one command, never exported.
	TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 "$PY" "$PIPER_DIR/generate_samples.py" "$text" \
		--max-samples "$count" \
		--batch-size "$PIPER_BATCH" \
		--output-dir "$dir/$name" 2>&1 | tee -a "$LOG"
}

# merge <src_dir> <dest_dir> <heldout_dir> <heldout_src_name>: copy every set's
# clips into one flat dir for Clips(file_pattern='*.wav'). The first $HELDOUT
# clips (sorted order) of <heldout_src_name> are copied to <heldout_dir>
# instead of <dest_dir>.
merge() {
	local srcroot="$1" dest="$2" held="$3" heldname="$4"
	safe_rm_subdir "${dest#"$MWW_ROOT/"}"
	safe_rm_subdir "${held#"$MWW_ROOT/"}"
	mkdir -p "$dest" "$held"
	local d f n
	for d in "$srcroot"/*/; do
		n="$(basename "$d")"
		local idx=0
		while IFS= read -r f; do
			idx=$((idx + 1))
			if [[ "$n" == "$heldname" && "$idx" -le "$HELDOUT" ]]; then
				cp "$f" "$held/${n}_$(basename "$f")"
			else
				cp "$f" "$dest/${n}_$(basename "$f")"
			fi
		done < <(find "$d" -maxdepth 1 -name '*.wav' | sort)
	done
	log "merged $(count_wavs "$dest") clips -> $dest ($(count_wavs "$held") held out)"
}

# --- steps ----------------------------------------------------------------
step_positives() {
	gen hey_rocky "hey rocky" "$POSITIVE_COUNT" "$MWW_ROOT/gen/positives"
	local v
	for v in "${POSITIVE_VARIANTS[@]}"; do
		gen "${v%%|*}" "${v#*|}" "$VARIANT_COUNT" "$MWW_ROOT/gen/positives"
	done
	merge "$MWW_ROOT/gen/positives" "$MWW_ROOT/positives" "$MWW_ROOT/heldout/positives" hey_rocky
}

step_negatives() {
	local per=$((CONFUSABLE_COUNT / ${#CONFUSABLES[@]}))
	local c
	for c in "${CONFUSABLES[@]}"; do
		gen "${c%%|*}" "${c#*|}" "$per" "$MWW_ROOT/gen/confusables"
	done
	# Hold out an even share of each confusable phrase for smoke_test.py.
	safe_rm_subdir confusables
	safe_rm_subdir heldout/confusables
	mkdir -p "$MWW_ROOT/confusables" "$MWW_ROOT/heldout/confusables"
	# Total held out is exactly HELDOUT: base share each, the remainder spread
	# one extra clip over the first phrases.
	local base=$((HELDOUT / ${#CONFUSABLES[@]})) rem=$((HELDOUT % ${#CONFUSABLES[@]}))
	local per_held n idx f k=0
	for c in "${CONFUSABLES[@]}"; do
		n="${c%%|*}"
		per_held=$((base + (k < rem ? 1 : 0)))
		k=$((k + 1))
		idx=0
		while IFS= read -r f; do
			idx=$((idx + 1))
			if ((idx <= per_held)); then
				cp "$f" "$MWW_ROOT/heldout/confusables/${n}_$(basename "$f")"
			else
				cp "$f" "$MWW_ROOT/confusables/${n}_$(basename "$f")"
			fi
		done < <(find "$MWW_ROOT/gen/confusables/$n" -maxdepth 1 -name '*.wav' | sort)
	done
	log "confusables: $(count_wavs "$MWW_ROOT/confusables") train, $(count_wavs "$MWW_ROOT/heldout/confusables") held out"
}

step_features() {
	# make_features.py never deletes: a leftover <mmap>.part from an interrupted
	# run is cleared here, through the guarded helper, before it starts.
	local out split
	for out in generated_augmented_features generated_augmented_confusables; do
		for split in training validation testing; do
			safe_rm_subdir "$out/$split/wakeword_mmap.part"
		done
	done
	# make_features.py skips existing mmaps, so a forced run (--from at or before
	# features) or one that follows a rebuilt positives/negatives step must
	# remove them first; otherwise stale features would be reused.
	if ((EXPLICIT == 1 || FORCE_FEATURES == 1)); then
		log "removing stale feature mmaps"
		safe_rm_subdir generated_augmented_features
		safe_rm_subdir generated_augmented_confusables
	fi
	"$PY" -I "$HERE/make_features.py" --root "$MWW_ROOT" 2>&1 | tee -a "$LOG"
}

step_yaml() {
	local tpl="$HERE/training.yaml.in" content
	[[ -s "$tpl" ]] || die "missing template $tpl"
	content="$(<"$tpl")"
	printf '%s\n' "${content//@MWW_ROOT@/$MWW_ROOT}" >"$MWW_ROOT/training_parameters.yaml"
	log "wrote $MWW_ROOT/training_parameters.yaml"
}

step_train() {
	log "train (mixednet, stride 3); resumes from checkpoint if present"
	"$PY" -I -m microwakeword.model_train_eval \
		--training_config="$MWW_ROOT/training_parameters.yaml" \
		--train 1 \
		--restore_checkpoint 1 \
		--test_tf_nonstreaming 0 \
		--test_tflite_nonstreaming 0 \
		--test_tflite_nonstreaming_quantized 0 \
		--test_tflite_streaming 0 \
		--test_tflite_streaming_quantized 1 \
		--use_weights "best_weights" \
		mixednet \
		--pointwise_filters "64,64,64,64" \
		--repeat_in_block "1, 1, 1, 1" \
		--mixconv_kernel_sizes '[5], [7,11], [9,15], [23]' \
		--residual_connection "0,0,0,0" \
		--first_conv_filters 32 \
		--first_conv_kernel_size 5 \
		--stride 3 2>&1 | tee -a "$LOG"
}

step_install() {
	local src="$MWW_ROOT/trained_models/wakeword/tflite_stream_state_internal_quant/stream_state_internal_quant.tflite"
	[[ -s "$src" ]] || die "trained model not found: $src"
	cp "$src" "$OUT/hey_rocky.tflite"
	if [[ -e "$OUT/hey_rocky.json" && "$FORCE_MANIFEST" -eq 0 ]]; then
		log "kept existing $OUT/hey_rocky.json (use --force-manifest to rewrite)"
	else
		printf '%s\n' '{"type":"micro","wake_word":"Hey Rocky","author":"Ed Kubiak","model":"hey_rocky.tflite","trained_languages":["en"],"version":2,"micro":{"probability_cutoff":0.97,"feature_step_size":10,"sliding_window_size":5,"tensor_arena_size":22860,"minimum_esphome_version":"2024.7.0"}}' \
			>"$OUT/hey_rocky.json"
	fi
	log "installed $OUT/hey_rocky.tflite and hey_rocky.json"
}

step_smoke() {
	local upy="${UNDERTONE_PY:-$HOME/Projects/personal/undertone/.venv/bin/python}"
	[[ -x "$upy" ]] || die "undertone venv python not found: $upy"
	"$upy" -I "$HERE/smoke_test.py" --root "$MWW_ROOT" 2>&1 | tee -a "$LOG"
}

# --- run ------------------------------------------------------------------
for i in "${!STEPS[@]}"; do
	s="${STEPS[$i]}"
	marker="$MWW_ROOT/.done-train-$s"
	if ((i < from_idx)); then
		log "skip $s (before --from $FROM)"
		continue
	fi
	# Without --from, finished steps are skipped (smoke always reruns);
	# with --from, that step and every later one is forced.
	if ((EXPLICIT == 0)) && [[ -e "$marker" && "$s" != smoke ]]; then
		log "skip $s (done)"
		continue
	fi
	log "=== step: $s ==="
	"step_$s"
	if [[ "$s" == positives || "$s" == negatives ]]; then
		FORCE_FEATURES=1
		safe_rm_subdir .done-train-features
	fi
	touch "$marker"
	log "=== done: $s ==="
done
log "train.sh complete"

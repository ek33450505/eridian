#!/usr/bin/env bash
# Prove the audio pipeline (wake word, VAD, STT, TTS) runs with the network denied.
# Uses macOS sandbox-exec; a Docker --network none container can't run MLX or `say`.
# Run `uv run undertone models fetch` first (outside the sandbox).
#
# Usage: scripts/verify-offline.sh [--keep]
set -euo pipefail

KEEP=0
for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=1 ;;
    *)
      echo "usage: $0 [--keep]" >&2
      exit 2
      ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "verify-offline: requires macOS (MLX and 'say' are macOS-only)." >&2
  exit 1
fi
if ! command -v sandbox-exec > /dev/null 2>&1; then
  echo "verify-offline: sandbox-exec not found." >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "verify-offline: $PY missing - run 'uv sync' first." >&2
  exit 1
fi

TMP="$(mktemp -d)"
# shellcheck disable=SC2329 # invoked via trap
cleanup() {
  if [[ "$KEEP" -eq 1 ]]; then
    echo "kept: $TMP"
  else
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT

clip="$TMP/clip.wav"
out="$TMP/reply.aiff"
phrase="Hey Jarvis. [[slnc 600]] What is the capital of France? [[slnc 1500]]"

voice_args=()
if [[ -n "${UNDERTONE_VOICE:-}" ]]; then
  voice_args=(-v "$UNDERTONE_VOICE")
fi
say "${voice_args[@]+"${voice_args[@]}"}" -o "$clip" --data-format=LEI16@16000 -- "$phrase"

profile='(version 1)(allow default)(deny network*)'
rc=0
sandbox-exec -p "$profile" "$PY" "$ROOT/scripts/offline_check.py" "$clip" "$out" || rc=$?

if [[ "$rc" -eq 0 ]]; then
  echo "OFFLINE CHECK: PASS (wake, VAD, STT, TTS ran with network denied)"
else
  echo "OFFLINE CHECK: FAIL (exit $rc)"
fi
exit "$rc"

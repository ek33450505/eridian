"""Kokoro-82M ONNX voice: phoneme string -> 16 kHz mono int16 PCM bytes.

This module never sees raw text. Phonemes come from `speaker.espeak_phonemes`,
which is the choke point that scrubs the wake word and `[[ ]]` before espeak-ng.

Local-first: the onnxruntime macOS wheel embeds Microsoft's 1DS telemetry
client (mobile.events.data.microsoft.com/OneCollector), so ORT_DISABLE_TELEMETRY
is FORCED to "1" here, before onnxruntime is ever imported. HF_HUB_OFFLINE is
setdefault'd to "1" like `stt.py`; `fetch_model` is the one place that unsets it.

Spike facts (measured): the repo's model_fp16.onnx produced NaN audio on 2/8
sentences on CPU, fp32 produced 0 NaN in 200; inference costs ~0.05 s plus
0.085 s per second of audio, so the first piece of a sentence is kept short;
every export starts with ~0.36-0.42 s of near-silence that must be trimmed.
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, Any, Callable

os.environ["ORT_DISABLE_TELEMETRY"] = "1"
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np

MODEL_REPO = "onnx-community/Kokoro-82M-v1.0-ONNX"
MODEL_REVISION = "1939ad2a8e416c0acfeecc08a694d14ef25f2231"
MODEL_FILE = "onnx/model.onnx"
TOKENIZER_FILE = "tokenizer.json"
DEFAULT_VOICE = "am_michael"
VOICE_ENV = "UNDERTONE_KOKORO_VOICE"

FIRST_PIECE_TOKENS = 50
PIECE_TOKENS = 200
MAX_TOKENS = 510

MODEL_RATE = 24000
OUT_RATE = 16000
STYLE_DIM = 256
WARM_UP_PHONEMES = "ɹˈɛdi."

_VOICE_RE = re.compile(r"[a-z]{2}_[a-z]+")
_LONG_PAUSE = ".!?…"
_SHORT_PAUSE = ",;:—"
_LONG_PAUSE_S = 0.25
_SHORT_PAUSE_S = 0.1
_SILENCE_FRACTION = 0.01  # -40 dB relative to the clip's peak
_LEAD_S = 0.02
_TAIL_S = 0.05


class KokoroUnavailable(RuntimeError):
    """Raised when the Kokoro model or voice can't be loaded."""


def fetch_model() -> None:
    """Download the pinned model, tokenizer and voices once (used by `undertone models fetch`)."""
    previous = os.environ.pop("HF_HUB_OFFLINE", None)
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(
            MODEL_REPO,
            revision=MODEL_REVISION,
            allow_patterns=[MODEL_FILE, TOKENIZER_FILE, "voices/*.bin"],
        )
    finally:
        if previous is not None:
            os.environ["HF_HUB_OFFLINE"] = previous


def _check_voice_name(voice: str) -> str:
    # The name becomes part of a file path, so it is validated, never sanitized.
    if not _VOICE_RE.fullmatch(voice):
        raise KokoroUnavailable(
            f"undertone: invalid Kokoro voice name {voice!r} (expected e.g. 'am_michael')"
        )
    return voice


def model_paths(voice: str) -> tuple[str, str, str]:
    """Return (model, tokenizer, voice file) from the local HF cache only."""
    _check_voice_name(voice)
    from huggingface_hub import hf_hub_download

    try:
        return tuple(  # type: ignore[return-value]
            hf_hub_download(
                MODEL_REPO,
                name,
                revision=MODEL_REVISION,
                local_files_only=True,
            )
            for name in (MODEL_FILE, TOKENIZER_FILE, f"voices/{voice}.bin")
        )
    except Exception as exc:  # noqa: BLE001 - any cache miss means "not fetched"
        raise KokoroUnavailable(
            f"undertone: Kokoro model/voice {voice!r} is not cached locally -- "
            "run `undertone models fetch` first"
        ) from exc


def _default_session(model_path: str) -> Any:
    import onnxruntime

    onnxruntime.disable_telemetry_events()
    return onnxruntime.InferenceSession(model_path, providers=["CPUExecutionProvider"])


def _default_resample(pcm24: bytes) -> bytes:
    from livekit import rtc

    resampler = rtc.AudioResampler(
        MODEL_RATE, OUT_RATE, num_channels=1, quality=rtc.AudioResamplerQuality.HIGH
    )
    frames = resampler.push(rtc.AudioFrame(pcm24, MODEL_RATE, 1, len(pcm24) // 2))
    frames += resampler.flush()
    return b"".join(bytes(f.data) for f in frames)


class KokoroVoice:
    """Resident Kokoro voice. Inject `paths`/`session_factory`/`resample` in tests."""

    def __init__(
        self,
        voice: str | None = None,
        *,
        paths: tuple[str, str, str] | None = None,
        session_factory: Callable[[str], Any] | None = None,
        resample: Callable[[bytes], bytes] | None = None,
    ) -> None:
        self.voice = _check_voice_name(voice or os.environ.get(VOICE_ENV) or DEFAULT_VOICE)
        model_path, tokenizer_path, voice_path = paths or model_paths(self.voice)

        try:
            with open(tokenizer_path, encoding="utf-8") as fh:
                self._vocab: dict[str, int] = json.load(fh)["model"]["vocab"]
            styles = np.fromfile(voice_path, dtype=np.float32)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise KokoroUnavailable(f"undertone: could not load Kokoro assets: {exc}") from exc
        if styles.size == 0 or styles.size % STYLE_DIM:
            raise KokoroUnavailable(
                f"undertone: Kokoro voice {self.voice!r} has an unexpected shape"
            )
        self._styles = styles.reshape(-1, STYLE_DIM)

        self._session = (session_factory or _default_session)(model_path)
        self._resample = resample or _default_resample

    def _ntok(self, text: str) -> int:
        return sum(1 for c in text if c in self._vocab)

    def _hard_cut(self, word: str) -> list[str]:
        chunks: list[str] = []
        cur: list[str] = []
        count = 0
        for ch in word:
            if ch in self._vocab:
                if count == MAX_TOKENS:
                    chunks.append("".join(cur))
                    cur, count = [], 0
                count += 1
            cur.append(ch)
        if cur:
            chunks.append("".join(cur))
        return chunks

    def pieces(self, phonemes: str) -> list[str]:
        """Split into render pieces: short first piece, clause-aligned, <= MAX_TOKENS each."""
        clauses: list[list[str]] = []
        clause: list[str] = []
        for word in phonemes.split():
            clause.append(word)
            if word[-1] in _LONG_PAUSE + _SHORT_PAUSE:
                clauses.append(clause)
                clause = []
        if clause:
            clauses.append(clause)

        out: list[str] = []
        cur = ""

        def limit() -> int:
            return FIRST_PIECE_TOKENS if not out else PIECE_TOKENS

        def add(unit: str) -> None:
            nonlocal cur
            cand = f"{cur} {unit}" if cur else unit
            if cur and self._ntok(cand) > limit():
                out.append(cur)
                cur = unit
            else:
                cur = cand

        for words in clauses:
            text = " ".join(words)
            fresh_limit = PIECE_TOKENS if (out or cur) else FIRST_PIECE_TOKENS
            if self._ntok(text) <= fresh_limit:
                add(text)
                continue
            for word in words:
                for chunk in self._hard_cut(word) if self._ntok(word) > MAX_TOKENS else [word]:
                    add(chunk)
        if cur:
            out.append(cur)
        return out

    def render(self, phonemes: str) -> bytes | None:
        """Render one piece to 16 kHz int16 PCM, or None if there is nothing safe to play."""
        ids = [self._vocab[c] for c in phonemes if c in self._vocab]
        if not ids:
            return None
        style = self._styles[min(len(ids), len(self._styles)) - 1][None, :]
        feeds = {
            "input_ids": np.array([[0, *ids, 0]], dtype=np.int64),
            "style": style.astype(np.float32),
            "speed": np.array([1.0], dtype=np.float32),
        }
        wave = np.asarray(self._session.run(None, feeds)[0], dtype=np.float32).reshape(-1)
        # NaN/inf must never reach the speaker.
        if wave.size == 0 or not np.all(np.isfinite(wave)):
            return None
        peak = float(np.max(np.abs(wave)))
        if peak <= 0.0:
            return None
        loud = np.flatnonzero(np.abs(wave) > peak * _SILENCE_FRACTION)
        start = max(int(loud[0]) - int(_LEAD_S * MODEL_RATE), 0)
        end = min(int(loud[-1]) + 1 + int(_TAIL_S * MODEL_RATE), wave.size)
        wave = wave[start:end]

        last = phonemes.rstrip()[-1:]
        pause_s = (
            _LONG_PAUSE_S if last in _LONG_PAUSE else _SHORT_PAUSE_S if last in _SHORT_PAUSE else 0.0
        )
        wave = np.concatenate([wave, np.zeros(int(pause_s * MODEL_RATE), dtype=np.float32)])

        pcm24 = (np.clip(wave, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        pcm = self._resample(pcm24)
        return pcm[: len(pcm) // 2 * 2]

    def warm_up(self) -> None:
        """Run one throwaway inference; the first after load takes ~0.7 s."""
        self.render(WARM_UP_PHONEMES)

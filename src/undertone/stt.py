"""Parakeet speech-to-text wrapper: lazy-loaded, offline-first, float32-only.

`parakeet_mlx`/`huggingface_hub` are imported lazily inside functions so
`undertone hook stop` and other lightweight commands stay fast even when this
module is imported transitively. HF_HUB_OFFLINE is forced to "1" at import
time of THIS module (before any parakeet/huggingface_hub import happens),
because parakeet's `from_pretrained` otherwise contacts the Hugging Face Hub
on every start even with a warm local cache -- that breaks the local-first
promise. `undertone models fetch` is the one place that explicitly unsets it.
"""

from __future__ import annotations

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

from typing import TYPE_CHECKING

import numpy as np

from .listener import SAMPLE_RATE

MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v3"

if TYPE_CHECKING:
    from parakeet_mlx import ParakeetTDT  # noqa: F401 - typing only


class ModelNotCachedError(RuntimeError):
    """Raised when the Parakeet model isn't in the local HF cache yet."""


def is_model_cached(model_id: str = MODEL_ID) -> bool:
    """Best-effort check for whether `model_id` is already in the local HF cache."""
    from huggingface_hub import scan_cache_dir

    try:
        cache_info = scan_cache_dir()
    except Exception:  # noqa: BLE001 - treat a broken cache as "not cached"
        return False
    return any(repo.repo_id == model_id for repo in cache_info.repos)


def fetch_model(model_id: str = MODEL_ID) -> None:
    """Download `model_id` once, contacting the Hub (used by `undertone models fetch`)."""
    previous = os.environ.pop("HF_HUB_OFFLINE", None)
    try:
        from parakeet_mlx import from_pretrained

        from_pretrained(model_id)
    finally:
        if previous is not None:
            os.environ["HF_HUB_OFFLINE"] = previous


class ParakeetSTT:
    """Callable: float32 mono np.ndarray (16 kHz) -> transcript text.

    Always pass float32 mono audio at 16 kHz -- `__call__` casts a
    non-float32 array before running inference, but a bf16/float64 cast
    breaks parakeet's STFT, so the cast is a safety net, not a free pass to
    skip getting the dtype right upstream.
    """

    def __init__(self, model_id: str = MODEL_ID) -> None:
        if not is_model_cached(model_id):
            raise ModelNotCachedError(
                f"undertone: model {model_id!r} is not cached locally -- "
                "run `undertone models fetch` first"
            )
        from parakeet_mlx import from_pretrained

        self._model = from_pretrained(model_id)
        self._warm_up()

    def _warm_up(self) -> None:
        # The first real inference is ~10x slower than steady-state (823 ms
        # vs 60-78 ms, measured live) if the model hasn't run yet -- eat that
        # cost here, once, at load time, with throwaway silence instead of
        # on the user's first utterance.
        self(np.zeros(SAMPLE_RATE, dtype=np.float32))

    def __call__(self, audio: np.ndarray) -> str:
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        if audio.dtype != np.float32:
            # A bf16/float64 cast breaks parakeet's STFT -- float32 only.
            audio = audio.astype(np.float32)
        logmel = get_logmel(mx.array(audio), self._model.preprocessor_config)
        result = self._model.generate(logmel)
        return result[0].text if result else ""

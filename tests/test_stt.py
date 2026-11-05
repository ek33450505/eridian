"""Tests for eridian.stt -- offline-first env var and missing-model handling.

Never imports the real parakeet_mlx/huggingface_hub model-loading path: the
"missing model" test mocks `is_model_cached`, and the offline-env test runs
in a subprocess so it observes a fresh import without depending on module
import order within this test process.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from unittest import mock

import numpy as np

from eridian.stt import SAMPLE_RATE, ModelNotCachedError, ParakeetSTT


class HfHubOfflineDefaultTest(unittest.TestCase):
    def test_offline_env_defaults_to_1_after_import(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os; os.environ.pop('HF_HUB_OFFLINE', None); "
                "import eridian.stt; "
                "print(os.environ.get('HF_HUB_OFFLINE'))",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "1")

    def test_offline_env_does_not_override_an_explicit_value(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os; os.environ['HF_HUB_OFFLINE'] = '0'; "
                "import eridian.stt; "
                "print(os.environ.get('HF_HUB_OFFLINE'))",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "0")


class MissingModelTest(unittest.TestCase):
    def test_uncached_model_raises_clear_error_without_loading(self) -> None:
        with mock.patch("eridian.stt.is_model_cached", return_value=False):
            with self.assertRaises(ModelNotCachedError) as ctx:
                ParakeetSTT()
        self.assertIn("eridian models fetch", str(ctx.exception))

    def test_cached_model_proceeds_to_load(self) -> None:
        fake_model = mock.Mock()
        fake_model.generate.return_value = [mock.Mock(text="")]
        with mock.patch("eridian.stt.is_model_cached", return_value=True):
            with mock.patch(
                "parakeet_mlx.from_pretrained", return_value=fake_model
            ) as mock_from_pretrained:
                with mock.patch("parakeet_mlx.audio.get_logmel"):
                    stt = ParakeetSTT()
        mock_from_pretrained.assert_called_once()
        self.assertIs(stt._model, fake_model)


class WarmUpTest(unittest.TestCase):
    def test_model_is_warmed_up_once_during_init_before_any_real_call(self) -> None:
        fake_model = mock.Mock()
        fake_model.generate.return_value = [mock.Mock(text="")]
        with mock.patch("eridian.stt.is_model_cached", return_value=True):
            with mock.patch("parakeet_mlx.from_pretrained", return_value=fake_model):
                with mock.patch("parakeet_mlx.audio.get_logmel") as mock_get_logmel:
                    stt = ParakeetSTT()

        # __init__ already ran one warm-up inference -- before the caller
        # has made any real transcription call.
        self.assertEqual(fake_model.generate.call_count, 1)
        warmup_audio = np.array(mock_get_logmel.call_args.args[0])
        np.testing.assert_array_equal(
            warmup_audio, np.zeros(SAMPLE_RATE, dtype=np.float32)
        )

        # A real call afterward is a separate, second inference.
        with mock.patch("parakeet_mlx.audio.get_logmel"):
            stt(np.zeros(SAMPLE_RATE, dtype=np.float32))
        self.assertEqual(fake_model.generate.call_count, 2)


if __name__ == "__main__":
    unittest.main()

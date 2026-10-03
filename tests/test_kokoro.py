import hashlib
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from undertone import kokoro


RATE = 24000
VOCAB_CHARS = "abcdefghij"
MARKS = ",.!?;:—…"


def make_vocab() -> dict[str, int]:
    vocab = {"$": 0, " ": 1}
    for ch in VOCAB_CHARS + MARKS:
        vocab[ch] = len(vocab)
    return vocab


class FakeSession:
    def __init__(self, waveform=None):
        self.waveform = waveform if waveform is not None else np.full(RATE, 0.5, np.float32)
        self.calls = []

    def run(self, output_names, feeds):
        self.calls.append(feeds)
        return [np.asarray(self.waveform, dtype=np.float32)[None, :]]


def tone_clip() -> np.ndarray:
    t = np.arange(int(0.2 * RATE)) / RATE
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return np.concatenate([np.zeros(int(0.4 * RATE), np.float32), tone, np.zeros(int(0.5 * RATE), np.float32)])


class KokoroTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        self.tokenizer = d / "tokenizer.json"
        self.tokenizer.write_text(json.dumps({"model": {"vocab": make_vocab()}}))
        self.voice_file = d / "voice.bin"
        rows = np.arange(510, dtype=np.float32)[:, None] * np.ones((1, 256), np.float32)
        rows.tofile(self.voice_file)
        self.paths = ("model.onnx", str(self.tokenizer), str(self.voice_file))
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(kokoro.VOICE_ENV, None)

    def make(self, waveform=None, resample=lambda b: b, **kw):
        self.session = FakeSession(waveform)
        return kokoro.KokoroVoice(
            paths=self.paths, session_factory=lambda _p: self.session, resample=resample, **kw
        )


class RenderTests(KokoroTestBase):
    def test_exact_feeds(self):
        v = self.make()
        v.render("ab zz c")  # z is unknown and dropped
        feeds = self.session.calls[0]
        ids = [v._vocab[c] for c in "ab  c"]
        self.assertEqual(feeds["input_ids"].dtype, np.int64)
        self.assertEqual(feeds["input_ids"].tolist(), [[0, *ids, 0]])
        self.assertEqual(feeds["style"].dtype, np.float32)
        self.assertEqual(feeds["style"].shape, (1, 256))
        self.assertTrue(np.all(feeds["style"] == float(len(ids) - 1)))
        self.assertEqual(feeds["speed"].dtype, np.float32)
        self.assertEqual(feeds["speed"].tolist(), [1.0])

    def test_style_row_clamped(self):
        v = self.make()
        v.render("a" * 600)
        self.assertTrue(np.all(self.session.calls[0]["style"] == 509.0))

    def test_no_vocab_chars_returns_none_without_running(self):
        v = self.make()
        self.assertIsNone(v.render("zzz"))
        self.assertEqual(self.session.calls, [])

    def test_non_finite_returns_none(self):
        for bad in (np.nan, np.inf, -np.inf):
            wave = np.full(RATE, 0.5, np.float32)
            wave[100] = bad
            self.assertIsNone(self.make(wave).render("ab."), bad)

    def test_silent_or_empty_returns_none(self):
        self.assertIsNone(self.make(np.zeros(RATE, np.float32)).render("ab."))
        self.assertIsNone(self.make(np.zeros(0, np.float32)).render("ab."))

    def _trimmed_len(self, text):
        out = self.make(tone_clip()).render(text)
        return len(out) // 2 / RATE

    def test_trim_and_pause(self):
        self.assertAlmostEqual(self._trimmed_len("ab."), 0.02 + 0.2 + 0.05 + 0.25, delta=0.002)
        self.assertAlmostEqual(self._trimmed_len("ab,"), 0.02 + 0.2 + 0.05 + 0.1, delta=0.002)
        self.assertAlmostEqual(self._trimmed_len("ab"), 0.02 + 0.2 + 0.05, delta=0.002)

    def test_output_is_int16_bytes(self):
        out = self.make(tone_clip()).render("ab.")
        self.assertEqual(len(out) % 2, 0)
        self.assertGreater(np.abs(np.frombuffer(out, np.int16)).max(), 10000)

    def test_real_resampler_length(self):
        v = kokoro.KokoroVoice(
            paths=self.paths, session_factory=lambda _p: FakeSession(tone_clip())
        )
        out = v.render("ab.")
        n24 = int((0.02 + 0.2 + 0.05 + 0.25) * RATE) * 2
        self.assertEqual(len(out) % 2, 0)
        self.assertAlmostEqual(len(out) / n24, 2 / 3, delta=0.01)

    def test_warm_up_runs_session(self):
        v = self.make()
        v.warm_up()
        self.assertEqual(len(self.session.calls), 1)


class PiecesTests(KokoroTestBase):
    def words(self, n, size=4):
        return " ".join("abcd"[:size] for _ in range(n))

    def test_empty(self):
        v = self.make()
        self.assertEqual(v.pieces(""), [])
        self.assertEqual(v.pieces("   "), [])

    def test_first_piece_short_and_ends_at_clause(self):
        v = self.make()
        text = "abcd abcd, " + self.words(12) + ". " + self.words(30) + "."
        pieces = v.pieces(text)
        self.assertLessEqual(v._ntok(pieces[0]), kokoro.FIRST_PIECE_TOKENS)
        self.assertEqual(pieces[0], "abcd abcd,")
        for p in pieces[1:]:
            self.assertLessEqual(v._ntok(p), kokoro.PIECE_TOKENS)
        self.assertEqual(" ".join(pieces), " ".join(text.split()))

    def test_no_punctuation_split_at_spaces(self):
        v = self.make()
        text = self.words(60)  # 60 * 5 - 1 = 299 tokens
        pieces = v.pieces(text)
        self.assertGreater(len(pieces), 1)
        self.assertLessEqual(v._ntok(pieces[0]), kokoro.FIRST_PIECE_TOKENS)
        for p in pieces[1:]:
            self.assertLessEqual(v._ntok(p), kokoro.PIECE_TOKENS)
            self.assertTrue(all(w == "abcd" for w in p.split()))
        self.assertEqual(" ".join(pieces), text)

    def test_merges_small_clauses_later(self):
        v = self.make()
        text = "ab. " * 30
        pieces = v.pieces(text)
        self.assertLessEqual(len(pieces), 3)
        self.assertEqual(" ".join(pieces), " ".join(text.split()))

    def test_giant_word_hard_cut(self):
        v = self.make()
        pieces = v.pieces("a" * 1100)
        self.assertEqual([v._ntok(p) for p in pieces], [510, 510, 80])
        self.assertEqual("".join(pieces), "a" * 1100)


class VoiceNameTests(KokoroTestBase):
    def test_rejects_bad_names(self):
        for bad in ("../x", "AM_michael", "a_b", "am_michael\n", "am_mich/ael"):
            with self.assertRaises(kokoro.KokoroUnavailable, msg=bad):
                self.make(voice=bad)

    def test_empty_name_falls_back_to_default(self):
        self.assertEqual(self.make(voice="").voice, "am_adam")

    def test_env_invalid_rejected(self):
        os.environ[kokoro.VOICE_ENV] = "../x"
        with self.assertRaises(kokoro.KokoroUnavailable):
            self.make()

    def test_env_honored(self):
        os.environ[kokoro.VOICE_ENV] = "af_heart"
        self.assertEqual(self.make().voice, "af_heart")
        self.assertEqual(self.make(voice="bm_lewis").voice, "bm_lewis")

    def test_bad_voice_shape(self):
        Path(self.voice_file).write_bytes(np.zeros(100, np.float32).tobytes())
        with self.assertRaises(kokoro.KokoroUnavailable):
            self.make()
        Path(self.voice_file).write_bytes(b"")
        with self.assertRaises(kokoro.KokoroUnavailable):
            self.make()


class ModelConstantsTests(unittest.TestCase):
    def test_default_voice(self):
        self.assertEqual(kokoro.DEFAULT_VOICE, "am_adam")
        self.assertIn(kokoro.DEFAULT_VOICE, kokoro.VOICE_SHA256)

    def test_first_piece_budget(self):
        # 32 tokens ~ 0.27 s first audio on an idle M4 Max; 47 took 0.35 s (2026-10-02).
        self.assertEqual(kokoro.FIRST_PIECE_TOKENS, 32)

    def test_uses_fp32_model(self):
        # fp16 produced NaN audio on 2/8 sentences on CPU.
        self.assertEqual(kokoro.MODEL_FILE, "onnx/model.onnx")


class EnvTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(importlib.reload, kokoro)

    def test_telemetry_forced_off(self):
        os.environ["ORT_DISABLE_TELEMETRY"] = "0"
        importlib.reload(kokoro)
        self.assertEqual(os.environ["ORT_DISABLE_TELEMETRY"], "1")

    def test_hf_offline_default_not_overridden(self):
        os.environ.pop("HF_HUB_OFFLINE", None)
        importlib.reload(kokoro)
        self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
        os.environ["HF_HUB_OFFLINE"] = "0"
        importlib.reload(kokoro)
        self.assertEqual(os.environ["HF_HUB_OFFLINE"], "0")


class HubTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)

    def test_fetch_model(self):
        os.environ["HF_HUB_OFFLINE"] = "1"
        seen = {}

        def fake_download(*args, **kwargs):
            seen["args"], seen["kwargs"] = args, kwargs
            seen["offline"] = os.environ.get("HF_HUB_OFFLINE")

        hub = mock.Mock(snapshot_download=fake_download)
        with mock.patch.dict(sys.modules, {"huggingface_hub": hub}):
            kokoro.fetch_model()
        self.assertEqual(seen["args"], (kokoro.MODEL_REPO,))
        self.assertEqual(
            seen["kwargs"],
            {
                "revision": kokoro.MODEL_REVISION,
                "allow_patterns": ["onnx/model.onnx", "tokenizer.json", "voices/*.bin"],
            },
        )
        self.assertIsNone(seen["offline"])
        self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")

    def test_model_paths_missing(self):
        hub = mock.Mock(hf_hub_download=mock.Mock(side_effect=OSError("nope")))
        with mock.patch.dict(sys.modules, {"huggingface_hub": hub}):
            with self.assertRaisesRegex(kokoro.KokoroUnavailable, "models fetch"):
                kokoro.model_paths("am_michael")

    def test_model_paths_rejects_bad_voice_before_hub(self):
        hub = mock.Mock()
        with mock.patch.dict(sys.modules, {"huggingface_hub": hub}):
            with self.assertRaises(kokoro.KokoroUnavailable):
                kokoro.model_paths("../x")
        hub.hf_hub_download.assert_not_called()


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.files = {
            "onnx/model.onnx": b"model-bytes",
            "tokenizer.json": b"{}",
            "voices/am_michael.bin": b"voice-bytes",
        }
        for name, data in self.files.items():
            path = self.dir / name.replace("/", "_")
            path.write_bytes(data)
        sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
        for patch in (
            mock.patch.object(kokoro, "MODEL_SHA256", sha(self.files["onnx/model.onnx"])),
            mock.patch.object(kokoro, "TOKENIZER_SHA256", sha(self.files["tokenizer.json"])),
            mock.patch.dict(
                kokoro.VOICE_SHA256, {"am_michael": sha(self.files["voices/am_michael.bin"])}
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.hub = mock.Mock(
            hf_hub_download=mock.Mock(
                side_effect=lambda r, f, **kw: str(self.dir / f.replace("/", "_"))
            )
        )

    def paths(self, voice="am_michael"):
        with mock.patch.dict(sys.modules, {"huggingface_hub": self.hub}):
            return kokoro.model_paths(voice)

    def test_matching_hashes_return_paths(self):
        paths = self.paths()
        self.assertEqual(len(paths), 3)
        for call in self.hub.hf_hub_download.call_args_list:
            self.assertEqual(
                call.kwargs, {"revision": kokoro.MODEL_REVISION, "local_files_only": True}
            )

    def test_one_byte_change_rejected(self):
        (self.dir / "tokenizer.json").write_bytes(b"{ }")
        with self.assertRaisesRegex(kokoro.KokoroUnavailable, "tokenizer.json.*integrity"):
            self.paths()
        (self.dir / "tokenizer.json").write_bytes(b"{}")
        (self.dir / "voices_am_michael.bin").write_bytes(b"voice-byteX")
        with self.assertRaisesRegex(kokoro.KokoroUnavailable, "am_michael.bin.*integrity"):
            self.paths()

    def test_unknown_voice_rejected(self):
        with self.assertRaisesRegex(kokoro.KokoroUnavailable, "unknown"):
            self.paths("zz_nobody")


class RealConstantsTests(unittest.TestCase):
    def test_voice_pins(self):
        self.assertEqual(len(kokoro.VOICE_SHA256), 55)
        self.assertIn("am_michael", kokoro.VOICE_SHA256)
        for sha in (*kokoro.VOICE_SHA256.values(), kokoro.MODEL_SHA256, kokoro.TOKENIZER_SHA256):
            self.assertRegex(sha, r"^[0-9a-f]{64}$")


class StartupFailureTests(KokoroTestBase):
    def test_default_session_wraps_ort_errors(self):
        ort = mock.Mock()
        ort.InferenceSession.side_effect = RuntimeError("InvalidProtobuf")
        with mock.patch.dict(sys.modules, {"onnxruntime": ort}):
            with self.assertRaises(kokoro.KokoroUnavailable):
                kokoro._default_session("model.onnx")

    def test_warm_up_render_raises(self):
        v = self.make()
        self.session.run = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertRaisesRegex(kokoro.KokoroUnavailable, "warm-up"):
            v.warm_up()

    def test_warm_up_none(self):
        v = self.make(np.zeros(RATE, np.float32))
        with self.assertRaisesRegex(kokoro.KokoroUnavailable, "warm-up"):
            v.warm_up()

    def test_bad_vocabs(self):
        good = make_vocab()
        bad = [
            "a string",
            {**good, "ab": 5},
            {**good, "x": "7"},
            {**good, "x": True},
            {**good, "$": 1},
            {k: v for k, v in good.items() if k != "$"},
        ]
        for vocab in bad:
            self.tokenizer.write_text(json.dumps({"model": {"vocab": vocab}}))
            with self.assertRaises(kokoro.KokoroUnavailable, msg=str(vocab)):
                self.make()


@unittest.skipUnless(os.environ.get("UNDERTONE_TEST_REAL_KOKORO") == "1", "real model smoke test")
class RealModelSmoke(unittest.TestCase):
    def test_renders_audio(self):
        v = kokoro.KokoroVoice()
        v.warm_up()
        out = v.render("həlˈoʊ wˈɜːld.")
        self.assertTrue(out and len(out) % 2 == 0)


if __name__ == "__main__":
    unittest.main()

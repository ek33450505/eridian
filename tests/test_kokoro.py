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
        # Distinguishable from the reply voice: every style value is 1000 higher.
        self.own_file = d / "own.bin"
        (rows + 1000).tofile(self.own_file)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(kokoro.VOICE_ENV, None)
        os.environ.pop(kokoro.OWN_VOICE_ENV, None)

    def make(self, waveform=None, resample=lambda b: b, **kw):
        self.session = FakeSession(waveform)
        kw.setdefault("own_voice_path", str(self.own_file))
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
            paths=self.paths,
            session_factory=lambda _p: FakeSession(tone_clip()),
            own_voice_path=str(self.own_file),
        )
        out = v.render("ab.")
        n24 = int((0.02 + 0.2 + 0.05 + 0.25) * RATE) * 2
        self.assertEqual(len(out) % 2, 0)
        self.assertAlmostEqual(len(out) / n24, 2 / 3, delta=0.01)

    def test_warm_up_runs_session(self):
        v = self.make()
        v.warm_up()
        self.assertEqual(len(self.session.calls), 1)


class OwnVoiceTests(KokoroTestBase):
    def style_fed(self, **render_kw):
        v = self.make()
        v.render("ab.", **render_kw)
        return float(self.session.calls[-1]["style"][0][0])

    def test_reply_voice_is_the_default(self):
        self.assertLess(self.style_fed(), 1000)

    def test_own_styles_used_when_own(self):
        self.assertGreaterEqual(self.style_fed(own=True), 1000)

    def test_default_own_name(self):
        self.assertEqual(self.make().own_voice, "af_heart")
        self.assertEqual(kokoro.DEFAULT_OWN_VOICE, "af_heart")

    def test_env_override(self):
        os.environ[kokoro.OWN_VOICE_ENV] = "bf_emma"
        self.assertEqual(self.make().own_voice, "bf_emma")
        self.assertEqual(self.make(own_voice="bm_lewis").own_voice, "bm_lewis")

    def test_invalid_own_name_raises(self):
        for bad in ("../x", "AM_michael", "a_b"):
            with self.assertRaises(kokoro.KokoroUnavailable, msg=bad):
                self.make(own_voice=bad)
        os.environ[kokoro.OWN_VOICE_ENV] = "../x"
        with self.assertRaises(kokoro.KokoroUnavailable):
            self.make()

    def test_bad_shape_own_file_raises(self):
        self.own_file.write_bytes(np.zeros(100, np.float32).tobytes())
        with self.assertRaises(kokoro.KokoroUnavailable):
            self.make()
        self.own_file.write_bytes(b"")
        with self.assertRaises(kokoro.KokoroUnavailable):
            self.make()

    def test_missing_own_file_raises(self):
        with self.assertRaises(kokoro.KokoroUnavailable):
            self.make(own_voice_path=str(self.own_file) + ".gone")

    def test_absent_path_resolves_via_model_paths(self):
        with mock.patch.object(
            kokoro, "model_paths", return_value=("m", "t", str(self.own_file))
        ) as mp:
            v = kokoro.KokoroVoice(
                paths=self.paths, session_factory=lambda _p: FakeSession(), resample=lambda b: b
            )
        mp.assert_called_once_with("af_heart")
        v.render("ab.", own=True)

    def test_model_paths_failure_means_unavailable(self):
        with mock.patch.object(
            kokoro, "model_paths", side_effect=kokoro.KokoroUnavailable("not cached")
        ):
            with self.assertRaises(kokoro.KokoroUnavailable):
                kokoro.KokoroVoice(paths=self.paths, session_factory=lambda _p: FakeSession())


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


class VoiceBlendTests(KokoroTestBase):
    def setUp(self):
        super().setUp()
        d = Path(self._tmp.name)
        rows = np.arange(510, dtype=np.float32)[:, None] * np.ones((1, 256), np.float32)
        self.a, self.b = d / "a.bin", d / "b.bin"
        rows.tofile(self.a)
        (rows * 3 + 10).tofile(self.b)
        self.rows = rows

    def blend(self, spec, **kw):
        return self.make(
            voice=spec, voice_paths={"am_adam": str(self.a), "bm_george": str(self.b)}, **kw
        )

    def test_parse_valid(self):
        got = kokoro.parse_voice_spec("am_adam:0.6,bm_george:0.4")
        self.assertEqual([n for n, _ in got], ["am_adam", "bm_george"])
        self.assertAlmostEqual(got[0][1], 0.6)
        self.assertAlmostEqual(got[1][1], 0.4)
        self.assertEqual(kokoro.parse_voice_spec("am_adam"), [("am_adam", 1.0)])
        self.assertEqual(kokoro.voice_names("am_adam:3, bm_george:1"), ["am_adam", "bm_george"])

    def test_parse_normalises_weights(self):
        got = kokoro.parse_voice_spec("am_adam:3,bm_george:1")
        self.assertAlmostEqual(got[0][1], 0.75)

    def test_parse_rejects_bad_names_and_weights(self):
        for bad in (
            "../x:1,am_adam:1",
            "am_adam:1,AM_x:1",
            "am_adam:0,bm_george:1",
            "am_adam:-1,bm_george:1",
            "am_adam:nan,bm_george:1",
            "am_adam:inf,bm_george:1",
            "am_adam:abc,bm_george:1",
            "am_adam:,bm_george:1",
            "am_adam,bm_george:1",
            "am_adam:1,",
            "am_adam:1,am_adam:1",
        ):
            with self.assertRaises(kokoro.KokoroUnavailable, msg=bad):
                kokoro.parse_voice_spec(bad)

    def test_blend_is_weighted_sum(self):
        v = self.blend("am_adam:0.6,bm_george:0.4")
        expected = 0.6 * self.rows + 0.4 * (self.rows * 3 + 10)
        np.testing.assert_allclose(v._styles, expected, rtol=1e-5)
        self.assertEqual(v.voice_names, ["am_adam", "bm_george"])
        v.render("ab.")
        np.testing.assert_allclose(self.session.calls[0]["style"][0], expected[2], rtol=1e-5)

    def test_blend_shape_mismatch(self):
        np.zeros(256 * 3, np.float32).tofile(self.b)
        with self.assertRaises(kokoro.KokoroUnavailable):
            self.blend("am_adam:0.6,bm_george:0.4")

    def test_blend_bad_component_file(self):
        self.b.write_bytes(np.zeros(100, np.float32).tobytes())
        with self.assertRaises(kokoro.KokoroUnavailable):
            self.blend("am_adam:0.6,bm_george:0.4")

    def test_blend_hashes_model_once(self):
        calls = []
        with tempfile.TemporaryDirectory() as td, \
                mock.patch("huggingface_hub.hf_hub_download") as dl, \
                mock.patch.object(kokoro, "_sha256_file") as sha:
            def fake(repo, name, **kw):
                calls.append(name)
                return str(Path(td) / name.replace("/", "_"))
            dl.side_effect = fake
            sha.side_effect = lambda path: {
                **{kokoro.MODEL_FILE.replace("/", "_"): kokoro.MODEL_SHA256,
                   kokoro.TOKENIZER_FILE: kokoro.TOKENIZER_SHA256},
                **{f"voices_{n}.bin": h for n, h in kokoro.VOICE_SHA256.items()},
            }[Path(path).name]
            model, tok, files = kokoro._model_paths_multi(["am_adam", "bm_george", "am_adam"])
        self.assertEqual(calls.count(kokoro.MODEL_FILE), 1)
        self.assertEqual(sha.call_count, 4)  # model, tokenizer, two distinct voices
        self.assertEqual(len(files), 3)


def sine(freq, seconds=2.0, amp=0.4):
    t = np.arange(int(seconds * RATE)) / RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def peak_hz(x):
    sp = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    return float(np.argmax(sp)) / (x.size / RATE)


class ChordEffectTests(KokoroTestBase):
    def voice(self, effect):
        return self.make(sine(150), effect=effect)

    def pcm(self, v, **kw):
        return np.frombuffer(v.render("ab", **kw), np.int16).astype(np.float64)

    def test_changes_audio_and_keeps_peak(self):
        dry = self.pcm(self.voice(""))
        wet = self.pcm(self.voice("chord"))
        self.assertEqual(dry.size, wet.size)
        self.assertFalse(np.allclose(dry, wet, atol=100))
        self.assertLessEqual(np.abs(wet).max(), np.abs(dry).max() + 1)

    def test_skipped_for_own_voice(self):
        v = self.voice("chord")
        np.testing.assert_array_equal(self.pcm(v, own=True), self.pcm(self.voice(""), own=True))
        self.assertFalse(np.array_equal(self.pcm(v), self.pcm(v, own=True)))

    def test_env_switch(self):
        os.environ[kokoro.EFFECT_ENV] = "chord"
        self.assertEqual(self.make().effect, "chord")
        os.environ[kokoro.EFFECT_ENV] = "off"
        self.assertEqual(self.make().effect, "")
        del os.environ[kokoro.EFFECT_ENV]
        self.assertEqual(self.make().effect, "")
        os.environ[kokoro.EFFECT_ENV] = "bogus"
        with self.assertRaises(kokoro.KokoroUnavailable):
            self.make()

    def test_shift_pitch(self):
        out = kokoro._shift(sine(150).astype(np.float64), 4.0)[RATE // 2 : 3 * RATE // 2]
        self.assertAlmostEqual(peak_hz(out), 150 * 2 ** (4 / 12), delta=2.0)
        out = kokoro._shift(sine(150).astype(np.float64), 7.0)[RATE // 2 : 3 * RATE // 2]
        self.assertAlmostEqual(peak_hz(out), 150 * 2 ** (7 / 12), delta=2.0)

    def test_chord_has_all_three_pitches(self):
        wet = kokoro.chord_effect(sine(150))[RATE // 2 : 3 * RATE // 2]
        sp = np.abs(np.fft.rfft(wet * np.hanning(wet.size)))
        for semis in (0, 4, 7):
            f = 150 * 2 ** (semis / 12)
            band = sp[int(f * 0.95 * wet.size / RATE) : int(f * 1.05 * wet.size / RATE)]
            self.assertGreater(band.max(), sp.max() * 0.1, semis)

    def test_empty_input_unchanged(self):
        out = kokoro.chord_effect(np.zeros(0, np.float32))
        self.assertEqual(out.size, 0)

    def test_crossfade_has_no_clicks_at_delay_wrap(self):
        # Without the sin^2 crossfade each tap jumps at its wrap. 173 Hz, because the 960-sample
        # window is a whole number of 150 Hz periods, so that wrap would be invisible.
        out = kokoro._shift(sine(173, seconds=3.0).astype(np.float64), 4.0)[RATE // 2 :]
        self.assertLess(np.abs(np.diff(out)).max(), 0.05)

    def test_vibrato_modulates_pitch_but_stays_in_tune(self):
        x = sine(150, seconds=3.0).astype(np.float64)
        plain = kokoro._shift(x, 4.0)[RATE // 2 : 5 * RATE // 2]
        wobbly = kokoro._shift(x, 4.0, 5.3)[RATE // 2 : 5 * RATE // 2]
        rms = lambda a: float(np.sqrt(np.mean(a**2)))
        self.assertGreater(rms(plain - wobbly), 0.1 * rms(plain))
        self.assertAlmostEqual(peak_hz(wobbly), 150 * 2 ** (4 / 12), delta=4.0)

    def test_cost_under_budget(self):
        import time

        x = sine(150, seconds=4.0)
        kokoro.chord_effect(x)
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            kokoro.chord_effect(x)
            best = min(best, time.perf_counter() - t0)
        ms_per_s = best / 4.0 * 1000
        print(f"\nchord effect: {ms_per_s:.2f} ms per second of audio")
        self.assertLess(ms_per_s, 10.0)


class PitchTests(KokoroTestBase):
    def pcm(self, v, **kw):
        return np.frombuffer(v.render("ab", **kw), np.int16)

    def test_pitch_moves_fft_peak(self):
        for semis in (2.0, -3.0):
            v = self.make(sine(150, seconds=3.0), pitch=semis)
            out = self.pcm(v).astype(np.float64)[RATE // 2 : 5 * RATE // 2]
            self.assertAlmostEqual(peak_hz(out), 150 * 2 ** (semis / 12), delta=2.0, msg=semis)

    def test_zero_is_identity(self):
        # _shift(x, 0) is not identity (it delays by half a window), so the guard must skip it.
        wave = sine(150)
        with mock.patch.object(kokoro, "_shift", side_effect=AssertionError("shift called")) as sh:
            explicit = self.pcm(self.make(wave, pitch=0.0))
            unset = self.pcm(self.make(wave))
            os.environ[kokoro.PITCH_ENV] = "0"
            from_env = self.pcm(self.make(wave))
        sh.assert_not_called()
        np.testing.assert_array_equal(explicit, unset)
        np.testing.assert_array_equal(from_env, unset)
        # And the samples are the trimmed input itself, undelayed.
        loud = np.flatnonzero(np.abs(wave) > np.abs(wave).max() * 0.01)
        start = max(int(loud[0]) - int(0.02 * RATE), 0)
        end = min(int(loud[-1]) + 1 + int(0.05 * RATE), wave.size)
        expected = (np.clip(wave[start:end], -1, 1) * 32767).astype(np.int16)
        np.testing.assert_array_equal(unset[: expected.size], expected)

    def test_own_voice_skipped(self):
        np.testing.assert_array_equal(
            self.pcm(self.make(sine(150), pitch=3.0), own=True),
            self.pcm(self.make(sine(150)), own=True),
        )

    def test_chord_is_relative_to_shifted_root(self):
        v = self.make(sine(150, seconds=3.0), pitch=2.0, effect="chord")
        wet = self.pcm(v).astype(np.float64)[RATE // 2 : 5 * RATE // 2]
        sp = np.abs(np.fft.rfft(wet * np.hanning(wet.size)))
        root = 150 * 2 ** (2 / 12)
        for semis in (0, 4, 7):
            f = root * 2 ** (semis / 12)
            band = sp[int(f * 0.95 * wet.size / RATE) : int(f * 1.05 * wet.size / RATE)]
            self.assertGreater(band.max(), sp.max() * 0.1, semis)

    def test_env_and_bad_values(self):
        os.environ[kokoro.PITCH_ENV] = "1.5"
        self.assertEqual(self.make().pitch, 1.5)
        os.environ[kokoro.PITCH_ENV] = ""
        self.assertEqual(self.make().pitch, 0.0)
        for bad in ("7", "-6.5", "nan", "inf", "-inf", "abc"):
            os.environ[kokoro.PITCH_ENV] = bad
            with self.assertRaises(kokoro.KokoroUnavailable, msg=bad):
                self.make()
        self.assertEqual(self.make(pitch=6.0).pitch, 6.0)
        with self.assertRaises(kokoro.KokoroUnavailable):
            self.make(pitch=float("nan"))


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

    def test_blend_and_chord_sample(self):
        import wave

        pitch = float(os.environ.get("UNDERTONE_TEST_SAMPLE_PITCH", "0"))
        v = kokoro.KokoroVoice("am_adam:0.6,bm_george:0.4", effect="chord", pitch=pitch)
        out = v.render("hɛlˈoʊ, aɪ æm ɹˈɑki. aɪ wɪl hɛlp.")
        self.assertTrue(out and len(out) % 2 == 0)
        dest = os.environ.get("UNDERTONE_TEST_SAMPLE_WAV")
        if dest:
            with wave.open(dest, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(kokoro.OUT_RATE)
                w.writeframes(out)


if __name__ == "__main__":
    unittest.main()

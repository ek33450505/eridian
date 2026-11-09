"""Pure-function tests for training/microwakeword/{record_clips,smoke_test}.py.

The scripts are imported by path (they are not a package). No audio device, no
model: smoke_test only needs pymicro_wakeword importable (the repo venv).
"""

import importlib.util
import os
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parent.parent / "training" / "microwakeword"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rc = _load("record_clips")
st = _load("smoke_test")


def synth(speech_s=1.0, lead_s=1.0, tail_s=1.0, amp=8000, noise=20, seed=0):
    rng = np.random.default_rng(seed)
    n = lambda s: int(s * rc.RATE)
    total = n(lead_s) + n(speech_s) + n(tail_s)
    x = rng.normal(0, noise, total)
    t = np.arange(n(speech_s)) / rc.RATE
    x[n(lead_s) : n(lead_s) + n(speech_s)] += amp * np.sin(2 * np.pi * 220 * t)
    return np.clip(x, -32768, 32767).astype(np.int16)


class AnalyzeTakeTest(unittest.TestCase):
    def test_good_take_is_trimmed_with_pad(self):
        r = rc.analyze_take(synth())
        self.assertTrue(r.ok, r.message)
        secs = len(r.samples) / rc.RATE
        self.assertAlmostEqual(secs, 1.0 + 2 * 0.25, delta=0.05)

    def test_too_quiet(self):
        r = rc.analyze_take(synth(amp=300, noise=5))
        self.assertFalse(r.ok)
        self.assertIn("quiet", r.message)

    def test_clipped(self):
        x = synth()
        x[rc.RATE + 100 : rc.RATE + 105] = 32767
        r = rc.analyze_take(x)
        self.assertFalse(r.ok)
        self.assertIn("lipped", r.message)

    def test_too_short(self):
        r = rc.analyze_take(synth(speech_s=0.15))
        self.assertFalse(r.ok)
        self.assertIn("short", r.message)

    def test_cut_off_at_end(self):
        r = rc.analyze_take(synth(lead_s=1.0, speech_s=1.0, tail_s=0.0))
        self.assertFalse(r.ok)
        self.assertIn("end", r.message)

    def test_cut_off_at_start(self):
        r = rc.analyze_take(synth(lead_s=0.0, speech_s=1.0, tail_s=1.0))
        self.assertFalse(r.ok)
        self.assertIn("start", r.message)

    def test_silence_has_no_speech(self):
        x = np.zeros(rc.RATE * 2, dtype=np.int16)
        x[1000] = 600  # passes the quiet gate (-34.8 dBFS) but is below the speech floor
        r = rc.analyze_take(x)
        self.assertFalse(r.ok)
        self.assertIn("No speech", r.message)

    def test_empty(self):
        self.assertFalse(rc.analyze_take(np.zeros(0, dtype=np.int16)).ok)


class RootAndIndexTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.home = Path(self._td.name).resolve()

    def make_root(self, sentinel=True):
        root = self.home / "data" / "mww"
        root.mkdir(parents=True)
        if sentinel:
            (root / rc.SENTINEL).write_text("")
        return root

    def test_valid_root(self):
        root = self.make_root()
        self.assertEqual(rc.resolve_root(root, home=self.home), root)

    def test_missing_sentinel(self):
        root = self.make_root(sentinel=False)
        with self.assertRaises(rc.RootError):
            rc.resolve_root(root, home=self.home)

    def test_home_itself_and_outside_rejected(self):
        (self.home / rc.SENTINEL).write_text("")
        with self.assertRaises(rc.RootError):
            rc.resolve_root(self.home, home=self.home)
        with tempfile.TemporaryDirectory() as other:
            (Path(other) / rc.SENTINEL).write_text("")
            with self.assertRaises(rc.RootError):
                rc.resolve_root(other, home=self.home)

    def test_dotdot_escape_rejected(self):
        root = self.make_root()
        with self.assertRaises(rc.RootError):
            rc.resolve_root(root / ".." / ".." / "..", home=self.home)

    def test_next_index_and_count(self):
        d = self.home / "clips"
        self.assertEqual(rc.next_index(d), 1)
        d.mkdir()
        for i in (1, 2, 3):
            rc.write_wav(rc.clip_path(d, i), np.zeros(10, dtype=np.int16))
        (d / "notes.txt").write_text("x")
        self.assertEqual(rc.count_clips(d), 3)
        self.assertEqual(rc.next_index(d), 4)

    def test_next_index_never_reuses_gap(self):
        d = self.home / "clips"
        d.mkdir()
        for i in (1, 5):
            rc.write_wav(rc.clip_path(d, i), np.zeros(10, dtype=np.int16))
        self.assertEqual(rc.next_index(d), 6)

    def test_write_wav_is_exclusive_unless_overwrite(self):
        p = self.home / "b.wav"
        rc.write_wav(p, np.zeros(10, dtype=np.int16))
        with self.assertRaises(FileExistsError):
            rc.write_wav(p, np.ones(10, dtype=np.int16))
        rc.write_wav(p, np.ones(20, dtype=np.int16), overwrite=True)
        with wave.open(str(p)) as w:
            self.assertEqual(w.getnframes(), 20)

    def test_symlinked_real_dir_refused(self):
        root = self.make_root()
        outside = self.home / "elsewhere"
        outside.mkdir()
        (root / "real").symlink_to(outside)
        with self.assertRaises(rc.RootError):
            rc.safe_phrase_dir(root, "hey_rocky")
        with self.assertRaises(rc.RootError):
            rc.run_phrase("hey_rocky", root, 2, 2.5, lambda s: synth(), lambda p: "", lambda m: None)
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlinked_phrase_dir_refused(self):
        root = self.make_root()
        outside = self.home / "elsewhere"
        outside.mkdir()
        (root / "real" / "positives").mkdir(parents=True)
        (root / "real" / "positives" / "hey_rocky").symlink_to(outside)
        with self.assertRaises(rc.RootError):
            rc.safe_phrase_dir(root, "hey_rocky")
        self.assertEqual(rc.safe_phrase_dir(root, "hey_rock"), root / "real" / "confusables" / "hey_rock")

    def test_wav_format(self):
        p = self.home / "a.wav"
        rc.write_wav(p, np.arange(100, dtype=np.int16))
        with wave.open(str(p)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 16000))


class RunPhraseTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name).resolve()
        self.log = []

    def run_phrase(self, keys, takes, recordings, name="hey_rocky"):
        keys, recs = iter(keys), iter(recordings)
        return rc.run_phrase(name, self.root, takes, 2.5, lambda s: next(recs), lambda p: next(keys), self.log.append)

    def test_records_rejects_and_redoes(self):
        good, quiet = synth(seed=1), synth(amp=300, noise=5)
        # Enter (good), Enter (rejected, not counted), Enter (good), r (redo #2), Enter -> reaches 3
        saved, action = self.run_phrase(
            ["", "", "", "r", ""], 3, [good, quiet, good, synth(seed=2), good]
        )
        d = rc.phrase_dir(self.root, "hey_rocky")
        self.assertEqual((saved, action), (3, "done"))
        self.assertEqual(sorted(p.name for p in d.glob("*.wav")), ["0001.wav", "0002.wav", "0003.wav"])

    def test_redo_overwrites_only_last_written(self):
        d = rc.phrase_dir(self.root, "hey_rocky")
        d.mkdir(parents=True)
        old = rc.clip_path(d, 1)
        rc.write_wav(old, np.zeros(50, dtype=np.int16))
        before = old.read_bytes()
        saved, _ = self.run_phrase(["", "r", "q"], 5, [synth(seed=1), synth(seed=2)])
        self.assertEqual(saved, 1)
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in d.glob("*.wav")), ["0001.wav", "0002.wav"])

    def test_redo_with_nothing_written(self):
        saved, action = self.run_phrase(["r", "q"], 2, [])
        self.assertEqual((saved, action), (0, "quit"))
        self.assertTrue(any("Nothing to redo" in m for m in self.log))

    def test_resume_and_skip_when_full(self):
        d = rc.phrase_dir(self.root, "hey_rock")
        d.mkdir(parents=True)
        for i in (1, 2):
            rc.write_wav(rc.clip_path(d, i), np.zeros(10, dtype=np.int16))
        self.assertEqual(self.run_phrase([], 2, [], name="hey_rock"), (0, "done"))
        saved, _ = self.run_phrase([""], 3, [synth()], name="hey_rock")
        self.assertEqual(saved, 1)
        self.assertTrue(rc.clip_path(d, 3).exists())

    def test_skip_and_eof(self):
        self.assertEqual(self.run_phrase(["s"], 3, []), (0, "skip"))

        def eof(_):
            raise EOFError

        self.assertEqual(rc.run_phrase("hey_rocky", self.root, 3, 2.5, None, eof, self.log.append), (0, "quit"))


class SaturationTest(unittest.TestCase):
    def test_saturated_negative_has_no_cutoff(self):
        self.assertTrue(st.is_saturated(st.SATURATION))
        self.assertTrue(st.is_saturated(0.996))
        self.assertIsNone(st.suggest_cutoff(0.996))
        self.assertIsNone(st.suggest_cutoff(st.SATURATION))

    def test_suggestion_strictly_between(self):
        for worst in (0.0, 0.3, 0.54, 0.95, 0.97, 0.985, 0.993):
            c = st.suggest_cutoff(worst)
            self.assertIsNotNone(c, worst)
            self.assertLess(worst, c, worst)
            self.assertLess(c, st.SATURATION, worst)

    def test_typical_gap_is_plus_002(self):
        self.assertEqual(st.suggest_cutoff(0.54), 0.56)

    def test_never_bogus_099_when_negatives_near_ceiling(self):
        c = st.suggest_cutoff(0.992)
        self.assertLess(c, st.SATURATION)
        self.assertGreater(c, 0.992)


class CeilingTest(unittest.TestCase):
    def test_ceiling_from_model_scale(self):
        class M:
            output_dtype = np.dtype(np.uint8)
            output_scale = 1 / 256
            output_zero_point = 0

        self.assertAlmostEqual(st.output_ceiling(M()), 255 / 256)
        M.output_scale = 1 / 128
        M.output_zero_point = 127
        self.assertAlmostEqual(st.output_ceiling(M()), 128 / 128)

    def test_ceiling_falls_back(self):
        self.assertEqual(st.output_ceiling(object()), st.SATURATION)

    def test_custom_ceiling_used(self):
        self.assertTrue(st.is_saturated(0.9, ceiling=0.9))
        self.assertIsNone(st.suggest_cutoff(0.9, ceiling=0.9))
        c = st.suggest_cutoff(0.5, ceiling=0.9)
        self.assertTrue(0.5 < c < 0.9)


class RollingWindowTest(unittest.TestCase):
    def test_rolling_means(self):
        r = st.rolling_means([0, 1, 1, 1, 0], 3)
        np.testing.assert_allclose(r, [2 / 3, 1.0, 2 / 3])

    def test_window_one_is_identity(self):
        np.testing.assert_allclose(st.rolling_means([0.1, 0.9], 1), [0.1, 0.9])

    def test_short_clip_never_reports(self):
        self.assertEqual(len(st.rolling_means([1.0, 1.0], 3)), 0)
        self.assertEqual(st.clip_max([1.0, 1.0], 3), 0.0)

    def test_bad_window(self):
        with self.assertRaises(ValueError):
            st.rolling_means([1.0], 0)

    def test_clip_max_shrinks_with_window(self):
        infs = [0.0] * 5 + [1.0] * 4 + [0.0] * 5
        self.assertEqual(st.clip_max(infs, 4), 1.0)
        self.assertAlmostEqual(st.clip_max(infs, 8), 0.5)

    def test_count_over_is_strict(self):
        self.assertEqual(st.count_over([0.97, 0.98], 0.97), 1)


class StreamInferencesTest(unittest.TestCase):
    """The skipped-call counting must keep genuine 0.0 outputs and drop padding."""

    def test_counts_by_stride_not_value(self):
        class FakeRaw:
            stride = 3

            def __init__(self):
                self.n = 0

            def reset(self):
                self.n = 0

            def process_streaming_prob(self, _):
                self.n += 1
                return 0.0  # every inference output is a genuine 0.0

        class FakeFeats:
            def process_streaming(self, chunk):
                return [chunk]

        orig = st.MicroWakeWordFeatures
        st.MicroWakeWordFeatures = FakeFeats
        try:
            out = st.stream_inferences(FakeRaw(), np.zeros(1600, dtype=np.int16))
        finally:
            st.MicroWakeWordFeatures = orig
        total_calls = (1600 + 2 * rc.RATE) * 2 // st.CHUNK_BYTES
        self.assertEqual(len(out), total_calls // 3)


class ReportFormattingTest(unittest.TestCase):
    pos = [np.array([0, 1, 1, 1, 1, 1, 1, 0.0])] * 2
    conf = [Path("hey_rock_a.wav"), Path("hey_rock_b.wav"), Path("rocky_road_a.wav")]
    conf_inf = [np.array([1.0] * 6), np.zeros(6), np.zeros(6)]
    neg = [np.zeros(8)] * 3

    def test_sweep_table(self):
        lines = st.sweep_lines(5, 0.9, self.conf, self.pos, self.conf_inf, self.neg, {}, windows=(5, 8))
        self.assertIn("(c) neg clips w/ det", lines[0])
        self.assertNotIn("(d)", lines[0])
        # window 5: both positives have 6 ones in a row; window 8 never fills enough
        self.assertEqual(lines[1].split("|")[0].strip(), "5*")
        self.assertIn("2/2", lines[1])
        self.assertIn("1/3", lines[1])
        self.assertIn("0/3", lines[1])
        self.assertEqual(lines[2].split("|")[0].strip(), "8")
        self.assertIn("0/2", lines[2])
        text = "\n".join(lines)
        self.assertRegex(text, r"hey_rock\s+5:1\s+8:0")
        self.assertRegex(text, r"rocky_road\s+5:0\s+8:0")

    def test_sweep_table_with_real_clips(self):
        mine_inf = {("positives", "hey_rocky"): [np.ones(8)], ("confusables", "hey_lucky"): [np.zeros(8), np.ones(8)]}
        lines = st.sweep_lines(5, 0.9, self.conf, self.pos, self.conf_inf, self.neg, mine_inf, windows=(5,))
        self.assertIn("(d) pos det", lines[0])
        self.assertIn("1/1", lines[1])
        self.assertIn("1/2", lines[1])
        self.assertRegex("\n".join(lines), r"real:hey_lucky\s+5:1")

    def test_real_report(self):
        root = Path("/x/mww")
        self.assertIn("no /x/mww/real directory", st.real_report_lines(root, None, {}, 5, 0.9, 10)[0])
        empty = {"positives": {}, "confusables": {}}
        self.assertIn("no clips yet", st.real_report_lines(root, empty, {}, 5, 0.9, 10)[0])
        mine = {"positives": {"hey_rocky": [Path("1.wav"), Path("2.wav")]},
                "confusables": {"hey_rock": [Path("1.wav")]}}
        inf = {("positives", "hey_rocky"): [np.ones(8), np.zeros(8)], ("confusables", "hey_rock"): [np.ones(8)]}
        lines = st.real_report_lines(root, mine, inf, 5, 0.9, 10)
        self.assertIn("closest offline proxy for live", lines[0])
        self.assertRegex(lines[1], r"hey_rocky\s+detected=1/2")
        self.assertRegex(lines[2], r"hey_rock\s+false accepts=1/1")


MODEL_ROOT = Path(os.environ.get("MWW_ROOT", "/nonexistent-mww-root"))


@unittest.skipUnless((MODEL_ROOT / "out" / "hey_rocky.tflite").exists(), "trained model not present")
class ModelEquivalenceTest(unittest.TestCase):
    """Window-1 capture + rolling means must equal what MicroWakeWord itself
    reports at the manifest window, elementwise and in count."""

    def test_matches_manifest_window(self):
        import json

        pos = sorted((MODEL_ROOT / "heldout" / "positives").glob("*.wav"))[:2]
        conf = sorted((MODEL_ROOT / "heldout" / "confusables").glob("*.wav"))[:2]
        if not pos or not conf:
            self.skipTest("no heldout clips")
        cfg_path = MODEL_ROOT / "out" / "hey_rocky.json"
        cfg = json.loads(cfg_path.read_text())
        window = cfg["micro"]["sliding_window_size"]
        ref = st.MicroWakeWord.from_config(cfg_path)
        self.addCleanup(ref.close)
        with tempfile.TemporaryDirectory() as td:
            raw = st.load_window1_model(cfg_path, cfg, Path(td))
            self.addCleanup(raw.close)
            for p in pos + conf:
                samples = st.read_wav16k(p)
                infs = st.stream_inferences(raw, samples)
                ref.reset()
                feats = st.MicroWakeWordFeatures()
                audio = np.concatenate([np.zeros(st.TARGET_RATE, np.int16), samples,
                                        np.zeros(st.TARGET_RATE, np.int16)]).tobytes()
                outs, calls = [], 0
                for i in range(0, len(audio), st.CHUNK_BYTES):
                    for f in feats.process_streaming(audio[i : i + st.CHUNK_BYTES]):
                        v = ref.process_streaming_prob(f)
                        calls += 1
                        if calls % ref.stride == 0:
                            outs.append(v)
                self.assertEqual(len(outs), len(infs), p.name)
                expected = np.array(outs[window - 1 :])  # first window-1 are fill padding
                got = st.rolling_means(infs, window)
                self.assertEqual(len(got), len(expected), p.name)
                np.testing.assert_allclose(got, expected, atol=1e-5, err_msg=p.name)


class RealHeldoutTest(unittest.TestCase):
    def test_first_n_sorted_per_phrase(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for i in range(1, 6):
                p = root / "real" / "positives" / "hey_rocky" / f"{i:04d}.wav"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"")
            c = root / "real" / "confusables" / "hey_rock"
            c.mkdir(parents=True)
            (c / "0001.wav").write_bytes(b"")
            out = st.real_heldout(root, 3)
            self.assertEqual([p.name for p in out["positives"]["hey_rocky"]], ["0001.wav", "0002.wav", "0003.wav"])
            self.assertEqual(len(out["confusables"]["hey_rock"]), 1)

    def test_absent_dir_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(st.real_heldout(td, 10), {"positives": {}, "confusables": {}})

    def test_phrase_of_uses_last_underscore(self):
        self.assertEqual(st.phrase_of(Path("rocky_road_0003.wav")), "rocky_road")

    def test_read_real_heldout(self):
        self.assertEqual(st.read_real_heldout({}), 10)
        self.assertEqual(st.read_real_heldout({"REAL_HELDOUT": "4"}), 4)
        self.assertEqual(st.read_real_heldout({"REAL_HELDOUT": "010"}), 10)
        self.assertEqual(st.read_real_heldout({"REAL_HELDOUT": "0"}), 0)
        self.assertEqual(st.read_real_heldout({"REAL_HELDOUT": "999999999"}), 999999999)
        for bad in ("abc", "-1", "1.5", "", " 3", "\uff11\uff12", "1\n", "1234567890"):
            with self.assertRaises(SystemExit, msg=repr(bad)):
                st.read_real_heldout({"REAL_HELDOUT": bad})


if __name__ == "__main__":
    unittest.main()

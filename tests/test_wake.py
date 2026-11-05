"""Tests for eridian.wake: the configurable wake word and every surface that reads it."""

import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eridian import cli, listener, mcp_channel, speaker, spoken, voice_guard, wake


def write_manifest(directory: str, phrase: str = "Hey Rocky", **overrides) -> Path:
    manifest = {
        "type": "micro",
        "wake_word": phrase,
        "model": "hey_rocky.tflite",
        "micro": {"probability_cutoff": 0.9, "sliding_window_size": 5},
    }
    manifest.update(overrides)
    path = Path(directory) / "hey_rocky.json"
    path.write_text(json.dumps(manifest))
    path.chmod(0o644)
    model = Path(directory) / "hey_rocky.tflite"
    if manifest.get("model") == "hey_rocky.tflite" and not model.exists():
        model.write_bytes(b"\0")
        model.chmod(0o644)
    return path


def fresh_dir(base: str, name: str) -> str:
    d = Path(base) / name
    d.mkdir()
    return str(d)


class WakeTestCase(unittest.TestCase):
    """Isolates the env: no inherited wake config, ERIDIAN_HOME in a tempdir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = {k: v for k, v in os.environ.items() if k not in (wake.MODEL_ENV, wake.PHRASE_ENV)}
        env["ERIDIAN_HOME"] = self._tmp.name
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use_rocky(self) -> Path:
        path = write_manifest(self._tmp.name)
        os.environ[wake.MODEL_ENV] = str(path)
        return path


class ResolveTest(WakeTestCase):
    def test_default_is_hey_jarvis(self) -> None:
        w = wake.resolve()
        self.assertEqual((w.phrase, w.name, w.model_path), ("hey jarvis", "jarvis", None))
        self.assertTrue(w.is_default)

    def test_manifest_sets_phrase_and_name(self) -> None:
        self.use_rocky()
        w = wake.resolve()
        self.assertEqual((w.phrase, w.name), ("hey rocky", "rocky"))
        self.assertFalse(w.is_default)

    def test_loads_come_from_a_private_copy(self) -> None:
        src = self.use_rocky()
        w = wake.resolve()
        copy = w.model_path
        self.assertNotEqual(copy, src)
        self.assertEqual(copy.name, "manifest.json")
        self.assertEqual(copy.parent.parent, Path(self._tmp.name) / "wake-model")
        self.assertEqual(stat.S_IMODE(copy.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
        self.assertEqual(copy.read_bytes(), src.read_bytes())
        self.assertEqual((copy.parent / "hey_rocky.tflite").read_bytes(), b"\0")
        # Same content resolves to the same copy; changing the source afterwards does not touch it.
        self.assertEqual(wake.resolve().model_path, copy)
        (src.parent / "hey_rocky.tflite").write_bytes(b"swapped")
        self.assertEqual((copy.parent / "hey_rocky.tflite").read_bytes(), b"\0")

    def test_tampered_copy_is_repaired(self) -> None:
        self.use_rocky()
        copy = wake.resolve().model_path
        (copy.parent / "hey_rocky.tflite").chmod(0o600)
        (copy.parent / "hey_rocky.tflite").write_bytes(b"evil")
        self.assertEqual(wake.resolve().model_path, copy)
        self.assertEqual((copy.parent / "hey_rocky.tflite").read_bytes(), b"\0")

    def test_empty_or_partial_copy_dir_is_repaired(self) -> None:
        self.use_rocky()
        copy = wake.resolve().model_path
        for victim in (None, "manifest.json"):
            with self.subTest(victim=victim):
                for f in copy.parent.iterdir():
                    if victim is None or f.name == victim:
                        f.unlink()
                self.assertEqual(wake.resolve().model_path, copy)
                self.assertEqual((copy.parent / "hey_rocky.tflite").read_bytes(), b"\0")
                self.assertTrue(copy.is_file())

    def test_extra_file_in_copy_dir_is_repaired(self) -> None:
        self.use_rocky()
        copy = wake.resolve().model_path
        (copy.parent / "extra").write_bytes(b"x")
        wake.resolve()
        self.assertFalse((copy.parent / "extra").exists())

    def test_symlinked_copy_dir_is_refused(self) -> None:
        self.use_rocky()
        copy = wake.resolve().model_path
        other = Path(fresh_dir(self._tmp.name, "elsewhere"))
        shutil.rmtree(copy.parent)
        copy.parent.symlink_to(other)
        with self.assertRaises(wake.WakeWordError) as ctx:
            wake.resolve()
        self.assertIn("symlink", str(ctx.exception))
        self.assertTrue(other.is_dir())  # nothing was deleted through the link

    def test_symlinked_wake_model_root_is_refused(self) -> None:
        other = Path(fresh_dir(self._tmp.name, "elsewhere"))
        other.chmod(0o755)
        (Path(self._tmp.name) / "wake-model").symlink_to(other)
        self.use_rocky()
        with self.assertRaises(wake.WakeWordError):
            wake.resolve()
        self.assertEqual(stat.S_IMODE(other.stat().st_mode), 0o755)  # chmod did not follow the link
        self.assertEqual(list(other.iterdir()), [])

    def test_regular_file_at_copy_path_is_not_a_directory(self) -> None:
        self.use_rocky()
        copy = wake.resolve().model_path
        shutil.rmtree(copy.parent)
        copy.parent.write_bytes(b"x")
        copy.parent.chmod(0o600)  # owned, private-looking: only the type check can reject it
        self.assertEqual(
            wake._copy_problem(copy.parent, {"manifest.json": b"x"}), "not a private directory"
        )
        with self.assertRaises(wake.WakeWordError):
            wake.resolve()

    def test_remove_copy_is_narrow(self) -> None:
        root = Path(self._tmp.name) / "wake-model"
        root.mkdir()
        for name in ("not-a-hash", "../escape", "A" * 64):
            with self.subTest(name), self.assertRaises(wake.WakeWordError):
                wake._remove_copy(root, root / name)

    def test_model_name_edge_cases_are_refused(self) -> None:
        for label, model in (
            ("case-folded manifest name", "Manifest.JSON"),
            ("NUL", "a\0b.tflite"),
            ("newline", "a\nb.tflite"),
        ):
            with self.subTest(label):
                path = write_manifest(fresh_dir(self._tmp.name, label.replace(" ", "")), model=model)
                self.assertRefused(path)

    def test_os_errors_while_copying_become_wake_errors(self) -> None:
        path = write_manifest(fresh_dir(self._tmp.name, "oserr"))
        with mock.patch.object(wake, "_stage", side_effect=OSError("disk full")):
            self.assertRefused(path, contains="disk full")

    def test_empty_env_counts_as_unset(self) -> None:
        os.environ[wake.MODEL_ENV] = ""
        self.assertTrue(wake.resolve().is_default)

    def test_phrase_env_is_used_without_a_model(self) -> None:
        os.environ[wake.PHRASE_ENV] = "Hey Rocky"
        self.assertEqual(wake.resolve().phrase, "hey rocky")

    def assertRefused(self, path, *, contains: str = "") -> None:
        with self.assertRaises(wake.WakeWordError) as ctx:
            wake.resolve({wake.MODEL_ENV: str(path)})
        self.assertIn(contains, str(ctx.exception))

    def test_invalid_manifests_fail_closed(self) -> None:
        base = self._tmp.name
        d = Path(fresh_dir(base, "plain"))
        (d / "bad.json").write_text("{nope")
        (d / "bad.json").chmod(0o644)
        cases = {
            "missing": d / "absent.json",
            "bad json": d / "bad.json",
            "directory": d,
            "no model key": write_manifest(fresh_dir(base, "a"), model=None),
            "model path escapes": write_manifest(fresh_dir(base, "b"), model="../x.tflite"),
            "model is the manifest name": write_manifest(fresh_dir(base, "b2"), model="manifest.json"),
            "model file missing": write_manifest(fresh_dir(base, "c"), model="gone.tflite"),
            "no micro": write_manifest(fresh_dir(base, "d"), micro={}),
            "bad phrase": write_manifest(fresh_dir(base, "e"), phrase="x; rm -rf"),
            "no phrase": write_manifest(fresh_dir(base, "f"), phrase=None),
            "confirm in phrase": write_manifest(fresh_dir(base, "g"), phrase="hey confirm"),
            "jarvis in custom phrase": write_manifest(fresh_dir(base, "h"), phrase="okay jarvis"),
        }
        for label, path in cases.items():
            with self.subTest(label):
                self.assertRefused(path)

    def test_symlinked_manifest_is_refused(self) -> None:
        real = write_manifest(fresh_dir(self._tmp.name, "real"))  # a perfectly valid manifest
        d = Path(fresh_dir(self._tmp.name, "linked"))
        (d / "hey_rocky.tflite").write_bytes(b"\0")
        (d / "hey_rocky.tflite").chmod(0o644)
        (d / "link.json").symlink_to(real)
        self.assertRefused(d / "link.json")

    def test_symlinked_model_is_refused(self) -> None:
        real = Path(fresh_dir(self._tmp.name, "real"))
        (real / "m.tflite").write_bytes(b"\0")
        (real / "m.tflite").chmod(0o644)
        d = fresh_dir(self._tmp.name, "linked")
        path = write_manifest(d, model="m.tflite")
        (Path(d) / "m.tflite").symlink_to(real / "m.tflite")
        self.assertRefused(path)

    def test_foreign_owner_is_refused(self) -> None:
        path = write_manifest(fresh_dir(self._tmp.name, "own"))
        self.assertEqual(wake.resolve({wake.MODEL_ENV: str(path)}).phrase, "hey rocky")
        with mock.patch.object(wake.os, "geteuid", return_value=os.geteuid() + 1):
            self.assertRefused(path, contains="not owned")

    def test_group_or_other_writable_is_refused(self) -> None:
        for target in ("manifest", "model", "dir"):
            with self.subTest(target):
                d = fresh_dir(self._tmp.name, f"w-{target}")
                path = write_manifest(d)
                victim = {"manifest": path, "model": Path(d) / "hey_rocky.tflite", "dir": Path(d)}[target]
                victim.chmod(0o666 if victim.is_file() else 0o777)
                self.assertRefused(path, contains="writable")

    def test_relative_path_is_refused(self) -> None:
        self.assertRefused("hey_rocky.json", contains="absolute")

    def test_tilde_path_is_expanded(self) -> None:
        d = fresh_dir(self._tmp.name, "tilde")
        write_manifest(d)
        with mock.patch.dict(os.environ, {"HOME": self._tmp.name}):
            w = wake.resolve({wake.MODEL_ENV: "~/tilde/hey_rocky.json"})
        self.assertEqual(w.phrase, "hey rocky")

    def test_bad_phrase_env_fails(self) -> None:
        with self.assertRaises(wake.WakeWordError):
            wake.resolve({wake.PHRASE_ENV: "hey \"; drop"})

    def test_load_model_wraps_from_config_errors(self) -> None:
        self.use_rocky()
        fake = mock.MagicMock()
        fake.MicroWakeWord.from_config.side_effect = RuntimeError("boom")
        with mock.patch.dict(sys.modules, {"pymicro_wakeword": fake}):
            with self.assertRaises(wake.WakeWordError):
                wake.resolve().load_model()


class SurfacesTest(WakeTestCase):
    def test_default_scrub_and_strip_unchanged(self) -> None:
        self.assertEqual(spoken.scrub_wake_word("say Hey Jarvis now"), "say the wake word now")
        self.assertEqual(spoken.scrub_wake_word("J.A.R.V.I.S. hi"), "the wake word. hi")
        self.assertEqual(listener.strip_wake_phrase("Hey Jarvis, what time is it"), "what time is it")

    def test_scrub_hey_rocky_variants(self) -> None:
        self.use_rocky()
        for text in ("Hey Rocky", "hey, rocky", "H.E.Y. R.O.C.K.Y.", "hay rocki", "HEY  ROCKY!"):
            with self.subTest(text):
                self.assertIn("the wake word", spoken.scrub_wake_word(text))
                self.assertNotIn("rock", spoken.scrub_wake_word(text).lower())

    def test_scrub_keeps_bare_name_and_old_word(self) -> None:
        self.use_rocky()
        self.assertEqual(spoken.scrub_wake_word("Rocky road is tasty"), "Rocky road is tasty")
        self.assertEqual(spoken.scrub_wake_word("the hey rockyfoo"), "the hey rockyfoo")
        self.assertEqual(spoken.scrub_wake_word("Hey Jarvis"), "Hey Jarvis")

    def test_prepare_speech_scrubs_configured_phrase(self) -> None:
        self.use_rocky()
        self.assertEqual(speaker.prepare_speech("ok hey rocky ok"), "ok the wake word ok")

    def test_scrub_hi_homophone(self) -> None:
        self.use_rocky()
        self.assertEqual(spoken.scrub_wake_word("Hi Rocky"), "the wake word")

    def test_strip_multi_word_phrase(self) -> None:
        os.environ[wake.MODEL_ENV] = str(
            write_manifest(fresh_dir(self._tmp.name, "big"), phrase="hey big rocky")
        )
        for heard, rest in (
            ("Hey big Rocky, open it", "open it"),
            ("big rocky open it", "open it"),
            ("Hey Rocky open it", "open it"),
            ("rocky.", ""),
        ):
            with self.subTest(heard):
                self.assertEqual(listener.strip_wake_phrase(heard), rest)

    def test_strip_wake_phrase_for_rocky(self) -> None:
        self.use_rocky()
        self.assertEqual(listener.strip_wake_phrase("Hey Rocky, open the file"), "open the file")
        self.assertEqual(listener.strip_wake_phrase("Hey Rocky."), "")
        self.assertEqual(listener.strip_wake_phrase("Hey Jarvis, open"), "Hey Jarvis, open")

    def test_instructions_use_configured_phrase(self) -> None:
        w = wake.WakeWord("hey rocky", Path("/x.json"))
        for safe in (False, True):
            with self.subTest(safe_phrase=safe):
                text = mcp_channel.build_instructions(safe, w)
                self.assertIn('not even "hey rocky"', text)
                self.assertIn('own name ("rocky")', text)
                self.assertNotIn("jarvis", text.lower())
                self.assertNotIn("{", text)
        self.assertIn('strips the wake word "hey rocky"', mcp_channel.build_instructions(False, w))

    def test_default_instructions_keep_original_wording(self) -> None:
        text = mcp_channel.INSTRUCTIONS
        self.assertIn('(not even "jarvis")', text)
        self.assertNotIn("own name", text)
        self.assertIn('strips the wake word "hey jarvis"', text)

    def test_safe_phrase_rejects_wake_words(self) -> None:
        self.use_rocky()
        with self.assertRaises(ValueError):
            voice_guard.validate_safe_phrase("copper rocky")
        self.assertEqual(voice_guard.validate_safe_phrase("copper maple"), "copper maple")

    def test_hint_lines_use_configured_phrase(self) -> None:
        self.use_rocky()
        io_ = mock.Mock(talk_over=False, voice_label="v")
        self.assertIn("'hey rocky'", cli.banner_line(io_))

    def test_channel_mcp_config_passes_phrase_not_model(self) -> None:
        home = Path(self._tmp.name)
        env = cli.channel_mcp_config("py", home, "tok")["mcpServers"]["eridian"]["env"]
        self.assertNotIn(wake.PHRASE_ENV, env)
        path = self.use_rocky()
        env = cli.channel_mcp_config("py", home, "tok")["mcpServers"]["eridian"]["env"]
        self.assertEqual(env[wake.PHRASE_ENV], "hey rocky")
        self.assertNotIn(str(path), json.dumps(env))

    def test_channel_never_reads_the_manifest(self) -> None:
        os.environ[wake.MODEL_ENV] = str(Path(self._tmp.name) / "absent.json")
        self.assertTrue(wake.resolve_phrase_only().is_default)
        self.assertEqual(mcp_channel.build_instructions(False, wake.current_phrase_only()), mcp_channel.INSTRUCTIONS)
        os.environ[wake.PHRASE_ENV] = "hey rocky"
        self.assertEqual(wake.resolve_phrase_only().phrase, "hey rocky")
        out = io.StringIO()
        server = mcp_channel.ChannelServer(io.StringIO(), out, Path(self._tmp.name) / "s.sock")
        server._handle_initialize(1, {})
        self.assertIn('not even "hey rocky"', json.loads(out.getvalue())["result"]["instructions"])
        with mock.patch.object(wake, "from_manifest", side_effect=AssertionError("read manifest")):
            with mock.patch.object(mcp_channel.ChannelServer, "run", return_value=0):
                self.assertEqual(mcp_channel.main(), 0)

    def test_channel_instructions_follow_phrase_env(self) -> None:
        os.environ[wake.PHRASE_ENV] = "hey rocky"
        out = io.StringIO()
        server = mcp_channel.ChannelServer(io.StringIO(), out, Path(self._tmp.name) / "s.sock")
        server._handle_initialize(1, {})
        self.assertIn('not even "hey rocky"', json.loads(out.getvalue())["result"]["instructions"])

    def test_channel_main_rejects_bad_phrase_env(self) -> None:
        os.environ[wake.PHRASE_ENV] = "bad;phrase"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mcp_channel.main(), 1)


class DetectorTest(WakeTestCase):
    def _fake(self, cutoff: float):
        fake = mock.MagicMock()
        fake.MicroWakeWord.from_config.return_value = mock.MagicMock(probability_cutoff=cutoff)
        return fake

    def test_factory_loads_configured_manifest(self) -> None:
        self.use_rocky()
        copy = wake.resolve().model_path  # loads read the private copy, never the source
        fake = self._fake(0.9)
        with mock.patch.dict(sys.modules, {"pymicro_wakeword": fake}):
            cli._make_wake_factory()()
        fake.MicroWakeWord.from_config.assert_called_with(copy)
        self.assertEqual(fake.MicroWakeWord.from_config.call_count, 1)  # startup load is reused
        fake.MicroWakeWord.from_builtin.assert_not_called()

    def test_default_factory_uses_builtin(self) -> None:
        fake = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"pymicro_wakeword": fake}):
            cli._make_wake_factory()()
        fake.MicroWakeWord.from_builtin.assert_called()
        fake.MicroWakeWord.from_config.assert_not_called()

    def test_screen_cutoff_never_looser_than_model(self) -> None:
        self.use_rocky()
        fake = self._fake(0.5)
        with mock.patch.dict(sys.modules, {"pymicro_wakeword": fake}):
            cli._make_wake_factory(probability_cutoff=0.8)()
        self.assertEqual(fake.MicroWakeWord.from_config.return_value.probability_cutoff, 0.5)

    def test_unloadable_model_fails_at_startup_not_to_jarvis(self) -> None:
        self.use_rocky()
        fake = mock.MagicMock()
        fake.MicroWakeWord.from_config.side_effect = RuntimeError("boom")
        with mock.patch.dict(sys.modules, {"pymicro_wakeword": fake}):
            with self.assertRaises(wake.WakeWordError):
                cli._make_wake_factory()
        fake.MicroWakeWord.from_builtin.assert_not_called()

    def test_main_exits_on_invalid_manifest(self) -> None:
        os.environ[wake.MODEL_ENV] = str(Path(self._tmp.name) / "absent.json")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["attach"])
        self.assertEqual(rc, 1)
        self.assertIn("absent.json", err.getvalue())


if __name__ == "__main__":
    unittest.main()

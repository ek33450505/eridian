"""Tests for eridian.memory: the personal-only cross-session memory store."""

from __future__ import annotations

import fcntl
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eridian import memory

SRC = str(Path(__file__).resolve().parent.parent / "src")
ORIGINAL_CWD = os.getcwd()


class MemoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.work = self.root / "work"
        self.work.mkdir()
        # cwd is a harmless sibling dir by default so the deny check passes
        self.cwd = self.root / "personal"
        self.cwd.mkdir()
        patcher = mock.patch.dict(os.environ, {"ERIDIAN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(os.chdir, ORIGINAL_CWD)
        os.chdir(self.cwd)

    def run_main(self, *argv: str, deny: Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        full = ["--deny-under", str(deny or self.work), *argv]
        try:
            code = memory.main(full, stdout=out, stderr=err)
        except SystemExit as exc:  # argparse errors
            code = exc.code
        return code, out.getvalue(), err.getvalue()

    def add(self, text: str, kind: str = "fact") -> int:
        return self.run_main("add", "--kind", kind, text)[0]

    @property
    def store(self) -> Path:
        return self.home / "memory.jsonl"


class RoundTripTests(MemoryTestCase):
    def test_add_then_list_exact_format(self) -> None:
        code, out, _ = self.run_main("add", "--kind", "win", "shipped", "the", "thing")
        self.assertEqual((code, out), (0, "saved (1 entries)\n"))
        self.add("second", "joke")
        _, out, _ = self.run_main("list")
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^1\. \d{4}-\d\d-\d\d win: shipped the thing$")
        self.assertRegex(lines[1], r"^2\. \d{4}-\d\d-\d\d joke: second$")

    def test_list_empty_store_prints_nothing(self) -> None:
        self.assertEqual(self.run_main("list"), (0, "", ""))

    def test_bad_kind_exits_2(self) -> None:
        self.assertEqual(self.run_main("add", "--kind", "bogus", "x")[0], 2)
        self.assertFalse(self.store.exists())

    def test_control_and_format_chars_stripped_whitespace_collapsed(self) -> None:
        self.run_main("add", "--kind", "fact", "a\nb\x00c", "‮d​e", "  f \t g  ")
        entries = memory.load_entries(self.home)
        self.assertEqual(entries[0]["text"], "a bc de f g")
        self.run_main("add", "--kind", "fact", "t\tu\nv")
        self.assertEqual(memory.load_entries(self.home)[1]["text"], "t u v")

    def test_empty_after_normalise_exits_2(self) -> None:
        code, _, err = self.run_main("add", "--kind", "fact", "\n​ ")
        self.assertEqual(code, 2)
        self.assertIn("empty", err)
        self.assertEqual(memory.load_entries(self.home), [])

    def test_length_limit(self) -> None:
        code, _, err = self.run_main("add", "--kind", "fact", "x" * 201)
        self.assertEqual(code, 2)
        self.assertIn("200", err)
        self.assertFalse(self.store.exists())
        self.assertEqual(self.add("y" * 200), 0)
        self.assertEqual(len(memory.load_entries(self.home)[0]["text"]), 200)

    def test_cap_drops_oldest(self) -> None:
        for i in range(61):
            self.assertEqual(self.add(f"entry {i}"), 0)
        entries = memory.load_entries(self.home)
        self.assertEqual(len(entries), 60)
        self.assertEqual(entries[0]["text"], "entry 1")
        self.assertEqual(entries[-1]["text"], "entry 60")


class ForgetTests(MemoryTestCase):
    def test_forget_n_removes_right_entry(self) -> None:
        for t in ("a", "b", "c"):
            self.add(t)
        self.assertEqual(self.run_main("forget", "2")[0], 0)
        self.assertEqual([e["text"] for e in memory.load_entries(self.home)], ["a", "c"])

    def test_forget_out_of_range(self) -> None:
        self.add("a")
        self.assertEqual(self.run_main("forget", "2")[0], 2)
        self.assertEqual(self.run_main("forget", "0")[0], 2)
        self.assertEqual(len(memory.load_entries(self.home)), 1)

    def test_forget_all(self) -> None:
        self.add("a")
        self.add("b")
        self.assertEqual(self.run_main("forget", "--all")[0], 0)
        self.assertEqual(memory.load_entries(self.home), [])


class DenyDirTests(MemoryTestCase):
    def test_all_commands_refused_inside_deny_dir(self) -> None:
        sub = self.work / "proj" / "deep"
        sub.mkdir(parents=True)
        os.chdir(sub)
        for argv in (["add", "--kind", "fact", "x"], ["list"], ["forget", "1"]):
            code, out, err = self.run_main(*argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(out, "")
            self.assertIn("personal-only", err)
        self.assertFalse(self.home.exists())

    def test_sibling_prefix_dir_allowed(self) -> None:
        evil = self.root / "work-evil"
        evil.mkdir()
        os.chdir(evil)
        self.assertEqual(self.add("ok"), 0)

    def test_symlink_into_deny_dir_refused(self) -> None:
        link = self.root / "link"
        link.symlink_to(self.work)
        os.chdir(link)
        self.assertEqual(self.run_main("list")[0], 2)

    def test_case_variant_deny_dir_refused(self) -> None:
        sub = self.root / "WorkDir" / "x"
        sub.mkdir(parents=True)
        if not (self.root / "workdir").exists():
            self.skipTest("case-sensitive filesystem")
        os.chdir(sub)
        code, out, err = self.run_main("list", deny=self.root / "workdir")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("personal-only", err)


class SafetyTests(MemoryTestCase):
    def test_symlinked_dir_refused(self) -> None:
        target = self.root / "target"
        target.mkdir()
        self.home.symlink_to(target)
        code, _, err = self.run_main("add", "--kind", "fact", "x")
        self.assertEqual(code, 1)
        self.assertIn("symlink", err)
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(self.run_main("list")[0], 1)

    def test_symlinked_store_file_refused(self) -> None:
        self.add("seed")
        target = self.root / "victim.txt"
        target.write_text("precious\n")
        self.store.unlink()
        self.store.symlink_to(target)
        self.assertEqual(self.run_main("add", "--kind", "fact", "x")[0], 1)
        self.assertEqual(self.run_main("list")[0], 1)
        self.assertEqual(target.read_text(), "precious\n")
        self.assertTrue(self.store.is_symlink())

    def test_symlinked_lock_file_refused(self) -> None:
        self.add("seed")
        target = self.root / "victim.txt"
        target.write_text("precious\n")
        (self.home / "memory.lock").unlink()
        (self.home / "memory.lock").symlink_to(target)
        self.assertEqual(self.add("x"), 1)
        self.assertEqual(target.read_text(), "precious\n")

    def test_modes(self) -> None:
        self.home.mkdir(mode=0o755)
        os.chmod(self.home, 0o755)
        self.add("x")
        self.assertEqual(stat.S_IMODE(os.stat(self.home).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.store).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.home / "memory.lock").st_mode), 0o600)
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.endswith(".tmp")], [])

    def plant(self, *raw_lines: bytes | str) -> None:
        self.home.mkdir(mode=0o700, exist_ok=True)
        blob = b"\n".join(x if isinstance(x, bytes) else x.encode() for x in raw_lines)
        self.store.write_bytes(blob + b"\n")

    def test_corrupt_lines_skipped_then_dropped_on_rewrite(self) -> None:
        good = {"ts": "2026-01-01", "kind": "win", "text": "good"}
        ok = {"ts": "2026-01-01", "kind": "win", "text": "x"}
        self.plant(
            "garbage",
            json.dumps(good),
            json.dumps({**ok, "ts": 5}),
            json.dumps({**ok, "kind": "nope"}),
            json.dumps({**ok, "text": "x" * 201}),
            json.dumps({**ok, "text": ""}),
            json.dumps({**ok, "text": 7}),
            json.dumps({**ok, "extra": 1}),
            json.dumps([good]),
            b'{"ts": "2026-01-01", "kind": "win", "text": "bad \xff\xfe bytes"}',
        )
        self.assertEqual(memory.load_entries(self.home), [good])
        self.assertEqual(self.add("new"), 0)
        rewritten = self.store.read_text().splitlines()
        self.assertEqual(len(rewritten), 2)
        self.assertEqual(json.loads(rewritten[0]), good)

    def test_planted_control_chars_skipped(self) -> None:
        good = {"ts": "2026-01-01", "kind": "win", "text": "good"}
        bad_ts = ["2026-01-01\n", "2026-01-01\x1b[31m", "2026-13-45", "2026-1-1", "d", "\u202e"]
        bad_text = [
            "\u202e", "\u0000", "a\nb", "\ud800", "a\tb", " lead", "two  spaces", "\ue000",
        ]
        lines = [json.dumps(good)]
        lines += [json.dumps({**good, "ts": t}) for t in bad_ts]
        lines += [json.dumps({**good, "text": t}) for t in bad_text]
        self.plant(*lines)
        self.assertEqual(memory.load_entries(self.home), [good])
        _, out, _ = self.run_main("list")
        self.assertEqual(out, "1. 2026-01-01 win: good\n")
        self.assertTrue(all(c.isprintable() or c == "\n" for c in out))

    def test_load_returns_newest_cap(self) -> None:
        self.plant(*(
            json.dumps({"ts": "2026-01-01", "kind": "win", "text": f"e{i}"}) for i in range(100)
        ))
        texts = [e["text"] for e in memory.load_entries(self.home)]
        self.assertEqual(texts, [f"e{i}" for i in range(40, 100)])

    def test_oversized_store_fails_closed(self) -> None:
        self.add("seed")
        with open(self.store, "ab") as fh:
            fh.write(b"z" * (memory.READ_CAP + 10) + b"\n")
        before = self.store.read_bytes()
        self.assertEqual(self.add("x"), 1)
        self.assertEqual(self.run_main("list")[0], 1)
        self.assertEqual(self.run_main("forget", "1")[0], 1)
        self.assertEqual(self.store.read_bytes(), before)

    def test_hardlinked_store_refused(self) -> None:
        self.add("seed")
        os.link(self.store, self.root / "alias")
        self.assertEqual(self.add("x"), 1)
        self.assertEqual(self.run_main("list")[0], 1)
        self.assertEqual(len((self.root / "alias").read_text().splitlines()), 1)

    def test_write_failure_leaves_no_temp_and_store_unchanged(self) -> None:
        self.add("seed")
        before = self.store.read_bytes()
        with mock.patch("os.fsync", side_effect=OSError("boom")):
            code, _, err = self.run_main("add", "--kind", "fact", "x")
        self.assertEqual(code, 1)
        self.assertIn("boom", err)
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.endswith(".tmp")], [])

    def test_missing_dir_loads_empty(self) -> None:
        self.assertEqual(memory.load_entries(self.home), [])
        self.assertFalse(self.home.exists())


class LockAndDirTests(MemoryTestCase):
    def test_lock_timeout_override_parsing(self) -> None:
        for bad in ("nan", "inf", "-1", "abc"):
            with mock.patch.dict(os.environ, {"ERIDIAN_TEST_LOCK_TIMEOUT": bad}):
                self.assertEqual(memory._lock_timeout(), memory.LOCK_TIMEOUT, bad)
        with mock.patch.dict(os.environ, {"ERIDIAN_TEST_LOCK_TIMEOUT": "0.3"}):
            self.assertEqual(memory._lock_timeout(), 0.3)

    def test_dir_vanishing_in_create_mode_exits_1(self) -> None:
        real_open = os.open

        def fake_open(path, flags, *args, **kwargs):
            if flags & os.O_DIRECTORY:
                raise FileNotFoundError(2, "gone", str(path))
            return real_open(path, flags, *args, **kwargs)

        with mock.patch("os.open", side_effect=fake_open):
            code, _, err = self.run_main("add", "--kind", "win", "x")
        self.assertEqual(code, 1)
        self.assertIn("vanished", err)
        self.assertNotIn("Traceback", err)


class SubprocessTests(MemoryTestCase):
    def sub(
        self, *argv: str, timeout: float = 60, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONPATH": SRC, **(extra_env or {})}
        return subprocess.run(
            [sys.executable, "-P", "-m", "eridian.memory", "--deny-under", str(self.work), *argv],
            cwd=self.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_entry_point_round_trip(self) -> None:
        res = self.sub("add", "--kind", "mood", "happy")
        self.assertEqual((res.returncode, res.stdout), (0, "saved (1 entries)\n"))
        res = self.sub("list")
        self.assertRegex(res.stdout, r"^1\. \d{4}-\d\d-\d\d mood: happy\n$")

    def test_concurrent_adds_lose_nothing(self) -> None:
        env = {**os.environ, "PYTHONPATH": SRC}
        procs = []
        for p in range(3):
            script = (
                "import sys\nfrom eridian import memory\n"
                f"for i in range(15):\n"
                f"    rc = memory.main(['--deny-under', {str(self.work)!r}, 'add', '--kind',"
                f" 'fact', 'p{p}-' + str(i)])\n"
                "    if rc: sys.exit(rc)\n"
            )
            procs.append(
                subprocess.Popen(
                    [sys.executable, "-P", "-c", script],
                    cwd=self.cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            )
        for proc in procs:
            _, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)
        texts = {e["text"] for e in memory.load_entries(self.home)}
        self.assertEqual(len(texts), 45)

    def test_fifo_store_refused_promptly(self) -> None:
        self.home.mkdir(mode=0o700)
        os.mkfifo(self.store)
        self.assertEqual(self.sub("list", timeout=10).returncode, 1)
        self.assertEqual(self.sub("add", "--kind", "fact", "x", timeout=10).returncode, 1)

    def test_lock_timeout_is_deterministic(self) -> None:
        self.add("seed")
        before = self.store.read_bytes()
        fd = os.open(self.home / "memory.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            res = self.sub(
                "add", "--kind", "fact", "x",
                timeout=30, extra_env={"ERIDIAN_TEST_LOCK_TIMEOUT": "0.3"},
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn("busy", res.stderr)
            self.assertEqual(self.store.read_bytes(), before)
        finally:
            os.close(fd)
        self.assertEqual(self.add("after"), 0)

    def test_no_stdin_read(self) -> None:
        # an open pipe that is never written or closed: a stdin read would hang
        env = {**os.environ, "PYTHONPATH": SRC}
        proc = subprocess.Popen(
            [sys.executable, "-P", "-m", "eridian.memory", "--deny-under", str(self.work),
             "add", "--kind", "fact", "x"],
            cwd=self.cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(proc.wait(timeout=10), 0, proc.stderr.read())
        finally:
            proc.kill()
            proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()


if __name__ == "__main__":
    unittest.main()

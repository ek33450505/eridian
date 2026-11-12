import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from eridian import statusline

SRC = Path(__file__).resolve().parent.parent / "src"
HEAD = "# branch.oid abc\n# branch.head main\n"
AB = "# branch.upstream origin/main\n# branch.ab +{a} -{b}\n"
DIRTY = "1 .M N... 100644 100644 100644 aaa bbb f.txt\n"
CONFLICT = "u UU N... 100644 100644 100644 100644 a b c f.txt\n"


class MoodFromPorcelainTest(unittest.TestCase):
    def test_each_mood(self) -> None:
        mood = statusline.mood_from_porcelain
        self.assertEqual(mood(HEAD + AB.format(a=0, b=0)), statusline.GOOD)
        self.assertEqual(mood(HEAD + AB.format(a=0, b=0) + DIRTY), statusline.WORK)
        self.assertEqual(mood(HEAD + AB.format(a=0, b=0) + DIRTY.replace("1 ", "2 ")),
                         statusline.WORK)
        self.assertEqual(mood(HEAD + AB.format(a=2, b=0)), statusline.PUSH)
        self.assertEqual(mood(HEAD + AB.format(a=0, b=3)), statusline.PULL)
        self.assertEqual(mood(HEAD + AB.format(a=1, b=1)), statusline.WORRY)
        self.assertEqual(mood(HEAD + CONFLICT), statusline.WORRY)
        self.assertEqual(mood("# branch.head (detached)\n"), statusline.WORRY)

    def test_no_upstream_is_clean(self) -> None:
        self.assertEqual(statusline.mood_from_porcelain(HEAD), statusline.GOOD)

    def test_precedence(self) -> None:
        mood = statusline.mood_from_porcelain
        self.assertEqual(mood(HEAD + DIRTY + CONFLICT), statusline.WORRY)
        self.assertEqual(mood(HEAD + CONFLICT + DIRTY), statusline.WORRY)
        self.assertEqual(mood(HEAD + AB.format(a=1, b=0) + DIRTY), statusline.WORK)
        self.assertEqual(mood(HEAD + AB.format(a=0, b=1) + DIRTY), statusline.WORK)
        self.assertEqual(mood("# branch.head (detached)\n" + DIRTY), statusline.WORRY)
        self.assertEqual(mood(HEAD + AB.format(a=1, b=1) + DIRTY), statusline.WORRY)

    def test_garbage(self) -> None:
        # No `# branch.head` header means it is not git status output: None (print nothing).
        self.assertIsNone(statusline.mood_from_porcelain("garbage\nnot git\n"))
        self.assertIsNone(statusline.mood_from_porcelain(""))
        # A malformed branch.ab line is ignored; the rest still counts.
        self.assertEqual(statusline.mood_from_porcelain(HEAD + "# branch.ab x y\n"),
                         statusline.GOOD)


def run_main(payload: object, raw: str | None = None) -> tuple[int, str]:
    out = io.StringIO()
    text = raw if raw is not None else json.dumps(payload)
    rc = statusline.main(stdin=io.StringIO(text), stdout=out)
    return rc, out.getvalue()


GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


@unittest.skipIf(shutil.which("git") is None, "git not installed")
class GitFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="sl")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        # main() scrubs only GIT_*: point HOME/XDG at the temp root so a developer's global
        # git config cannot change results.
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.root),
                                               "XDG_CONFIG_HOME": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        timeout = mock.patch.object(statusline, "GIT_TIMEOUT", 5.0)
        timeout.start()
        self.addCleanup(timeout.stop)
        self.remote = self.root / "remote.git"
        self.git(self.root, "init", "--bare", "-q", "-b", "main", str(self.remote))
        self.repo = self.clone("work")
        (self.repo / "f.txt").write_text("one\n")
        self.git(self.repo, "add", "f.txt")
        self.git(self.repo, "commit", "-q", "-m", "init")
        self.git(self.repo, "push", "-q", "-u", "origin", "main")

    def git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_ENV, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                   HOME=str(self.root), XDG_CONFIG_HOME=str(self.root))
        return subprocess.run(["git", "-c", "init.defaultBranch=main", "-c",
                               "commit.gpgsign=false", *args], cwd=cwd, env=env,
                              check=check, capture_output=True, timeout=30)

    def clone(self, name: str) -> Path:
        self.git(self.root, "clone", "-q", str(self.remote), name)
        return self.root / name

    def mood(self, cwd: Path | None = None) -> str:
        rc, out = run_main({"workspace": {"current_dir": str(cwd or self.repo)}})
        self.assertEqual(rc, 0)
        return out

    def test_clean_synced(self) -> None:
        self.assertEqual(self.mood(), statusline.GOOD + "\n")

    def test_modified_tracked(self) -> None:
        (self.repo / "f.txt").write_text("two\n")
        self.assertEqual(self.mood(), statusline.WORK + "\n")

    def test_untracked_only_is_clean(self) -> None:
        # Documented: -uno ignores untracked files.
        (self.repo / "new.txt").write_text("x\n")
        self.assertEqual(self.mood(), statusline.GOOD + "\n")

    def test_local_commit_is_ahead(self) -> None:
        (self.repo / "f.txt").write_text("two\n")
        self.git(self.repo, "commit", "-q", "-am", "local")
        self.assertEqual(self.mood(), statusline.PUSH + "\n")

    def test_remote_ahead(self) -> None:
        other = self.clone("other")
        (other / "f.txt").write_text("remote\n")
        self.git(other, "commit", "-q", "-am", "remote")
        self.git(other, "push", "-q")
        self.git(self.repo, "fetch", "-q")
        self.assertEqual(self.mood(), statusline.PULL + "\n")

    def test_merge_conflict(self) -> None:
        self.git(self.repo, "checkout", "-q", "-b", "side")
        (self.repo / "f.txt").write_text("side\n")
        self.git(self.repo, "commit", "-q", "-am", "side")
        self.git(self.repo, "checkout", "-q", "main")
        (self.repo / "f.txt").write_text("main\n")
        self.git(self.repo, "commit", "-q", "-am", "main")
        self.git(self.repo, "merge", "side", check=False)
        self.assertEqual(self.mood(), statusline.WORRY + "\n")

    def test_detached(self) -> None:
        self.git(self.repo, "checkout", "-q", "--detach")
        self.assertEqual(self.mood(), statusline.WORRY + "\n")

    def test_non_repo(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        self.assertEqual(self.mood(plain), "")

    def test_cwd_fallback_key(self) -> None:
        self.assertEqual(run_main({"cwd": str(self.repo)}),
                         (0, statusline.GOOD + "\n"))

    def test_bad_inputs(self) -> None:
        for raw in ("not json", "[]", "null", '"x"', "", "{}",
                    json.dumps({"cwd": "relative/dir"}),
                    json.dumps({"cwd": str(self.root / "missing")}),
                    json.dumps({"cwd": 5}),
                    json.dumps({"workspace": {"current_dir": 5}})):
            with self.subTest(raw=raw):
                self.assertEqual(run_main(None, raw), (0, ""))

    def test_env_scrub(self) -> None:
        other = self.root / "elsewhere"
        other.mkdir()
        self.git(other, "init", "-q")
        (other / "g.txt").write_text("g\n")
        self.git(other, "add", "g.txt")
        self.git(other, "commit", "-q", "-m", "g")
        (other / "g.txt").write_text("changed\n")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"),
                                          "GIT_WORK_TREE": str(other)}):
            self.assertEqual(self.mood(), statusline.GOOD + "\n")

    def fake_timeout_popen(self) -> mock.MagicMock:
        proc = mock.MagicMock(pid=4242)
        proc.communicate.side_effect = subprocess.TimeoutExpired(["git"], 0.25)
        return proc

    def test_timeout(self) -> None:
        with mock.patch("subprocess.Popen", return_value=self.fake_timeout_popen()), \
                mock.patch("os.killpg"):
            self.assertEqual(run_main({"cwd": str(self.repo)}), (0, ""))

    def test_timeout_kills_process_group(self) -> None:
        with mock.patch("subprocess.Popen", return_value=self.fake_timeout_popen()), \
                mock.patch("os.killpg") as killpg:
            self.assertIsNone(statusline.git_status(str(self.repo)))
        killpg.assert_called_once_with(4242, signal.SIGKILL)

    def test_timeout_killpg_errors_ignored(self) -> None:
        with mock.patch("subprocess.Popen", return_value=self.fake_timeout_popen()), \
                mock.patch("os.killpg", side_effect=ProcessLookupError):
            self.assertIsNone(statusline.git_status(str(self.repo)))

    def test_git_command_shape(self) -> None:
        # Untracked files never change the mood, so assert the flags directly.
        with mock.patch("subprocess.Popen", side_effect=subprocess.Popen) as popen:
            self.assertEqual(statusline.git_status(str(self.repo)).splitlines()[0][:8],
                             "# branch")
        cmds = [c.args[0] for c in popen.call_args_list]
        self.assertEqual(len(cmds), 2)
        self.assertIn("--get-regexp", cmds[0])
        for flag in ("-uno", "--no-optional-locks", "core.fsmonitor=false"):
            self.assertIn(flag, cmds[1])
        for call in popen.call_args_list:
            self.assertTrue(call.kwargs["start_new_session"])

    def add_filter(self, key: str, config_path: Path | None = None) -> Path:
        marker = self.root / "marker"
        (self.repo / ".gitattributes").write_text("* filter=evil\n")
        self.git(self.repo, "add", ".gitattributes")
        self.git(self.repo, "commit", "-q", "-m", "attrs")
        section = f'[filter "evil"]\n\t{key} = touch {marker}; cat\n'
        target = config_path or self.repo / ".git" / "config"
        with open(target, "a") as fh:
            fh.write(section)
        if config_path:
            with open(self.repo / ".git" / "config", "a") as fh:
                fh.write(f"[include]\n\tpath = {config_path}\n")
        (self.repo / "f.txt").write_text("dirty\n")
        return marker

    def test_filter_clean_not_executed(self) -> None:
        marker = self.add_filter("clean")
        self.assertEqual(self.mood(), "")
        self.assertFalse(marker.exists())

    def test_filter_process_not_executed(self) -> None:
        marker = self.add_filter("process")
        self.assertEqual(self.mood(), "")
        self.assertFalse(marker.exists())

    def test_filter_via_include_not_executed(self) -> None:
        marker = self.add_filter("clean", self.root / "extra.cfg")
        self.assertEqual(self.mood(), "")
        self.assertFalse(marker.exists())

    def test_filter_via_worktree_config_not_executed(self) -> None:
        marker = self.root / "marker"
        (self.repo / ".gitattributes").write_text("* filter=evil\n")
        self.git(self.repo, "add", ".gitattributes")
        self.git(self.repo, "commit", "-q", "-m", "attrs")
        gitdir = self.repo / ".git"
        with open(gitdir / "config", "a") as fh:
            fh.write("[extensions]\n\tworktreeConfig = true\n")
        (gitdir / "config.worktree").write_text(
            f'[filter "evil"]\n\tclean = touch {marker}; cat\n')
        (self.repo / "f.txt").write_text("dirty\n")
        self.assertEqual(self.mood(), "")
        self.assertFalse(marker.exists())

    def test_unborn_repo(self) -> None:
        fresh = self.root / "fresh"
        fresh.mkdir()
        self.git(fresh, "init", "-q")
        self.assertEqual(self.mood(fresh), statusline.GOOD + "\n")

    def test_no_upstream(self) -> None:
        fresh = self.root / "noup"
        fresh.mkdir()
        self.git(fresh, "init", "-q")
        (fresh / "a").write_text("a\n")
        self.git(fresh, "add", "a")
        self.git(fresh, "commit", "-q", "-m", "a")
        self.assertEqual(self.mood(fresh), statusline.GOOD + "\n")

    def test_staged_rename(self) -> None:
        self.git(self.repo, "mv", "f.txt", "g.txt")
        self.assertEqual(self.mood(), statusline.WORK + "\n")

    def test_bare_repo(self) -> None:
        self.assertEqual(self.mood(self.remote), "")

    def test_cwd_inside_git_dir(self) -> None:
        self.assertEqual(self.mood(self.repo / ".git"), "")

    def test_closed_stdout_pipe(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(PYTHONPATH=str(SRC), HOME=str(self.root), XDG_CONFIG_HOME=str(self.root))
        proc = subprocess.Popen([sys.executable, "-P", "-m", "eridian.statusline"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env)
        proc.stdout.close()
        _, err = proc.communicate(json.dumps({"cwd": str(self.repo)}).encode(), timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(err, b"")

    def test_reap_is_bounded_with_setsid_grandchild(self) -> None:
        pidfile = self.root / "gc.pid"
        fake = self.root / "fakegit"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys, time\n"
            "if os.fork() == 0:\n"
            "    os.setsid()\n"
            f"    open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            "    time.sleep(5)\n"
            "    os._exit(0)\n"
            "time.sleep(5)\n")
        fake.chmod(0o700)
        self.addCleanup(self.kill_grandchild, pidfile)
        start = time.monotonic()
        with mock.patch.object(statusline, "GIT_TIMEOUT", 0.2):
            self.assertIsNone(statusline.run_git(str(fake), ["x"], dict(os.environ)))
        self.assertLess(time.monotonic() - start, 2.0)

    @staticmethod
    def kill_grandchild(pidfile: Path) -> None:
        try:
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass

    def test_oversize_payload(self) -> None:
        payload = {"cwd": str(self.repo), "pad": "x" * (70 * 1024)}
        self.assertEqual(run_main(payload), (0, ""))

    def test_module_entry_point(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(PYTHONPATH=str(SRC), HOME=str(self.root), XDG_CONFIG_HOME=str(self.root))
        proc = subprocess.run([sys.executable, "-P", "-m", "eridian.statusline"],
                              input=json.dumps({"cwd": str(self.repo)}), env=env,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, statusline.GOOD + "\n")

    def test_fast(self) -> None:
        start = time.monotonic()
        self.assertEqual(self.mood(), statusline.GOOD + "\n")
        self.assertLess(time.monotonic() - start, 1.0)


if __name__ == "__main__":
    unittest.main()

"""Tests for `eridian migrate` (pre-rename ~/.undertone -> eridian home).

Every test builds source and target under its own temp dir; the real
~/.undertone and ~/.eridian are never touched.
"""

import contextlib
import io
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eridian import cli, paths


class MigrateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # realpath: macOS temp dirs sit behind the /var -> /private/var symlink.
        self.root = Path(os.path.realpath(self._tmp.name))
        self.source = self.root / "old"
        self.target = self.root / "new"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_source(self) -> None:
        self.source.mkdir(mode=0o700)
        (self.source / "chat_session").write_text("abc")
        (self.source / "sessions").mkdir()
        (self.source / "sessions" / "s1.json").write_text("{}")
        secret = self.source / "safe_phrase.json"
        secret.write_text("hash")
        os.chmod(secret, 0o600)

    def _migrate(self, dry_run: bool = False) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli._migrate(self.source, self.target, dry_run=dry_run)
        return rc, out.getvalue(), err.getvalue()

    def _assert_untouched(self) -> None:
        self.assertTrue((self.source / "chat_session").exists())


class MigrateTest(MigrateTestCase):
    def test_happy_path_moves_everything(self) -> None:
        self._make_source()
        rc, out, _ = self._migrate()
        self.assertEqual(rc, 0)
        self.assertFalse(self.source.exists())
        self.assertEqual((self.target / "chat_session").read_text(), "abc")
        self.assertEqual((self.target / "sessions" / "s1.json").read_text(), "{}")
        secret = self.target / "safe_phrase.json"
        self.assertEqual(secret.read_text(), "hash")
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o700)
        self.assertIn(f"moved {self.source} -> {self.target}", out)

    def test_empty_existing_target_is_replaced(self) -> None:
        self._make_source()
        self.target.mkdir()
        rc, _, _ = self._migrate()
        self.assertEqual(rc, 0)
        self.assertEqual((self.target / "chat_session").read_text(), "abc")

    def test_non_empty_target_refused(self) -> None:
        self._make_source()
        self.target.mkdir()
        (self.target / "x").write_text("keep")
        rc, _, err = self._migrate()
        self.assertEqual(rc, 1)
        self.assertIn("not empty", err)
        self._assert_untouched()
        self.assertEqual((self.target / "x").read_text(), "keep")

    def test_source_missing_is_nothing_to_do(self) -> None:
        rc, out, _ = self._migrate()
        self.assertEqual(rc, 0)
        self.assertIn("nothing to migrate", out)
        self.assertFalse(self.target.exists())

    def test_symlinked_source_refused(self) -> None:
        real = self.root / "real"
        real.mkdir()
        (real / "chat_session").write_text("abc")
        self.source.symlink_to(real)
        rc, _, err = self._migrate()
        self.assertEqual(rc, 1)
        self.assertIn("symlink", err)
        self.assertFalse(self.target.exists())

    def test_symlinked_target_refused(self) -> None:
        self._make_source()
        real = self.root / "real"
        real.mkdir()
        self.target.symlink_to(real)
        rc, _, err = self._migrate()
        self.assertEqual(rc, 1)
        self.assertIn("symlink", err)
        self._assert_untouched()

    def test_symlinked_parent_refused(self) -> None:
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real)
        self.source = link / "old"
        self.source.mkdir()
        (self.source / "chat_session").write_text("abc")
        rc, _, err = self._migrate()
        self.assertEqual(rc, 1)
        self.assertIn("symlink", err)

    def test_source_not_a_directory_refused(self) -> None:
        self.source.write_text("file")
        rc, _, err = self._migrate()
        self.assertEqual(rc, 1)
        self.assertIn("not a directory", err)

    def test_foreign_owner_refused(self) -> None:
        self._make_source()
        with mock.patch.object(cli.os, "getuid", return_value=os.getuid() + 1):
            rc, _, err = self._migrate()
        self.assertEqual(rc, 1)
        self.assertIn("owned", err)
        self._assert_untouched()

    def test_live_attach_in_source_refused(self) -> None:
        self._make_source()
        (self.source / "attach.pid").write_text("4242\n")
        with mock.patch.object(cli, "_pid_is_attach", return_value=True) as m:
            rc, _, err = self._migrate()
        m.assert_called_with(4242)
        self.assertEqual(rc, 1)
        self.assertIn("attach is running", err)
        self._assert_untouched()

    def test_live_attach_in_target_refused(self) -> None:
        self._make_source()
        self.target.mkdir()
        (self.target / "attach.pid").write_text("4343\n")
        with mock.patch.object(cli, "_pid_is_attach", return_value=True):
            rc, _, err = self._migrate()
        self.assertEqual(rc, 1)
        self.assertIn("attach is running", err)
        self._assert_untouched()

    def test_stale_pidfile_does_not_block(self) -> None:
        self._make_source()
        (self.source / "attach.pid").write_text("4242\n")
        with mock.patch.object(cli, "_pid_is_attach", return_value=False), \
                mock.patch.object(cli, "_ps_command", return_value=""):
            rc, _, _ = self._migrate()
        self.assertEqual(rc, 0)

    def test_voice_socket_refused(self) -> None:
        self._make_source()
        # AF_UNIX paths are short; bind from inside the dir via a relative name.
        s = socket.socket(socket.AF_UNIX)
        cwd = os.getcwd()
        try:
            os.chdir(self.source)
            s.bind("voice.sock")
        finally:
            os.chdir(cwd)
        try:
            rc, _, err = self._migrate()
        finally:
            s.close()
        self.assertEqual(rc, 1)
        self.assertIn("voice.sock", err)
        self.assertIn(f"remove {self.source}/voice.sock and retry", err)
        self._assert_untouched()

    def test_different_filesystem_refused(self) -> None:
        self._make_source()
        real = os.lstat
        source = str(self.source)

        def fake(p, *a, **k):
            r = real(p, *a, **k)
            if str(p) == source:
                fields = list(r)
                fields[2] += 1  # st_dev
                return os.stat_result(fields)
            return r

        with mock.patch.object(cli.os, "lstat", side_effect=fake):
            rc, _, err = self._migrate()
        self.assertEqual(rc, 1)
        self.assertIn("filesystem", err)
        self._assert_untouched()

    def test_dry_run_changes_nothing(self) -> None:
        self._make_source()
        self.target.mkdir()
        rc, out, _ = self._migrate(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn("would move", out)
        self._assert_untouched()
        self.assertTrue(self.target.is_dir())
        self.assertEqual(list(self.target.iterdir()), [])

    def test_dry_run_still_refuses(self) -> None:
        self._make_source()
        self.target.mkdir()
        (self.target / "x").write_text("keep")
        rc, _, _ = self._migrate(dry_run=True)
        self.assertEqual(rc, 1)


def _as_uid(real, path_str: str, uid: int):
    """An os.lstat stand-in that reports st_uid=uid for path_str."""

    def fake(p, *a, **k):
        r = real(p, *a, **k)
        if str(p) == path_str:
            fields = list(r)
            fields[4] = uid  # st_uid
            return os.stat_result(fields)
        return r

    return fake


class MigrateHardeningTest(MigrateTestCase):
    def test_relative_target_refused(self) -> None:
        self._make_source()
        self.target = Path("rel/x")
        rc, _, err = self._migrate()
        self.assertEqual(rc, 1)
        self.assertIn("absolute", err)
        self._assert_untouched()

    def test_relative_home_env_refused(self) -> None:
        home = self.root / "fakehome"
        (home / ".undertone").mkdir(parents=True, mode=0o700)
        (home / ".undertone" / "chat_session").write_text("abc")
        with mock.patch.dict(os.environ, {"ERIDIAN_HOME": "lnk/x"}), \
                mock.patch.object(Path, "home", return_value=home), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["migrate"])
        self.assertEqual(rc, 1)
        self.assertTrue((home / ".undertone" / "chat_session").exists())

    def test_symlink_in_relative_first_component(self) -> None:
        real = self.root / "real"
        real.mkdir()
        (self.root / "lnk").symlink_to(real)
        cwd = os.getcwd()
        try:
            os.chdir(self.root)
            self.assertEqual(cli._symlink_in(Path("lnk/x")), Path("lnk"))
            self.assertIsNone(cli._symlink_in(Path("real/x")))
        finally:
            os.chdir(cwd)

    def test_symlink_in_anchored_first_component(self) -> None:
        real = self.root / "real"
        real.mkdir()
        link = self.root / "lnk"
        link.symlink_to(real)
        self.assertEqual(cli._symlink_in(link / "x"), link)

    def test_regular_file_component_is_refusal_not_traceback(self) -> None:
        self._make_source()
        blocker = self.root / "blocker"
        blocker.write_text("x")
        self.target = blocker / "sub" / "new"
        rc, _, err = self._migrate()
        self.assertEqual(rc, 1)
        self.assertIn("migrate refused", err)
        self._assert_untouched()

    def test_rename_oserror_refused_source_intact(self) -> None:
        self._make_source()
        with mock.patch.object(cli.os, "rename", side_effect=OSError("boom")):
            rc, _, err = self._migrate()
        self.assertEqual(rc, 1)
        self.assertIn("boom", err)
        self._assert_untouched()

    def test_fchmod_failure_prints_fix_message(self) -> None:
        self._make_source()
        with mock.patch.object(cli.os, "fchmod", side_effect=OSError("nope")):
            rc, _, err = self._migrate()
        self.assertEqual(rc, 1)
        self.assertIn("but could not set mode 0700", err)
        self.assertIn(f"chmod 700 {self.target}", err)
        self.assertTrue((self.target / "chat_session").exists())

    def test_writable_parent_refused(self) -> None:
        self._make_source()
        for mode in (0o770, 0o707):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.root, mode)
                try:
                    rc, _, err = self._migrate()
                finally:
                    os.chmod(self.root, 0o700)
                self.assertEqual(rc, 1)
                self.assertIn("writable", err)
                self._assert_untouched()

    def test_parent_other_uid_refused(self) -> None:
        self._make_source()
        fake = _as_uid(os.lstat, str(self.root), os.getuid() + 1)
        with mock.patch.object(cli.os, "lstat", side_effect=fake):
            rc, _, err = self._migrate()
        self.assertEqual(rc, 1)
        self.assertIn(f"{self.root} is not owned", err)
        self._assert_untouched()

    def test_existing_target_other_uid_refused(self) -> None:
        self._make_source()
        self.target.mkdir()
        fake = _as_uid(os.lstat, str(self.target), os.getuid() + 1)
        with mock.patch.object(cli.os, "lstat", side_effect=fake):
            rc, _, err = self._migrate()
        self.assertEqual(rc, 1)
        self.assertIn(f"{self.target} is not owned", err)
        self._assert_untouched()

    def test_legacy_undertone_attach_refused(self) -> None:
        self._make_source()
        (self.source / "attach.pid").write_text("4242\n")
        cmd = "/usr/bin/python -m undertone attach --no-beep\n"
        with mock.patch.object(cli, "_ps_command", return_value=cmd):
            rc, _, err = self._migrate()
        self.assertEqual(rc, 1)
        self.assertIn("attach is running", err)
        self._assert_untouched()

    def test_unrelated_process_does_not_block(self) -> None:
        self._make_source()
        (self.source / "attach.pid").write_text("4242\n")
        cmd = "tail -f ~/.undertone/attach.log\n"
        with mock.patch.object(cli, "_ps_command", return_value=cmd):
            rc, _, _ = self._migrate()
        self.assertEqual(rc, 0)


class MigrateCommandTest(MigrateTestCase):
    def test_eridian_home_target_honored(self) -> None:
        home = self.root / "fakehome"
        home.mkdir()
        (home / ".undertone").mkdir(mode=0o700)
        (home / ".undertone" / "chat_session").write_text("abc")
        target = home / "custom"
        env = {"ERIDIAN_HOME": str(target)}
        with mock.patch.dict(os.environ, env), mock.patch.object(
            Path, "home", return_value=home
        ), contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["migrate"])
        self.assertEqual(rc, 0)
        self.assertEqual((target / "chat_session").read_text(), "abc")
        self.assertFalse((home / ".undertone").exists())

    def test_default_target_is_dot_eridian(self) -> None:
        home = self.root / "fakehome"
        home.mkdir()
        (home / ".undertone").mkdir(mode=0o700)
        (home / ".undertone" / "chat_session").write_text("abc")
        with mock.patch.dict(os.environ), mock.patch.object(
            Path, "home", return_value=home
        ), contextlib.redirect_stdout(io.StringIO()):
            os.environ.pop("ERIDIAN_HOME", None)
            rc = cli.main(["migrate", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertTrue((home / ".undertone" / "chat_session").exists())
        self.assertFalse((home / ".eridian").exists())


class PathsHomeTest(MigrateTestCase):
    def test_home_path_does_not_create(self) -> None:
        with mock.patch.dict(os.environ, {"ERIDIAN_HOME": str(self.target)}):
            self.assertEqual(paths.home_path(), self.target)
        self.assertFalse(self.target.exists())

    def test_home_dir_still_creates_and_chmods(self) -> None:
        with mock.patch.dict(os.environ, {"ERIDIAN_HOME": str(self.target)}):
            self.assertEqual(paths.home_dir(), self.target)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()

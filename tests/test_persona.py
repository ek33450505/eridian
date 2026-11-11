import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eridian import persona


class TypedHookTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="ut")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.under = root / "personal"
        self.sub = self.under / "proj"
        self.evil = root / "personal-evil"
        self.outside = root / "elsewhere"
        for d in (self.sub, self.evil, self.outside):
            d.mkdir(parents=True)

    def _run(self, raw: str, under: object = None) -> tuple[int, str]:
        out = io.StringIO()
        argv = ["sessionstart", "--under", str(self.under if under is None else under)]
        rc = persona.main(argv, stdin=io.StringIO(raw), stdout=out)
        return rc, out.getvalue()

    def _cwd(self, cwd: object) -> tuple[int, str]:
        return self._run(json.dumps({"cwd": cwd}))

    def _assert_emits(self, cwd: object) -> None:
        rc, out = self._cwd(str(cwd))
        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(out),
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": persona.TYPED_ROCKY_BLOCK,
                }
            },
        )

    def _assert_silent(self, rc_out: tuple[int, str]) -> None:
        self.assertEqual(rc_out, (0, ""))

    def test_emits_when_cwd_is_under(self) -> None:
        self._assert_emits(self.under)

    def test_emits_for_subdir(self) -> None:
        self._assert_emits(self.sub)

    def test_sibling_prefix_dir_is_silent(self) -> None:
        self._assert_silent(self._cwd(str(self.evil)))

    def test_outside_dir_is_silent(self) -> None:
        self._assert_silent(self._cwd(str(self.outside)))

    def test_bad_cwd_values_are_silent(self) -> None:
        for cwd in (None, 5, ["x"], "", "relative/dir"):
            self._assert_silent(self._cwd(cwd))
        self._assert_silent(self._run("{}"))

    def test_bad_payloads_are_silent(self) -> None:
        for raw in ("", "not json", "[]", '"str"', "null"):
            self._assert_silent(self._run(raw))

    def test_relative_under_is_silent(self) -> None:
        self._assert_silent(self._run(json.dumps({"cwd": str(self.sub)}), under="personal"))

    def test_symlink_out_of_under_is_silent(self) -> None:
        link = self.under / "link"
        link.symlink_to(self.outside)
        self._assert_silent(self._cwd(str(link)))

    def test_block_keeps_plain_rules(self) -> None:
        for needle in (
            "code, file paths, commands",
            "never Rocky speech",
            "commit messages",
            "prompts for subagents",
            "Accuracy, safety rules",
            "friend",
        ):
            self.assertIn(needle, persona.TYPED_ROCKY_BLOCK)

    def test_deep_nested_json_is_silent(self) -> None:
        self._assert_silent(self._run("[" * 100000))

    def test_missing_stdin_returns_zero(self) -> None:
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", None):
            rc = persona.main(["sessionstart", "--under", str(self.under)], stdout=out)
        self.assertEqual((rc, out.getvalue()), (0, ""))

    def test_broken_stdout_returns_zero(self) -> None:
        class Broken(io.StringIO):
            def write(self, s: str) -> int:
                raise BrokenPipeError

        rc = persona.main(
            ["sessionstart", "--under", str(self.under)],
            stdin=io.StringIO(json.dumps({"cwd": str(self.sub)})),
            stdout=Broken(),
        )
        self.assertEqual(rc, 0)

    def test_nonexistent_cwd_under_emits(self) -> None:
        self._assert_emits(self.under / "not" / "yet")

    def test_dotdot_escape_is_silent(self) -> None:
        self._assert_silent(self._cwd(f"{self.sub}/../../elsewhere"))

    def test_block_has_no_opener_and_approval_rules(self) -> None:
        block = persona.TYPED_ROCKY_BLOCK
        for needle in (
            "no Rocky-speak opener or closer at all",
            "approve or confirm an action",
            "permission prompts",
            "do not greet again after a resume",
            "These plain-English rules override everything above.",
            "never attaches to a concerns, security or caveat",
        ):
            self.assertIn(needle, block)

    def test_block_has_no_personal_details(self) -> None:
        self.assertIsNone(re.search(r"\bEd\b", persona.TYPED_ROCKY_BLOCK))
        self.assertNotIn("/Users/", persona.TYPED_ROCKY_BLOCK)

    def test_broken_flush_returns_zero(self) -> None:
        class BrokenFlush(io.StringIO):
            def flush(self) -> None:
                raise BrokenPipeError

        rc = persona.main(
            ["sessionstart", "--under", str(self.under)],
            stdin=io.StringIO(json.dumps({"cwd": str(self.sub)})),
            stdout=BrokenFlush(),
        )
        self.assertEqual(rc, 0)

    def test_closed_pipe_stdout_exits_zero(self) -> None:
        repo = Path(__file__).resolve().parent.parent
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        try:
            proc = subprocess.run(
                [sys.executable, "-P", "-m", "eridian.persona", "sessionstart",
                 "--under", str(self.under)],
                input=json.dumps({"cwd": str(self.sub)}),
                stdout=write_fd,
                stderr=subprocess.PIPE,
                text=True,
                cwd=repo,
                env=os.environ.copy(),
                timeout=30,
            )
        finally:
            os.close(write_fd)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_module_entry_point(self) -> None:
        repo = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [sys.executable, "-P", "-m", "eridian.persona", "sessionstart", "--under", str(self.under)],
            input=json.dumps({"cwd": str(self.sub)}),
            capture_output=True,
            text=True,
            cwd=repo,
            env=os.environ.copy(),
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = json.loads(proc.stdout)
        self.assertEqual(got["hookSpecificOutput"]["hookEventName"], "SessionStart")


if __name__ == "__main__":
    unittest.main()

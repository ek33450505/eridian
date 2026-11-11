"""Scrub ERIDIAN_* from os.environ so the suite ignores the developer's shell.

This file does the scrub at import time, in two modes. `unittest discover -s tests`
imports modules as top-level names and never runs tests/__init__.py; discovery
imports test modules in sorted order and this name sorts first. `python -m unittest
tests.test_x` imports `tests` as a package, so tests/__init__.py imports this module
to apply the same scrub. Under discover the module can still load a second time as
`tests.test_00_hermetic_env` (a test doing `from tests import ...` imports the
package), so the temp home is recorded in ERIDIAN_TEST_HERMETIC_HOME, which the
scrub keeps, and a second load reuses it: one temp home, one atexit handler.
Tests that need a specific variable set it themselves (mock.patch.dict).
ERIDIAN_TEST_* opt-ins are kept. Running a file
directly (`python tests/test_x.py`) skips this scrub.

After the scrub, ERIDIAN_HOME is pointed at a throwaway temp dir so a test that
forgets to set its own never creates or touches the real ~/.eridian.
"""
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

for _name in [k for k in os.environ if k.startswith("ERIDIAN_") and not k.startswith("ERIDIAN_TEST_")]:
    del os.environ[_name]

_PREFIX = "eridian-test-home-"


def _is_our_temp_home(path: str) -> bool:
    """True only for a dir this module could have created: <tmpdir>/eridian-test-home-*.

    The env var survives the scrub, so a developer's shell could plant any path in it.
    """
    if not path or not os.path.isdir(path):
        return False
    real = os.path.realpath(path)
    return (os.path.dirname(real) == os.path.realpath(tempfile.gettempdir())
            and os.path.basename(real).startswith(_PREFIX))


_TEST_HOME = os.environ.get("ERIDIAN_TEST_HERMETIC_HOME", "")
if not _is_our_temp_home(_TEST_HOME):
    _TEST_HOME = os.path.realpath(tempfile.mkdtemp(prefix=_PREFIX))
    atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)
    os.environ["ERIDIAN_TEST_HERMETIC_HOME"] = _TEST_HOME
_TEST_HOME = os.path.realpath(_TEST_HOME)
os.environ["ERIDIAN_HOME"] = _TEST_HOME


class HermeticEnvTest(unittest.TestCase):
    def test_home_is_redirected_away_from_real_home(self):
        real = os.path.realpath(os.path.expanduser("~/.eridian"))
        self.assertNotEqual(os.path.realpath(os.environ["ERIDIAN_HOME"]), real)

    def test_no_leaked_eridian_vars(self):
        leaked = [k for k in os.environ
                  if k.startswith("ERIDIAN_") and not k.startswith("ERIDIAN_TEST_")
                  and k != "ERIDIAN_HOME"]
        self.assertEqual(leaked, [])

    def test_single_temp_home_across_module_names(self):
        self.assertEqual(os.environ["ERIDIAN_HOME"], os.environ["ERIDIAN_TEST_HERMETIC_HOME"])
        homes = {m._TEST_HOME for name, m in list(sys.modules.items())
                 if name.endswith("test_00_hermetic_env") and hasattr(m, "_TEST_HOME")}
        self.assertEqual(homes, {os.environ["ERIDIAN_HOME"]})

    def test_planted_hermetic_home_is_not_trusted(self):
        planted = tempfile.mkdtemp(prefix="not-a-test-home-")
        self.addCleanup(shutil.rmtree, planted, ignore_errors=True)
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {**os.environ, "ERIDIAN_TEST_HERMETIC_HOME": planted}
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, 'tests'); import test_00_hermetic_env as m, os; "
             "print(os.environ['ERIDIAN_HOME'])"],
            cwd=repo, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        home = proc.stdout.strip()
        self.assertNotEqual(os.path.realpath(home), os.path.realpath(planted))
        self.assertTrue(os.path.basename(home).startswith(_PREFIX))

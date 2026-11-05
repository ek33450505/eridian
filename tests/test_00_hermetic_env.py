"""Scrub ERIDIAN_* from os.environ so the suite ignores the developer's shell.

`unittest discover -s tests` imports modules as top-level names and never runs
tests/__init__.py, so this file does the scrub at import time. Discovery imports
test modules in sorted order and this name sorts first. Tests that need a
specific variable set it themselves (mock.patch.dict). ERIDIAN_TEST_* opt-ins
(e.g. ERIDIAN_TEST_REAL_KOKORO) are kept. Running a single module directly
(not via discover) skips this scrub.
"""
import os

for _name in [k for k in os.environ if k.startswith("ERIDIAN_") and not k.startswith("ERIDIAN_TEST_")]:
    del os.environ[_name]

"""Package-mode hermetic setup: `python -m unittest tests.test_x` imports this package.

Importing test_00_hermetic_env applies the ERIDIAN_* scrub and ERIDIAN_HOME
redirect. Under `discover -s tests` this file never runs and test_00 is imported
top-level instead; a double load under discover reuses the same temp home (see the
test_00_hermetic_env docstring).
"""
from . import test_00_hermetic_env  # noqa: F401

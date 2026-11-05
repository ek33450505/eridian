"""`python -m eridian` entry point (used to spawn a background attach)."""

import sys

from .cli import main

sys.exit(main())

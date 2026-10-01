"""Filesystem layout for undertone's runtime state.

All paths are rooted at UNDERTONE_HOME (env override, default ~/.undertone).
Tests MUST set UNDERTONE_HOME to a temp dir — never touch the real ~/.undertone.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME_MODE = 0o700


def home_dir() -> Path:
    """Return UNDERTONE_HOME, creating it (mode 0700) if missing."""
    raw = os.environ.get("UNDERTONE_HOME")
    path = Path(raw).expanduser() if raw else Path.home() / ".undertone"
    path.mkdir(mode=HOME_MODE, parents=True, exist_ok=True)
    # mkdir's mode is masked by umask; force it explicitly.
    os.chmod(path, HOME_MODE)
    return path


def socket_path() -> Path:
    """Unix socket the speaker daemon listens on."""
    return home_dir() / "speaker.sock"


def enabled_flag_path() -> Path:
    """Flag file whose mere existence means 'speak replies is on'."""
    return home_dir() / "enabled"


def log_dir() -> Path:
    """Directory for hook/daemon logs, created if missing."""
    path = home_dir() / "logs"
    path.mkdir(mode=HOME_MODE, parents=True, exist_ok=True)
    return path

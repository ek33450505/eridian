"""Filesystem layout for eridian's runtime state.

All paths are rooted at ERIDIAN_HOME (env override, default ~/.eridian).
Tests MUST set ERIDIAN_HOME to a temp dir — never touch the real ~/.eridian.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME_MODE = 0o700


def home_path() -> Path:
    """Return ERIDIAN_HOME (default ~/.eridian) without creating it."""
    raw = os.environ.get("ERIDIAN_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".eridian"


def home_dir() -> Path:
    """Return ERIDIAN_HOME, creating it (mode 0700) if missing."""
    path = home_path()
    path.mkdir(mode=HOME_MODE, parents=True, exist_ok=True)
    # mkdir's mode is masked by umask; force it explicitly.
    os.chmod(path, HOME_MODE)
    return path


def voice_socket_path() -> Path:
    """Unix socket the `eridian attach` voice process listens on; channel servers connect to it."""
    return home_dir() / "voice.sock"


def log_dir() -> Path:
    """Directory for logs, created if missing."""
    path = home_dir() / "logs"
    path.mkdir(mode=HOME_MODE, parents=True, exist_ok=True)
    return path


def attach_pid_path() -> Path:
    """Pidfile of the running `eridian attach` (0600)."""
    return home_dir() / "attach.pid"


def attach_lock_path() -> Path:
    """flock target that serializes `eridian launch` check-and-spawn."""
    return home_dir() / "attach.lock"


def attach_log_path() -> Path:
    """Where a background attach appends its stdout and stderr."""
    return home_dir() / "attach.log"


def write_attach_pid(pid: int) -> None:
    """Record pid in the attach pidfile, created 0600."""
    path = attach_pid_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, f"{pid}\n".encode())
    finally:
        os.close(fd)


def read_attach_pid() -> int | None:
    """The pid in the attach pidfile, or None if missing or malformed."""
    try:
        return int(attach_pid_path().read_text().strip())
    except (OSError, ValueError):
        return None


def remove_attach_pid(pid: int) -> None:
    """Remove the pidfile, but only if it still names pid (a newer attach may own it)."""
    if read_attach_pid() == pid:
        try:
            attach_pid_path().unlink()
        except OSError:
            pass

"""Filesystem layout for eridian's runtime state.

All paths are rooted at ERIDIAN_HOME (env override, default ~/.eridian).
Tests MUST set ERIDIAN_HOME to a temp dir — never touch the real ~/.eridian.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

HOME_MODE = 0o700
VOICE_STATE_NAME = "voice-state.json"
PRIVATE_FILE_MODE = 0o600  # voice-state.json and its temp file


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


def control_socket_path() -> Path:
    """Unix socket the `eridian attach` process serves its mute / per-session voice control on; 0600."""
    return home_dir() / "control.sock"


def voice_state_path() -> Path:
    """Persisted per-session voice state (which sessions are OFF); written 0600."""
    return home_dir() / VOICE_STATE_NAME


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


def write_private_json(path: Path, obj: object) -> None:
    """Atomically replace `path` with `obj` as JSON, 0600 (temp file + os.replace).

    The temp file is created O_EXCL|O_NOFOLLOW next to the target, so a planted
    symlink is never written through; os.replace swaps the name itself.
    """
    data = json.dumps(obj).encode("utf-8")
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        os.unlink(tmp)  # a stale temp from a crashed write; unlink never follows a symlink
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, PRIVATE_FILE_MODE)
    try:
        try:
            os.fchmod(fd, PRIVATE_FILE_MODE)
            view = memoryview(data)
            while view:  # os.write may be short
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_private_json(path: Path, limit: int = 1 << 20) -> object:
    """Parse `path`: never follows a symlink, never blocks, regular files only.

    Raises OSError / ValueError; FileNotFoundError if missing. O_NONBLOCK keeps
    a FIFO planted at the path from hanging the open; fstat then rejects it.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path.name} is not a regular file")
        chunks: list[bytes] = []
        size = 0
        while size <= limit:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > limit:
        raise ValueError("state file too large")
    return json.loads(b"".join(chunks).decode("utf-8"))

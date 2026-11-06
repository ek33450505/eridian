"""Client for the control socket `eridian attach` serves (mute, per-session voice)."""

from __future__ import annotations

import errno
import json
import os
import socket
import stat
import struct
import sys
import time
from pathlib import Path

from . import paths, spoken

MAX_RESPONSE = 64 * 1024
NOT_RUNNING = (
    "Attach is not running (start it with eridian attach, or start a session with eridian session)"
)


class ControlError(Exception):
    """The control socket could not be used; the message is safe to print."""


def _check_socket(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise ControlError(NOT_RUNNING) from None
    except OSError:
        raise ControlError("could not inspect the control socket") from None
    if not stat.S_ISSOCK(st.st_mode):
        raise ControlError("refusing the control socket path: it is not a socket")
    if st.st_uid != os.getuid():
        raise ControlError("refusing the control socket: it is owned by another user")


def _peer_uid(sock: socket.socket) -> int:
    """uid of the process serving `sock`; raises ControlError when it can't be read."""
    try:
        if sys.platform == "darwin":
            # SOL_LOCAL=0, LOCAL_PEERCRED=1 -> struct xucred { u_int cr_version; uid_t cr_uid; ... }
            raw = sock.getsockopt(0, 1, 76)
            version, uid = struct.unpack_from("II", raw)
            if version != 0:  # XUCRED_VERSION; an unknown layout can't be trusted
                raise struct.error("unknown xucred version")
            return uid
        if sys.platform.startswith("linux"):
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", raw)[1]
    except (OSError, struct.error):
        pass
    raise ControlError("could not verify who owns the control socket; refusing it")


def request(obj: dict, *, timeout: float = 2.0, path: Path | None = None) -> dict:
    """Send one JSON request line and return the one JSON response object.

    `timeout` bounds the whole exchange, not each socket call.
    """
    sock_path = path if path is not None else paths.home_path() / "control.sock"
    _check_socket(sock_path)
    deadline = time.monotonic() + timeout
    too_slow = ControlError("Attach did not answer in time")

    def arm(sock: socket.socket) -> None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise too_slow
        sock.settimeout(max(left, 0.01))

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            arm(sock)
            sock.connect(str(sock_path))
            if _peer_uid(sock) != os.getuid():
                raise ControlError("refusing the control socket: it is served by another user")
            arm(sock)
            sock.sendall(json.dumps(obj).encode("utf-8") + b"\n")
            buf = b""
            while b"\n" not in buf:
                arm(sock)
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_RESPONSE:
                    raise ControlError("control response too large")
        except socket.timeout:
            raise too_slow from None
        except (ConnectionRefusedError, FileNotFoundError):
            raise ControlError(NOT_RUNNING) from None
        except OSError as exc:
            if exc.errno in (errno.ECONNREFUSED, errno.ENOENT):
                raise ControlError(NOT_RUNNING) from None
            raise ControlError("Could not talk to attach") from None
    finally:
        sock.close()
    line = buf.split(b"\n", 1)[0]
    try:
        reply = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ControlError("Attach sent a malformed response") from None
    if not isinstance(reply, dict):
        raise ControlError("Attach sent a malformed response")
    return reply


def clean(text: object, limit: int = 80) -> str:
    """Terminal-safe, truncated rendering of server-supplied text."""
    s = spoken.sanitize_terminal(str(text))
    s = "".join(c if c.isprintable() else "?" for c in s)
    return s if len(s) <= limit else s[: limit - 1] + "…"

"""The speaker daemon: a unix-socket server that turns text into speech.

Protocol is newline-delimited JSON over a unix socket at paths.socket_path():
  {"op": "say", "text": "..."}  -> speak text, interrupting any current speech
  {"op": "stop"}                -> kill current speech
  {"op": "ping"}                -> {"ok": true}

Speech runs via the command in UNDERTONE_SAY_CMD (default "say"); tests MUST
override this to a stub that never produces real audio.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import paths
from .spoken import sanitize_for_tts

SOCKET_BACKLOG = 5
_ACCEPT_POLL_TIMEOUT = 0.5
_CONN_DEADLINE = 1.0  # overall wall-clock budget per connection, not per-recv
_MAX_MESSAGE_BYTES = 64 * 1024

_current_process: subprocess.Popen | None = None


def send(msg: dict, timeout: float = 0.5) -> bool:
    """Send one JSON message to the daemon. Returns False (never raises) if it's down."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(paths.socket_path()))
            sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        raw = b"".join(chunks).decode("utf-8").strip()
        if not raw:
            return False
        response = json.loads(raw)
        return bool(response.get("ok"))
    except (OSError, ValueError):
        return False


def _respond(conn: socket.socket, payload: dict) -> None:
    try:
        conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
    except OSError:
        pass


def _log_error(context: str, exc: BaseException) -> None:
    try:
        log_path = paths.log_dir() / "speaker.log"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{context}] {exc!r}\n")
            fh.write(traceback.format_exc())
            fh.write("\n")
    except OSError:
        pass  # logging must never crash the daemon either


def build_say_argv(text: str) -> list[str] | None:
    """NUL-strip and sanitize_for_tts text; return None if empty; else [say_cmd, (-v voice), "--", text]."""
    # subprocess.Popen raises ValueError on an embedded NUL byte -- strip it
    # rather than letting one malformed "say" bring down the whole daemon.
    text = text.replace("\x00", "")
    # Hard control, not model compliance: the wake detector stays live while
    # we speak and there is no echo cancellation yet, so TTS saying the wake
    # phrase could self-trigger it (cut off speech, or capture the tail as a
    # command and confirm its own action). This is the single choke point for
    # all undertone speech (the daemon and SpeechQueue both go through here).
    text = sanitize_for_tts(text)
    if not text:
        return None
    say_cmd = os.environ.get("UNDERTONE_SAY_CMD", "say")
    voice = os.environ.get("UNDERTONE_VOICE", "Evan (Enhanced)")
    cmd = [say_cmd]
    if voice:
        cmd += ["-v", voice]
    # "--" stops `say` (and any well-behaved stub) from parsing `text` as
    # flags -- without it, a reply containing e.g. "-o/path/to/overwrite"
    # is argv flag injection into the TTS command. This is the ONE place
    # that builds the say argv; both the daemon (_say) and player.py's
    # SpeechQueue use it, and cli.py never shells out itself.
    cmd += ["--", text]
    return cmd


def _say(text: str) -> None:
    global _current_process
    _stop_current()
    argv = build_say_argv(text)
    if argv is None:
        return
    _current_process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop_current() -> None:
    global _current_process
    proc, _current_process = _current_process, None
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.0)


def _handle_connection(conn: socket.socket) -> None:
    # Deadline is for the WHOLE connection, not each recv() -- a client that
    # trickles in one byte at a time would otherwise be able to hold the
    # single-threaded accept loop hostage indefinitely.
    deadline = time.monotonic() + _CONN_DEADLINE
    buf = b""
    try:
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return  # connection exceeded its budget; drop it, no response
            conn.settimeout(remaining)
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > _MAX_MESSAGE_BYTES:
                return  # oversized message; drop the connection without acting
    except OSError:
        return

    line = buf.split(b"\n", 1)[0]
    try:
        msg = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        _respond(conn, {"ok": False, "error": "bad json"})
        return

    if not isinstance(msg, dict):
        # Valid JSON whose top level isn't an object, e.g. `[1]` or `"x"` --
        # there's no .get() to call on it, so bail out before trying.
        _respond(conn, {"ok": False, "error": "message must be a JSON object"})
        return

    op = msg.get("op")
    if op == "say":
        text = msg.get("text", "")
        if not isinstance(text, str):
            text = ""
        _say(text)
        _respond(conn, {"ok": True})
    elif op == "stop":
        _stop_current()
        _respond(conn, {"ok": True})
    elif op == "ping":
        _respond(conn, {"ok": True})
    else:
        _respond(conn, {"ok": False, "error": "unknown op"})


def serve(stop_event: threading.Event | None = None) -> int:
    """Run the speaker daemon until SIGINT/SIGTERM (or `stop_event` is set).

    `stop_event` lets tests run the server in a background thread and stop it
    deterministically; signal handlers can only be installed from the main
    thread, so they're skipped automatically when not running there.
    """
    sock_path = paths.socket_path()

    try:
        existing = os.lstat(sock_path)
    except FileNotFoundError:
        existing = None

    if existing is not None and not stat.S_ISSOCK(existing.st_mode):
        # Something that isn't ours (a regular file, a symlink, a directory,
        # ...) is sitting at the socket path. Never unlink something we
        # didn't create -- refuse to start instead.
        print(
            f"undertone: refusing to start -- {sock_path} exists and is not a "
            f"socket (mode {stat.filemode(existing.st_mode)})",
            file=sys.stderr,
        )
        return 1

    if existing is not None:
        # It IS a socket -- but is a server actually answering on it?
        if send({"op": "ping"}, timeout=0.3):
            print(f"undertone: a server is already listening at {sock_path}", file=sys.stderr)
            return 1
        sock_path.unlink()  # stale socket from a crashed/killed daemon

    stop_event = stop_event if stop_event is not None else threading.Event()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        server.listen(SOCKET_BACKLOG)
        server.settimeout(_ACCEPT_POLL_TIMEOUT)

        if threading.current_thread() is threading.main_thread():

            def _handle_signal(signum: int, frame: object) -> None:
                stop_event.set()

            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)

        while not stop_event.is_set():
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            with conn:
                try:
                    _handle_connection(conn)
                except Exception as exc:  # noqa: BLE001 - one bad connection must never kill the daemon
                    _log_error("handle_connection", exc)
    finally:
        _stop_current()
        server.close()
        if sock_path.exists():
            sock_path.unlink()

    return 0

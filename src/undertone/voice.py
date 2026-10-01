"""The Path A voice bridge: mic + speech on one side of a unix socket, a Claude
Code Channel MCP server (mcp_channel.py) on the other.

`undertone attach` runs this process. "hey jarvis" plus a spoken question is
transcribed on-device and sent to the attached channel as an `utterance`; the
channel injects it into Ed's interactive Claude Code session, Claude answers by
calling the channel's `reply` tool, and the channel sends the text back as a
`reply` line, which this process speaks and then opens a follow-up window.

Socket protocol (newline-delimited UTF-8 JSON, each line <= 64 KB; an oversized
line drops that connection). The socket is `paths.voice_socket_path()`, 0600:

    channel -> voice   {"type":"hello","pid":<int>,"cwd":"<str>"}   (first line)
                       {"type":"reply","text":"...","kind":"ack"|"answer"|"confirm"}
                                                                    (any number)
    voice -> channel   {"type":"utterance","text":"..."}
                       {"type":"detached"}   (sent to the OLD connection when a
                                              newer channel says hello; "latest
                                              session wins", then it is closed)

Reply `kind` gates the no-wake-word follow-up window: only an `answer` (the
default when `kind` is missing) opens it, once its speech goes idle. `ack` (an
early "running the tests now") never does, and neither does `confirm` -- a
spoken confirmation prompt must not be answerable by ambient audio (a video
saying "yes") inside an open mic window, so it needs a fresh wake word. An
unknown `kind` fails closed: it is spoken but opens no window.

Unknown types and malformed lines are ignored; a failure in one connection never
takes the process down.

Threading model: the `Listener` and `process_frame` are MAIN-thread only (as in
chat.ChatSession). An accept thread plus one reader thread per connection feed
replies to the player; one "speech watcher" thread waits for the player to go
idle and posts `("reply_done", generation)` to a control queue the main thread
drains before each frame. A generation counter (bumped on every utterance and
every barge-in) makes stale `reply_done` posts harmless. Lock order is session
lock, then the player's own lock; no lock is held across `player.stop()` or
socket I/O (the only I/O under a lock is the per-connection send lock, which
serializes writers on that one socket and is bounded).

Test seams: every collaborator (`wake_factory`, `vad`, `stt`, `clock`,
`player_factory`, `socket_path`, `cue`, `out`) is injected; tests/test_voice.py
uses fakes plus a real unix socket in a temp UNDERTONE_HOME -- no mic, speaker,
or real `claude`.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import select
import socket
import stat
import sys
import threading
from pathlib import Path
from typing import Callable, Iterable, Iterator, Protocol, TextIO

from . import paths
from .chunker import SentenceChunker
from .listener import Clock, Listener, ListenerConfig, ListenerEvent
from .player import SpeechQueue
from .spoken import sanitize_terminal

MAX_LINE_BYTES = 64 * 1024
NO_SESSION_SPEECH = "No Claude session is attached. Start one with undertone session."

_BACKLOG = 32  # above MAX_CONNECTIONS so the cap, not the kernel queue, is the limit
MAX_CONNECTIONS = 16
_POLL_S = 0.2  # accept/recv poll, so shutdown never waits on a blocked call
_IO_TIMEOUT_S = 2.0  # bound on any single socket send
_LOCK_TIMEOUT_S = 5.0


class PlayerLike(Protocol):
    busy: bool

    def enqueue(self, text: str) -> None: ...
    def stop(self) -> None: ...
    def wait_idle(self, timeout: float | None = None) -> bool: ...
    def close(self) -> None: ...


@contextlib.contextmanager
def _bounded(lock: threading.Lock, timeout: float = _LOCK_TIMEOUT_S) -> Iterator[None]:
    """Acquire `lock` or raise -- a wedged lock must surface, not hang the loop."""
    if not lock.acquire(timeout=timeout):
        raise TimeoutError("timed out acquiring a voice-bridge lock")
    try:
        yield
    finally:
        lock.release()


class _Conn:
    """One channel connection: the socket plus a lock serializing its writers."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.send_lock = threading.Lock()
        self.pid: int | None = None
        self.cwd = ""
        self.hello = False

    def send(self, obj: dict) -> bool:
        data = (json.dumps(obj) + "\n").encode("utf-8")
        try:
            with _bounded(self.send_lock, _IO_TIMEOUT_S):
                self.sock.sendall(data)
            return True
        except (OSError, TimeoutError):
            return False

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes a reader blocked in recv
        with contextlib.suppress(OSError):
            self.sock.close()


class VoiceBridge:
    def __init__(
        self,
        *,
        wake_factory,
        vad,
        stt,
        clock: Clock,
        socket_path: Path,
        player_factory: Callable[..., PlayerLike] = SpeechQueue,
        vad_reset=None,
        listener_config: ListenerConfig | None = None,
        follow_up_s: float = 6.0,
        cue: Callable[[str], None] | None = None,
        out: TextIO = sys.stdout,
    ) -> None:
        self._socket_path = Path(socket_path)
        self._follow_up_s = follow_up_s
        self._cue = cue or (lambda kind: None)
        self._out = out

        self._lock = threading.Lock()  # session lock: _gen, _conn, _conns, _closed
        self._print_lock = threading.Lock()
        self._chunk_lock = threading.Lock()
        self._gen = 0
        self._conn: _Conn | None = None  # the attached (latest-hello) channel
        self._conns: set[_Conn] = set()
        self._closed = False
        self._followed_gen = -1  # main thread only: dedupes reply_done per generation

        self._server: socket.socket | None = None
        self._bound = False
        self._threads: list[threading.Thread] = []
        self._chunker = SentenceChunker()
        self._control: queue.Queue = queue.Queue()
        self._speech_q: queue.Queue = queue.Queue()

        self._player = player_factory()
        self._listener = Listener(
            wake_factory=wake_factory,
            vad=vad,
            stt=stt,
            clock=clock,
            config=listener_config,
            on_event=self._on_listener_event,
            vad_reset=vad_reset,
        )

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> int:
        """Bind the listener socket and start the threads. 0 ok, 1 refused."""
        path = self._socket_path
        try:
            existing = os.lstat(path)
        except FileNotFoundError:
            existing = None

        if existing is not None and not stat.S_ISSOCK(existing.st_mode):
            # Never unlink something we didn't create.
            print(
                f"undertone: refusing to start -- {path} exists and is not a "
                f"socket (mode {stat.filemode(existing.st_mode)})",
                file=sys.stderr,
            )
            return 1

        if existing is not None:
            if _server_answers(path):
                print(f"undertone: another attach is already running at {path}", file=sys.stderr)
                return 1
            path.unlink()  # stale socket from a crashed/killed attach

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # umask so the socket is never wider than 0600, even briefly; the
            # explicit chmod below stays as the belt to this pair of braces.
            old_umask = os.umask(0o177)
            try:
                server.bind(str(path))
            finally:
                os.umask(old_umask)
            self._bound = True
            os.chmod(path, 0o600)
            server.listen(_BACKLOG)
            server.settimeout(_POLL_S)
        except OSError as exc:
            server.close()
            self._unlink_socket()
            print(f"undertone: cannot listen on {path}: {exc}", file=sys.stderr)
            return 1
        self._server = server

        for name, target in (
            ("undertone-voice-accept", self._accept_loop),
            ("undertone-voice-speech", self._speech_watcher),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return 0

    def close(self) -> None:
        with _bounded(self._lock):
            if self._closed:
                return
            self._closed = True
            self._gen += 1
            conns = list(self._conns)
            self._conn = None
        self._speech_q.put(None)
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()
        for conn in conns:
            conn.close()
        self._player.stop()
        # Accept and watcher first: once the accept thread is gone no new
        # readers can appear, so the copy below is complete.
        for thread in list(self._threads):
            thread.join(timeout=3.0)
        for thread in list(self._threads):
            thread.join(timeout=1.0)
        self._unlink_socket()
        self._player.close()

    def _unlink_socket(self) -> None:
        if not self._bound:
            return
        self._bound = False
        with contextlib.suppress(OSError):
            self._socket_path.unlink()

    # ---- main thread ---------------------------------------------------

    def process_frame(self, frame: bytes) -> None:
        self._drain_control()
        self._listener.process_frame(frame)

    def run(self, frames: Iterable[bytes]) -> None:
        for frame in frames:
            self.process_frame(frame)

    def _drain_control(self) -> None:
        while True:
            try:
                kind, gen = self._control.get_nowait()
            except queue.Empty:
                return
            if (
                kind == "reply_done"
                and self._follow_up_s > 0
                and self._listener.state == "IDLE"
                and gen != self._followed_gen
                and self._is_current(gen)
            ):
                self._followed_gen = gen
                self._listener.listen_now(self._follow_up_s)

    def _on_listener_event(self, event: ListenerEvent) -> None:
        if event.kind == "wake":
            self._cue("wake")
            self._maybe_barge_in()
        elif event.kind == "follow_up":
            self._cue("follow_up")
        elif event.kind == "utterance":
            text = event.text or ""
            self._print(f"you: {text}\n")
            with _bounded(self._lock):
                self._gen += 1
                conn = self._conn
            if conn is not None and conn.send({"type": "utterance", "text": text}):
                return
            self._print("[no Claude session attached]\n")
            self._player.enqueue(NO_SESSION_SPEECH)
        # wake_no_speech / dropped_empty: nothing to do

    def _maybe_barge_in(self) -> None:
        # Decide AND bump in one locked section (session lock, then the
        # player's); stop() can wait on `say` dying, so it runs outside.
        # `busy` is read best-effort: a missed barge-in is acceptable (the
        # player may flip right after) and stop() is idempotent.
        with _bounded(self._lock):
            if not self._player.busy:
                return
            self._gen += 1
        self._player.stop()
        self._print("[interrupted]\n")

    # ---- shared helpers ------------------------------------------------

    def _is_current(self, gen: int) -> bool:
        with _bounded(self._lock):
            return gen == self._gen

    def _print(self, text: str) -> None:
        with _bounded(self._print_lock):
            self._out.write(sanitize_terminal(text))
            self._out.flush()

    # ---- accept / reader threads ---------------------------------------

    def _accept_loop(self) -> None:
        server = self._server
        assert server is not None
        while not self._closed:
            try:
                sock, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return  # server closed
            try:
                sock.settimeout(_IO_TIMEOUT_S)
                conn = _Conn(sock)
                with _bounded(self._lock):
                    if self._closed:
                        conn.close()
                        return
                    over_cap = len(self._conns) >= MAX_CONNECTIONS
                    if not over_cap:
                        self._conns.add(conn)
                if over_cap:
                    self._print(f"[voice] too many connections (max {MAX_CONNECTIONS}); closed one\n")
                    conn.close()
                    continue
                reader = threading.Thread(
                    target=self._reader, args=(conn,), name="undertone-voice-reader", daemon=True
                )
                self._threads.append(reader)
                reader.start()
            except Exception as exc:  # noqa: BLE001 - never let accept die
                self._print(f"[voice] accept error: {type(exc).__name__}: {exc}\n")
                with contextlib.suppress(Exception):
                    sock.close()

    def _reader(self, conn: _Conn) -> None:
        try:
            self._read_lines(conn)
        except Exception as exc:  # noqa: BLE001 - one bad connection must never kill us
            with contextlib.suppress(Exception):
                self._print(f"[voice] connection error: {type(exc).__name__}: {exc}\n")
        finally:
            self._drop(conn)

    def _read_lines(self, conn: _Conn) -> None:
        buf = b""
        while not self._closed:
            # select + recv rather than a recv timeout: the socket's own
            # timeout is the (longer) send bound, and a recv blocked in the
            # kernel is not reliably woken by a close from another thread.
            try:
                ready, _, _ = select.select([conn.sock], [], [], _POLL_S)
                if not ready:
                    continue
                data = conn.sock.recv(4096)
            except (OSError, ValueError):  # closed under us (fileno -1)
                return
            if not data:
                return
            buf += data
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line, buf = buf[:nl], buf[nl + 1 :]
                if len(line) > MAX_LINE_BYTES:
                    return
                self._handle_line(conn, line)
            if len(buf) > MAX_LINE_BYTES:
                return  # oversized line with no newline yet

    def _handle_line(self, conn: _Conn, line: bytes) -> None:
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        if kind == "hello":
            pid, cwd = msg.get("pid"), msg.get("cwd")
            if isinstance(pid, int) and not isinstance(pid, bool) and isinstance(cwd, str):
                self._attach(conn, pid, cwd)
        elif kind == "reply":
            text = msg.get("text")
            if isinstance(text, str):
                reply_kind = msg.get("kind", "answer")
                self._on_reply(conn, text, reply_kind if isinstance(reply_kind, str) else "?")

    def _attach(self, conn: _Conn, pid: int, cwd: str) -> None:
        conn.pid, conn.cwd, conn.hello = pid, cwd, True
        with _bounded(self._lock):
            old, self._conn = self._conn, conn
        if old is not None and old is not conn:
            old.send({"type": "detached"})
            old.close()
        self._print(f"[attached: session pid {pid} in {cwd}]\n")

    def _drop(self, conn: _Conn) -> None:
        conn.close()
        with _bounded(self._lock):
            self._conns.discard(conn)
            was_current = self._conn is conn
            if was_current:
                self._conn = None
            closed = self._closed
        if was_current and not closed:
            self._print("[detached]\n")

    def _on_reply(self, conn: _Conn, text: str, kind: str = "answer") -> None:
        with _bounded(self._lock):
            if conn is not self._conn:
                return  # a replaced / never-hello'd connection has no voice
        self._print(f"claude: {text}\n")
        with _bounded(self._chunk_lock):
            self._chunker.reset()
            sentences = self._chunker.feed(text) + self._chunker.flush()
        with _bounded(self._lock):
            gen = self._gen
            for sentence in sentences:
                self._player.enqueue(sentence)
        if kind == "confirm":
            self._print('[confirmation needed: say "hey jarvis" and the phrase]\n')
        self._speech_q.put((gen, kind))

    # ---- speech watcher thread -----------------------------------------

    def _speech_watcher(self) -> None:
        while True:
            item = self._speech_q.get()
            if item is None:
                return
            gen, kind = item
            while not self._closed:
                if self._player.wait_idle(_POLL_S):
                    # Only an answer opens the open-mic window (see module doc).
                    if kind == "answer":
                        self._control.put(("reply_done", gen))
                    break


def _server_answers(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.3)
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def run_attach(*, no_beep: bool, follow_up_s: float) -> int:
    """The real wiring behind `undertone attach` (real mic; not unit-tested)."""
    # Lazy imports for the same reason as cli._cmd_chat: keep `--help` fast and
    # the heavy audio/model deps out of import time. cli is imported lazily too
    # (it imports this module).
    import time

    from . import audio, cli
    from .stt import ModelNotCachedError, ParakeetSTT

    try:
        stt = ParakeetSTT()
    except ModelNotCachedError as exc:
        print(f"undertone: {exc}", file=sys.stderr)
        return 1

    from pysilero_vad import SileroVoiceActivityDetector

    vad = SileroVoiceActivityDetector()
    cues = {"wake": cli._WAKE_CUE, "follow_up": cli._FOLLOW_UP_CUE}

    bridge = VoiceBridge(
        wake_factory=cli._make_wake_factory(),
        vad=lambda chunk: vad(chunk),
        stt=stt,
        clock=time.monotonic,
        socket_path=paths.voice_socket_path(),
        vad_reset=vad.reset,
        follow_up_s=follow_up_s,
        cue=None if no_beep else (lambda kind: cli._play_cue(cues[kind])),
    )
    if bridge.start() != 0:
        bridge.close()
        return 1

    print(
        "undertone attach: listening for 'hey jarvis'. Start a Claude session with "
        "`undertone session` in another terminal. Ctrl-C to quit.",
        flush=True,
    )
    try:
        bridge.run(audio.frame_source())
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
    return 0

"""The Path A voice bridge: mic + speech on one side of a unix socket, a Claude
Code Channel MCP server (mcp_channel.py) on the other.

`eridian attach` runs this process. "hey jarvis" plus a spoken question is
transcribed on-device and sent to the attached channel as an `utterance`; the
channel injects it into Ed's interactive Claude Code session, Claude answers by
calling the channel's `reply` tool, and the channel sends the text back as a
`reply` line, which this process speaks and then opens a follow-up window.

Socket protocol (newline-delimited UTF-8 JSON, each line <= 64 KB; an oversized
line drops that connection). The socket is `paths.voice_socket_path()`, 0600:

    channel -> voice   {"type":"hello","pid":<int>,"cwd":"<str>"[,"wake":"<phrase>"][,"session":"<32 hex>"]}
                                                                    (first line)
                       {"type":"reply","text":"...","kind":"ack"|"answer"|"confirm"[,"check":"..."]}
                                                                    (any number; `check` is
                        honoured on a confirm only: what the voice guard blocked,
                        spoken in eridian's own voice before Claude's words)
    voice -> channel   {"type":"utterance","text":"...","via":"wake"|"wake_barge_in"|"follow_up"|"talk_over"}
                       (`via` = what opened the turn; the channel lets only "wake"
                        satisfy a confirm phrase)
                       {"type":"detached"}   (sent to an OLD connection only when a
                                              newer hello carries the SAME session
                                              token, i.e. that session reconnected;
                                              then the old one is closed)

    hook -> voice      {"type":"announce","session":"<32 hex>","event":"<event>"}
                       (typically a one-shot connection with NO hello: it never
                        attaches or detaches anything)

Multi-attach and per-session voice (on/off): every hello'd connection stays
connected; a new hello detaches nobody (except a same-token reconnect, above).
Sessions are keyed by the hello `session` token and start ON. Wake / follow-up /
talk-over utterances go to the ROUTING TARGET: the most recently attached session
that is ON ("latest ON wins"). Older ON sessions stay connected but hear nothing
until the newer one is turned off or disconnects, then routing falls back to the
next most recent ON session. An OFF session is fully silent: it gets no
utterances, its replies are not spoken (`[not spoken: ...]` is printed), and its
announces are ignored. A reply from an ON session that is not the target is not
spoken either. A hello with no / invalid token is tracked (listable) but is never
routable, as if permanently OFF. Whenever the target changes the per-session turn
flags (_answered, _confirm_pending, _outstanding, _reply_origin) are reset, so a
pending confirmation never carries to another session. With no session at all the
bridge says NO_SESSION_SPEECH; with sessions but none ON, ALL_OFF_SPEECH.

OFF is persisted in `paths.voice_state_path()` (voice-state.json, 0600, written
atomically) keyed by sha256(token), never the raw token, capped at 64 entries.
A missing file means nothing is off; a corrupt / unreadable / wrong-shape file
fails toward not listening: EVERY session is treated as OFF (`state_error`)
until a write replaces the file. The in-process API for the control surface is
`sessions_status()`, `set_voice()`, `set_muted()` / `is_muted()` and
`voice_state_health()`.

State file v2: `{"version":2, "off":{sha256hex: ts}, "muted":bool}`. A v1 file (no
"muted") loads as unmuted; any other version, or a non-bool "muted", is corrupt.

Mute is GLOBAL and REAL: while muted, `process_frame` still drains the control
queue (replies and announces keep playing) but never hands a frame to the
listener, so no wake word, VAD or STT ever runs. At each mute / unmute transition
the main thread calls `Listener.cancel()`: a turn in progress when mute lands is
discarded, so nothing captured before the mute is transcribed or sent, and unmute
starts from fresh detectors. While muted the follow-up window does not open. Mute
fails toward not listening: unreadable / corrupt state starts MUTED (and every
session OFF); a mute is applied in memory before its write is tried (a failed
write leaves it muted, `saved` False); an unmute needs a successful write first
(else nothing changes and it returns False); an unmute during `state_error` uses
the same recovery write as voice ON, which records every connected tokened
session OFF, so recovering by unmuting never makes an OFF session audible. One
transaction (`_change_state`) serves set_voice and set_muted. A MUTE during
`state_error` also performs that recovery write (every connected tokened session
recorded OFF, `muted: true`, `state_error` cleared): fail-closed, intended.
The cancel is driven by a mute-change counter, so a quick mute->unmute between two
frames still cancels. The same counter gives each turn a MUTE EPOCH (`_turn_mute_gen`,
recorded when `wake` / `talk_over` / `follow_up` opens it; those events do nothing while
muted: no cue, no barge-in, no turn). `_turn_muted_locked()` is the one rule for an
utterance: muted now, or any mute change since the turn opened. The utterance branch
decides everything first (echo drop, strip_echo, too-soon hold, outgoing text) with no
prints or enqueues; then, under the session lock, applies that rule and bumps the turn
counters; then `conn.send` is the very next statement, with no I/O between. Only after the
send returns does any transcript text print. A muted turn prints just `[muted: not sent]`,
touches no turn flags and sends nothing; the non-send outcomes (echo drop, own-reply,
too-soon hold) apply the same rule before they print. The one remaining window: a mute
landing between the in-lock check and the `conn.send` call (a few bytecodes, no I/O), and a
frame already inside the listener; the next frame is dropped and the listener cancelled. For
a non-send outcome, audio captured BEFORE the mute may print in the bytecodes after its check;
nothing is sent and no post-mute audio is involved. The player's busy state keeps being
tracked while muted, so the self-wake guard survives a mute. If the control accept loop gives
up (ACCEPT_MAX_ERRORS consecutive failures) the mute switch is gone, so the bridge mutes
itself and prints `[control socket failed; muted]`.

Control socket (`paths.control_socket_path()`, 0600, bound like voice.sock). It is
the mute switch, so `start()` FAILS (and releases voice.sock) if it cannot be
bound. Newline-delimited JSON, one response line per request line, several
requests per connection allowed; caps: CONTROL_MAX_CONNECTIONS connections,
CONTROL_MAX_LINE_BYTES per line (longer drops the connection); a request line must
complete within CONTROL_IDLE_S of its first byte, idle between requests is capped at
CONTROL_IDLE_S, a connection lives at most CONTROL_LIFETIME_S and serves at most
CONTROL_MAX_REQUESTS requests (so a slow drip cannot hold a slot). A session `cwd`
in status is stripped of control characters and cut to CONTROL_CWD_CHARS:

    {"op":"status"}  -> {"ok":true,"muted":b,"state_error":b,"saved":b,"sessions":[...]}
    {"op":"voice_on"|"voice_off","session":"<id or pid>"} | {"op":"mute"} | {"op":"unmute"}
                     -> {"ok":true} | {"ok":false,"error":"<fixed text>"}
    anything else (unknown op, bad JSON, wrong types) -> {"ok":false,"error":"bad request"}

Errors are fixed text (no such session / can't turn on a session without a token /
could not save) and never echo the request. Nothing on this socket can create an
utterance, open a listening turn or grant anything: it only toggles state and
reports it. Each state change prints a terminal line.

Races, stated rather than closed: no lock is held across socket I/O, so an
utterance whose send began while its session was ON can still reach it if an OFF
lands in that microsecond window. Speech already playing is cut when a session
that has speech queued or playing is turned OFF, because OFF means silent: the
bridge keeps `_speakers`, the hashes of every session that enqueued speech
(replies, and announces it triggered) since the player last went idle after the
latest such enqueue. OFF on a member bumps the generation and stops the player
(which empties the single player queue, so a reply the CURRENT target queued
behind it is dropped as well); OFF on any other session does not.
A reply the NEW target enqueues in the microsecond between the OFF commit and the
stop() may be cut too; that is accepted. Speech is NOT cut when routing merely
moves on (a newer session attached, or the speaking session disconnected) --
only an explicit OFF cuts. A connection that has not said a valid hello is closed
NO_HELLO_IDLE_S after accept, a hard deadline no other line resets (hello'd
connections are exempt); a second hello on a connection is ignored.

Announce: a hook can make the bridge speak a short FIXED sentence
(ANNOUNCE_TEMPLATES, e.g. "Claude finished.") without a reply. The line is
honoured only if `session` equals (hmac.compare_digest) the `session` the
current routing target sent in its hello, and `event` is enabled by the
`--announce` level; anything else is silently ignored. Nothing from the message
is ever spoken or printed except the validated event key. The reader thread only
posts `("announce", event)` to the control queue; the main thread decides:
"finished" is skipped if a non-ack reply was already spoken since the last
"finished", and while a confirm (or unknown-kind) reply is pending -- i.e. until
an utterance is next sent to the channel -- because a no-wake-word follow-up
window must never be able to burn a pending confirm challenge. Any announce is
skipped while the listener is not IDLE or if the same event was spoken within
ANNOUNCE_REPEAT_S. An announcement queues behind current speech (never stops
it), bumps the generation, and plays with talk-over off. No announcement
(including "finished") ever opens the follow-up window: a session's Stop hook
fires after every turn, so ambient speech in such a window would chain turns.

Reply `kind` gates the no-wake-word follow-up window: only an `answer` (the
default when `kind` is missing) opens it, once its speech goes idle, and only
as a one-shot credit: when an utterance is sent, its `via` is remembered, and
the first non-`ack` reply afterwards spends it. Only "wake" / "wake_barge_in"
credit a window; an answer to a `follow_up` or `talk_over` turn, a second or
unprompted answer, one landing after an announcement or barge-in, or one with
no known origin opens none, so ambient audio can never chain windows. The
protocol carries no turn ids, so the bridge grants a window only when exactly
one utterance is outstanding (sent, not yet answered; reset by "finished", a
routing-target change) and the credit is under REPLY_WINDOW_CREDIT_TTL_S old;
any ambiguity fails closed (no window). A reset on Stop can let the answer to a
still-queued follow_up spend a wake credit sent right after it (one short
window, no chaining, no grant); closing that needs turn ids in the protocol. `ack` (an
early "running the tests now") never does, and neither does `confirm` -- a
spoken confirmation prompt must not be answerable by ambient audio (a video
saying "yes") inside an open mic window, so it needs a fresh wake word. An
unknown `kind` fails closed: it is spoken but opens no window.

Talk-over (`talk_over=True`, only when echo cancellation is on): while a reply
plays, sustained user speech stops it without the wake word and becomes the
next utterance. During confirm (and unknown-kind) speech talk-over is OFF: a
confirm prompt is short and must always be heard in full, so it plays to the
end. "hey jarvis" still interrupts it, and answering still needs the wake word
-- ambient audio must not be able to answer a confirm prompt. That rule is
enforced downstream too: every utterance carries `via`, and only a
wake-word turn can grant a confirmation (a talk-over turn could be our own
speech leaking through imperfect echo cancellation). A talk-over transcript
has the words that repeat the reply being spoken removed (`spoken.strip_echo`);
if nothing is left it is dropped instead of sent.

Echo text guard: a `talk_over` or `follow_up` turn (never a wake turn) whose
transcript mostly repeats what eridian itself just said (replies, read-backs,
announcements; numbers compared as digits, so "ninety three" matches "93") is
dropped and printed as `[dropped: echo of own speech]`, because a live speaker
leaks more than the offline AEC check shows. It only drops: it never changes
whether a turn may grant a confirmation. A talk-over that already stopped
playback stays stopped. attach prints every turn except a plain `wake` turn as
`you [via]: ...` (`talk_over`, `follow_up`, `wake_barge_in`).

`wake_barge_in`: a wake word heard while we are speaking, or within
SELF_WAKE_GUARD_S after we stopped, may be our own voice tripping the detector
(a reply containing "hey jarvice"). It still interrupts and opens a turn, but
that turn is tagged `wake_barge_in` and can never grant a confirmation; only a
wake word heard after a full second of silence can.
While a confirmation is pending, a `wake_barge_in` turn is held, not sent (it
could not grant, and sending it would use up the challenge): eridian says so
and the user repeats the wake word after a moment.

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
uses fakes plus a real unix socket in a temp ERIDIAN_HOME -- no mic, speaker,
or real `claude`.
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import hmac
import json
import math
import os
import queue
import re
import select
import signal
import socket
import stat
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Callable, Iterable, Iterator, Protocol, TextIO

from . import paths, wake
from .chunker import SentenceChunker
from .listener import Clock, Listener, ListenerConfig, ListenerEvent
from .player import SpeechQueue
from .spoken import is_echo, sanitize_terminal, strip_echo
from . import voice_guard
from .voice_guard import TOKEN_RE

MAX_LINE_BYTES = 64 * 1024
NO_SESSION_SPEECH = "No Claude session is attached. Start one with eridian session."
_RECOVERY_ATTEMPTS = 3  # set_voice / set_muted retry when a session connects during a recovery write
NO_HELLO_IDLE_S = 5.0  # a connection with no hello is closed after this long without a complete line
ALL_OFF_SPEECH = "Voice is off in every session. Turn one on in the eridian menu or with eridian voice on."
MAX_OFF_ENTRIES = 64  # persisted OFF sessions; the oldest are dropped past this
VOICE_STATE_VERSION = 2  # v1 files (no "muted") still load, as unmuted
CONTROL_MAX_CONNECTIONS = 8
CONTROL_MAX_LINE_BYTES = 4096
CONTROL_IDLE_S = 5.0  # a request line must complete this long after its first byte; idle between requests is capped too
CONTROL_LIFETIME_S = 30.0  # absolute cap on one control connection
CONTROL_MAX_REQUESTS = 64  # requests served on one control connection
ACCEPT_MAX_ERRORS = 50  # consecutive accept failures (~10 s) before an accept loop gives up
CONTROL_CWD_CHARS = 200  # status shows at most this much of a session's cwd
CONTROL_BAD_REQUEST = "bad request"
CONTROL_ERRORS = {
    "no_session": "no such session",
    "no_token": "can't turn on a session without a token",
    "save": "could not save",
}
WAKE_MISMATCH_REPEAT_S = 60.0  # speak the mismatch notice at most once per window, whatever phrase is claimed
WAKE_MISMATCH_SPEECH = "This session uses a different wake word than the voice process. Restart the session."
TOO_SOON_SPEECH = "That was too soon after I spoke. Wait a moment, then say the wake word again."

ANNOUNCE_TEMPLATES = {
    "finished": "Claude finished.",
    "permission": "Claude needs your permission.",
    "question": "Claude has a question for you.",
    "plan": "Claude's plan is ready.",
    "error": "Claude stopped with an error.",
}
ANNOUNCE_LEVELS = {
    "all": frozenset(ANNOUNCE_TEMPLATES),
    "permission": frozenset(ANNOUNCE_TEMPLATES) - {"finished"},
    "off": frozenset(),
}
ANNOUNCE_REPEAT_S = 3.0  # the same event is not spoken twice within this
WAKE_TURN_VIAS = ("wake", "wake_barge_in")  # turns whose answer may open the no-wake window
REPLY_WINDOW_CREDIT_TTL_S = 600.0  # a window credit older than this is void (ack-ed long work still fits)

_BACKLOG = 32  # above MAX_CONNECTIONS so the cap, not the kernel queue, is the limit
MAX_CONNECTIONS = 16
MAX_CHECK_CHARS = 600  # a confirm reply's read-back is ~400 chars at most
# A wake word heard while we speak, or this long after we stop, is treated as
# possibly our own voice. The detector fires 50-250 ms after the phrase ends
# (measured) plus output latency, so a self-wake on a reply's last words can
# land after the player is already idle. Such a turn is `wake_barge_in`, which
# never grants a confirmation.
SELF_WAKE_GUARD_S = 1.0
_SELF_WAKE_GUARD_MAX_S = 10.0
# Text guard for turns with no wake word (talk_over, follow_up): the transcript
# is compared with what eridian itself said. A turn is checked only if it
# opened while we were speaking or within ECHO_GUARD_S after the player went
# idle: the 6 s follow-up window opens right at idle, so its first words are
# the room's tail of our last sentence (reverb, output latency, AEC residue);
# 3 s is that tail with margin, and anything later is the user. Spoken text
# older than ECHO_HISTORY_S before the turn opened is not compared.
ECHO_GUARD_S = 3.0
ECHO_HISTORY_S = 60.0
_POLL_S = 0.2  # accept/recv poll, so shutdown never waits on a blocked call
_IO_TIMEOUT_S = 2.0  # bound on any single socket send
_LOCK_TIMEOUT_S = 5.0


class PlayerLike(Protocol):
    busy: bool

    def enqueue(self, text: str, *, own: bool = False) -> None: ...
    def stop(self) -> None: ...
    def wait_idle(self, timeout: float | None = None) -> bool: ...
    def close(self) -> None: ...


_CWD_DROP_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")  # control, format (e.g. U+202E), line / paragraph separators


def _sid_hash(token: str) -> str:
    """The persisted key for a session: the raw token is never stored."""
    return hashlib.sha256(token.encode()).hexdigest()


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
        self.session = ""  # 32-hex token from hello; "" can never match an announce

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
        talk_over: bool = False,
        self_wake_guard_s: float = SELF_WAKE_GUARD_S,
        announce: frozenset[str] = frozenset(),
        home: Path | None = None,
        no_hello_idle_s: float = NO_HELLO_IDLE_S,
        control_socket_path: Path | None = None,
        control_idle_s: float = CONTROL_IDLE_S,
    ) -> None:
        self._control_idle_s = control_idle_s
        self._no_hello_idle_s = no_hello_idle_s
        self._home = Path(home) if home is not None else None  # resolved lazily: no mkdir at construction
        self._socket_path = Path(socket_path)
        self._control_path: Path | None = None  # resolved lazily like the home: no mkdir at construction
        if control_socket_path is not None:
            self._control_path = Path(control_socket_path)
        elif self._home is not None:
            self._control_path = self._home / "control.sock"
        self._follow_up_s = follow_up_s
        self._talk_over = talk_over
        self._announce = announce
        self._cue = cue or (lambda kind: None)
        self._out = out

        self._lock = threading.Lock()  # session lock: _gen, _conn, _conns, _closed
        self._print_lock = threading.Lock()
        self._chunk_lock = threading.Lock()
        self._gen = 0
        self._no_talk_over_gen = -1  # gen whose speech (confirm) talk-over must not cut
        self._spoken_gen = -1  # gen that _spoken_text belongs to (session lock)
        self._spoken_text = ""  # reply text spoken in that gen, for the echo filter
        self._turn_mute_gen = 0  # main thread: _mute_gen when the current turn opened
        self._turn_via = ""  # main thread: what opened the current turn (tagging / echo filter only)
        self._reply_origin = ""  # session lock: via of the sole outstanding utterance; one-shot credit for one answer
        self._reply_origin_at = 0.0  # session lock: self._clock() when the credit was set
        self._outstanding = 0  # session lock: utterances sent and not yet answered
        self._echo_ref = ""  # main thread: spoken text snapshotted at talk-over
        self._spoken_log: collections.deque[tuple[float, str]] = collections.deque(maxlen=24)  # session lock: (time, text) of everything we said
        self._echo_hist = ""  # main thread: spoken text a non-wake turn is checked against
        self._conn: _Conn | None = None  # the routing target: newest hello'd session that is ON
        self._sessions: list[_Conn] = []  # session lock: hello'd connections, oldest first
        self._state_lock = threading.Lock()  # serializes voice-state changes; taken BEFORE the session lock
        self._off: dict[str, float] = {}  # session lock: sha256(token) -> when turned off
        self._state_extra: dict = {}  # unknown keys of voice-state.json, kept across writes
        self._muted = False  # written under the session lock (and _state_lock); the main thread reads it every frame
        self._mute_gen = 0  # bumped (session lock) on every mute-state change; never reset
        self._mute_applied_gen = 0  # main thread only: the _mute_gen the listener was last cancelled for
        self._threads_lock = threading.Lock()
        self._state_error = False  # session lock: state file unreadable -> every session is OFF
        self._saved = True  # session lock: False after an OFF whose write failed (file still says ON)
        self._speakers: set[str] = set()  # session lock: _sid_hash of sessions with speech queued/playing
        self._enq_seq = 0  # session lock: bumped on every session-attributed enqueue
        self._conns: set[_Conn] = set()
        self._closed = False
        self._followed_gen = -1  # main thread only: dedupes reply_done per generation
        self._answered = False  # session lock: a non-ack reply was spoken since the last "finished"
        self._confirm_pending = False  # session lock: confirm/unknown reply, no utterance sent since
        self._confirm_pending_at = 0.0  # session lock: self._clock() when _confirm_pending was set
        self._announce_last: dict[str, float] = {}  # main thread only: event -> last spoken
        self._clock = clock
        self._self_wake_guard_s = self_wake_guard_s
        self._last_busy = float("-inf")  # main thread only: last frame the player was busy

        self._server: socket.socket | None = None
        self._bound = False
        self._control_server: socket.socket | None = None
        self._control_bound = False
        self._control_ident: tuple[int, int] | None = None  # (dev, inode) of the socket file we bound
        self._control_conns: set[_Conn] = set()  # guarded by the session lock
        self._threads: list[threading.Thread] = []
        self._chunker = SentenceChunker()
        self._control: queue.Queue = queue.Queue()
        self._mismatch_spoken_at: float | None = None  # when we last spoke the wake-mismatch notice
        self._speech_q: queue.Queue = queue.Queue()

        self._load_state()
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

    # ---- per-session voice state ---------------------------------------

    def _state_file(self, create: bool) -> Path:
        if self._home is None:
            return paths.voice_state_path() if create else paths.home_path() / paths.VOICE_STATE_NAME
        if create:
            self._home.mkdir(mode=paths.HOME_MODE, parents=True, exist_ok=True)
        return self._home / paths.VOICE_STATE_NAME

    def _load_state(self) -> None:
        """Constructor only. Missing file = nothing off; anything unusable = every session OFF."""
        try:
            raw = paths.read_private_json(self._state_file(create=False))
            off = raw.get("off") if isinstance(raw, dict) else None
            version = raw.get("version") if isinstance(raw, dict) else None
            muted = False if version == 1 else raw.get("muted") if isinstance(raw, dict) else None  # v1 has no "muted"
            if (
                not isinstance(raw, dict)
                or type(version) is not int
                or version not in (1, VOICE_STATE_VERSION)
                or not isinstance(muted, bool)
                or not isinstance(off, dict)
                or not all(
                    isinstance(k, str) and re.fullmatch(r"[0-9a-f]{64}", k)
                    and isinstance(v, (int, float)) and not isinstance(v, bool)
                    for k, v in off.items()
                )
            ):
                raise ValueError("unexpected shape")
        except FileNotFoundError:
            return
        except (OSError, ValueError, RecursionError):
            self._state_error = True
            self._muted = True  # fail toward not listening
            self._print(
                "[voice] voice-state.json is unreadable: treating every session as off and "
                "starting muted until a voice on/off or unmute change rewrites it\n"
            )
            return
        self._off = dict(off)
        self._muted = muted
        # Hook for later keys: carried through every write untouched.
        self._state_extra = {k: v for k, v in raw.items() if k not in ("version", "off", "muted")}

    def _write_state(
        self, off: dict[str, float], live: frozenset[str] = frozenset(), muted: bool = False
    ) -> bool:
        """Persist `off` and `muted`; False if the write failed. No lock needed.

        Past MAX_OFF_ENTRIES only entries of disconnected sessions (hashes not in
        `live`) are dropped, oldest first; a connected session's OFF is kept.
        """
        ranked = sorted(off.items(), key=lambda kv: (kv[0] in live, kv[1]))
        kept = dict(ranked[-MAX_OFF_ENTRIES:])
        doc = {**self._state_extra, "version": VOICE_STATE_VERSION, "off": kept, "muted": muted}
        try:
            paths.write_private_json(self._state_file(create=True), doc)
        except (OSError, TypeError, ValueError) as exc:
            self._print(f"[voice] could not save voice state: {type(exc).__name__}: {exc}\n")
            return False
        return True

    def _is_off_locked(self, conn: _Conn) -> bool:
        """Session lock held. Token-less sessions are never routable; a bad state file means all OFF."""
        return self._state_error or not conn.session or _sid_hash(conn.session) in self._off

    def _reset_turn_flags_locked(self) -> None:
        self._answered = self._confirm_pending = False
        self._outstanding, self._reply_origin = 0, ""

    def _retarget_locked(self) -> bool:
        """Session lock held. Point `_conn` at the newest ON session; True if it moved."""
        new = next((c for c in reversed(self._sessions) if not self._is_off_locked(c)), None)
        if new is self._conn:
            return False
        self._conn = new
        self._reset_turn_flags_locked()  # a pending confirm must never reach another session
        return True

    @staticmethod
    def _session_id(conn: _Conn) -> str:
        return _sid_hash(conn.session)[:12] if conn.session else f"pid-{conn.pid}"

    def sessions_status(self) -> list[dict]:
        """One dict per hello'd connection, newest first. Never includes the raw token."""
        with _bounded(self._lock):
            return [
                {
                    "id": self._session_id(c),
                    "pid": c.pid,
                    "cwd": "".join(ch for ch in c.cwd if unicodedata.category(ch) not in _CWD_DROP_CATEGORIES)[:CONTROL_CWD_CHARS],
                    "on": not self._is_off_locked(c),
                    "target": c is self._conn,
                }
                for c in reversed(self._sessions)
            ]

    def voice_state_health(self) -> dict:
        """`state_error`: the state file was unreadable (all sessions off). `saved`: False after an
        OFF whose write failed, i.e. it would turn back on if attach restarts; True after any
        successful write."""
        with _bounded(self._lock):
            return {"state_error": self._state_error, "saved": self._saved}

    def _matches_locked(self, session_id: str) -> list[_Conn]:
        """Session lock held. Connections whose id (see sessions_status) or decimal pid is `session_id`."""
        return [
            c for c in self._sessions
            if self._session_id(c) == session_id or str(c.pid) == session_id
        ]

    def set_voice(self, session_id: str, on: bool) -> bool:
        """Turn voice on/off for the session(s) with this id (see sessions_status) or decimal pid.

        False if nothing matches. Rule: a failure to persist can never make a
        session audible that the user turned off. OFF is applied in memory
        BEFORE the write is tried, so it takes effect even if the write fails
        (printed, and `voice_state_health()["saved"]` goes False). ON needs a
        successful write first (else nothing changes and False is returned),
        and a token-less session can never be turned on. A successful write
        replaces the file, so it also clears `state_error`; because the lost
        file may have said some connected sessions were off, every other
        session connected at the moment of the write is written OFF
        explicitly, so only the session being turned on becomes audible. The write
        runs outside the lock: if a tokened session connected meanwhile and the
        file does not cover it, the write is redone (3 attempts); if it is
        still racing, state_error stays set, everything stays OFF and an ON
        returns False.
        Turning OFF the session that owns the speech in flight cuts it.
        """
        if not isinstance(session_id, str):
            return False
        return self._change_state(session_id, on, None)

    def set_muted(self, muted: bool) -> bool:
        """Mute or unmute the microphone for every session (global, persisted).

        Same discipline as set_voice: MUTE lands in memory before any I/O, so it
        holds even when the write fails (`saved` goes False). UNMUTE needs a
        successful write first (else nothing changes and False is returned),
        and during `state_error` it goes through the same recovery write as an
        ON, which records every connected tokened session OFF, so unmuting never
        makes an OFF session audible. The listener is cancelled on the main
        thread (see _sync_mute) at each transition.
        """
        if not isinstance(muted, bool):
            return False
        return self._change_state(None, not muted, muted)

    def is_muted(self) -> bool:
        with _bounded(self._lock):
            return self._muted

    def _change_state(self, session_id: str | None, on: bool, mute: bool | None) -> bool:
        """The snapshot / write / commit / retry transaction shared by set_voice and set_muted.

        `on` is the direction that needs a write first (voice on, unmute); the
        other direction is applied in memory before any I/O. `session_id` None
        means no session is targeted (a mute change); `mute` None leaves mute as is.
        """
        changed = stop = False
        target = None
        matches: list[_Conn] = []
        muted_changed = False
        with _bounded(self._state_lock):
            with _bounded(self._lock):
                was_muted = self._muted
            for _attempt in range(_RECOVERY_ATTEMPTS):
                with _bounded(self._lock):
                    hashes: set[str] = set()
                    if session_id is not None:
                        matches = self._matches_locked(session_id)
                        hashes = {_sid_hash(c.session) for c in matches if c.session}
                        if not matches or (on and not hashes):
                            return False
                    new_muted = self._muted if mute is None else mute
                    recovering = self._state_error
                    new_off = self._off_after_locked(hashes, on, recovering)
                    live = self._live_hashes_locked()
                    if not on:  # OFF / mute land in memory before any I/O can fail or raise
                        ch, target, st = self._commit_locked(new_off, matches, on, False, None, new_muted)
                        changed, stop = changed or ch, stop or st
                if on or hashes or recovering or mute is not None:
                    wrote = self._write_state(new_off, live, new_muted)  # no lock held across the write
                else:
                    wrote = None  # only token-less sessions: nothing to persist
                with _bounded(self._lock):
                    if on and not wrote:
                        return False
                    if recovering and wrote and not self._live_hashes_locked() <= live:
                        continue  # a tokened session connected mid-write: the file misses it
                    if on:
                        ch, target, st = self._commit_locked(new_off, matches, on, True, True, new_muted)
                        changed, stop = changed or ch, stop or st
                    elif wrote is not None:
                        self._saved = wrote
                        if wrote and recovering:
                            self._state_error = False
                break
            else:
                # Still racing after every attempt: fail closed. state_error stays set, so
                # every session stays OFF and the mic stays muted; an OFF / mute already applied stands.
                if on:
                    return False
            with _bounded(self._lock):
                muted_changed = self._muted != was_muted
        if stop:
            self._player.stop()  # outside the locks, like a barge-in
        label = "on" if on else "off"
        for c in matches:
            self._print(f"[voice {label}: session pid {c.pid}]\n")
        if muted_changed:
            self._print("[muted]\n" if self._muted else "[unmuted]\n")
        if changed:
            self._print(
                f"[voice target: session pid {target.pid}]\n" if target is not None
                else "[no session has voice on]\n"
            )
        return True

    def _note_enqueue_locked(self, shash: str | None) -> int:
        """Session lock held, called with a session's speech about to be enqueued. Returns the
        sequence number the speech watcher uses to know no later enqueue happened."""
        self._enq_seq += 1
        if shash:
            self._speakers.add(shash)
        return self._enq_seq

    def _live_hashes_locked(self) -> frozenset[str]:
        return frozenset(_sid_hash(c.session) for c in self._sessions if c.session)

    def _off_after_locked(self, hashes: set[str], on: bool, recovering: bool) -> dict[str, float]:
        """Session lock held. The OFF set after this change; when recovering, every other
        connected tokened session is added (the lost file may have had them off)."""
        new_off = dict(self._off)
        if recovering:
            for c in self._sessions:
                if c.session:
                    new_off.setdefault(_sid_hash(c.session), time.time())
        for h in hashes:
            if on:
                new_off.pop(h, None)
            else:
                new_off.setdefault(h, time.time())
        return new_off

    def _commit_locked(
        self,
        new_off: dict[str, float],
        matches: list[_Conn],
        on: bool,
        wrote: bool,
        saved: bool | None,
        muted: bool,
    ) -> tuple[bool, "_Conn | None", bool]:
        """Session lock held. Apply a voice change in memory; (target changed, target, cut speech)."""
        self._off = new_off
        if muted != self._muted:
            self._mute_gen += 1
        self._muted = muted
        if wrote:
            self._state_error = False
        if saved is not None:
            self._saved = saved
        stop = False
        if not on and self._speakers & {_sid_hash(c.session) for c in matches if c.session}:
            if self._no_talk_over_gen == self._gen:
                self._no_talk_over_gen = self._gen + 1
            self._gen += 1  # stale reply_done posts for the cut speech are harmless
            self._reply_origin = ""
            self._speakers.clear()  # stop() empties the whole player queue
            stop = True
        changed = self._retarget_locked()
        return changed, self._conn, stop

    # ---- lifecycle -----------------------------------------------------

    def _bind_socket(self, path: Path) -> socket.socket | None:
        """Bind a 0600 listening unix socket at `path`; None (reason printed) if refused.

        Never unlinks anything that is not a socket, refuses while another
        process answers on it, and removes a stale socket file.
        """
        try:
            existing = os.lstat(path)
        except FileNotFoundError:
            existing = None

        if existing is not None and not stat.S_ISSOCK(existing.st_mode):
            # Never unlink something we didn't create.
            print(
                f"eridian: refusing to start -- {path} exists and is not a "
                f"socket (mode {stat.filemode(existing.st_mode)})",
                file=sys.stderr,
            )
            return None

        if existing is not None:
            if _server_answers(path):
                print(f"eridian: another attach is already running at {path}", file=sys.stderr)
                return None
            path.unlink()  # stale socket from a crashed/killed attach

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        try:
            # umask so the socket is never wider than 0600, even briefly; the
            # explicit chmod below stays as the belt to this pair of braces.
            old_umask = os.umask(0o177)
            try:
                server.bind(str(path))
                bound = True
            finally:
                os.umask(old_umask)
            os.chmod(path, 0o600)
            server.listen(_BACKLOG)
            server.settimeout(_POLL_S)
        except OSError as exc:
            server.close()
            if bound:
                with contextlib.suppress(OSError):
                    path.unlink()
            print(f"eridian: cannot listen on {path}: {exc}", file=sys.stderr)
            return None
        return server

    def start(self) -> int:
        """Bind the voice and control sockets and start the threads. 0 ok, 1 refused.

        The control socket is the mute switch, so attach never runs without it:
        if it cannot be bound, the voice socket is released again and start fails.
        """
        server = self._bind_socket(self._socket_path)
        if server is None:
            return 1
        self._server = server
        self._bound = True
        if self._control_path is None:
            self._control_path = paths.control_socket_path()
        control = self._bind_socket(self._control_path)
        if control is None:
            server.close()
            self._server = None
            self._unlink_socket()
            return 1
        self._control_server = control
        self._control_bound = True
        with contextlib.suppress(OSError):
            ident = os.lstat(self._control_path)
            self._control_ident = (ident.st_dev, ident.st_ino)

        for name, target in (
            ("eridian-voice-accept", self._accept_loop),
            ("eridian-voice-speech", self._speech_watcher),
            ("eridian-control-accept", self._control_accept_loop),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._track_thread(thread)
        return 0

    def close(self) -> None:
        with _bounded(self._lock):
            if self._closed:
                return
            self._closed = True
            self._gen += 1
            conns = list(self._conns) + list(self._control_conns)
            self._conn = None
            self._sessions.clear()
        self._speech_q.put(None)
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()
        if self._control_server is not None:
            with contextlib.suppress(OSError):
                self._control_server.close()
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

    def _track_thread(self, thread: threading.Thread) -> None:
        """Remember `thread` for close(); dead reader threads are dropped so connects can't grow the list."""
        with self._threads_lock:
            self._threads[:] = [t for t in self._threads if t.is_alive() or t is thread]
            self._threads.append(thread)

    def _unlink_socket(self) -> None:
        if self._bound:
            self._bound = False
            with contextlib.suppress(OSError):
                self._socket_path.unlink()
        if self._control_bound and self._control_path is not None:
            self._control_bound = False
            with contextlib.suppress(OSError):
                self._control_path.unlink()

    # ---- main thread ---------------------------------------------------

    def _sync_mute(self) -> None:
        """Main thread. Cancel the listener whenever the mute state changed since it last ran.

        Mute: a turn in progress, the wake pre-roll and the talk-over buffers are
        discarded, so nothing captured before the mute is ever transcribed or sent.
        Unmute: the listener restarts from fresh detectors.
        """
        gen = self._mute_gen
        if gen != self._mute_applied_gen:  # a counter, so a quick mute->unmute still cancels
            self._mute_applied_gen = gen
            self._listener.cancel()

    def process_frame(self, frame: bytes) -> None:
        self._sync_mute()
        self._drain_control()
        if self._player.busy:  # kept fresh while muted: the self-wake guard must survive a mute
            self._last_busy = self._clock()
        if self._muted:
            return  # real mute: no wake word, VAD or STT ever sees this frame
        self._sync_mute()  # an unmute landed mid-drain: start from a cancelled listener
        self._listener.talk_over = self._talk_over_mode()
        self._listener.process_frame(frame)

    def _talk_over_mode(self) -> str:
        if not self._talk_over:
            return "off"
        # Session lock, then the player's (documented order).
        with _bounded(self._lock):
            if not self._player.busy:
                return "off"
            return "off" if self._no_talk_over_gen == self._gen else "turn"

    def run(self, frames: Iterable[bytes]) -> None:
        for frame in frames:
            self.process_frame(frame)

    def _drain_control(self) -> None:
        while True:
            try:
                kind, arg = self._control.get_nowait()
            except queue.Empty:
                return
            if kind == "announce":
                self._on_announce(*arg)
                continue
            if kind == "wake_mismatch":
                self._note_spoken(WAKE_MISMATCH_SPEECH)
                self._player.enqueue(WAKE_MISMATCH_SPEECH, own=True)
                continue
            gen = arg
            if (
                kind == "reply_done"
                and self._follow_up_s > 0
                and not self._muted
                and self._listener.state == "IDLE"
                and gen != self._followed_gen
                and self._is_current(gen)
            ):
                self._followed_gen = gen
                self._listener.listen_now(self._follow_up_s)

    def _confirm_is_pending(self) -> bool:
        """Session lock held. Pending only while the guard's challenge could still be alive."""
        return self._confirm_pending and (
            self._clock() - self._confirm_pending_at <= voice_guard.CHALLENGE_TTL_S
        )

    def _announce_ok_locked(self, shash: str) -> bool:
        """Session lock held. The announcing session is still the routing target and not OFF."""
        target = self._conn
        return (
            target is not None
            and bool(target.session)
            and not self._is_off_locked(target)
            and hmac.compare_digest(_sid_hash(target.session), shash)
        )

    def _on_announce(self, event: str, shash: str) -> None:
        """Main thread. `event` is already validated against the enabled templates; `shash` is
        the hash of the session that sent it."""
        with _bounded(self._lock):
            still_ok = self._announce_ok_locked(shash)  # fast path; re-checked at the enqueue
        if not still_ok:  # turned off / retargeted between the hook and now
            self._print(f"[announce skipped: {event} (voice off)]\n")
            return
        if event == "finished":
            with _bounded(self._lock):
                answered, self._answered = self._answered, False
                confirm_pending = self._confirm_is_pending()
            if confirm_pending:
                self._print("[announce skipped: finished (confirm pending)]\n")
                return
            if answered:
                self._print("[announce skipped: finished (already answered)]\n")
                return
        if self._listener.state != "IDLE":
            self._print(f"[announce skipped: {event} (listening)]\n")
            return
        now = self._clock()
        last = self._announce_last.get(event)
        if last is not None and now - last < ANNOUNCE_REPEAT_S:
            self._print(f"[announce skipped: {event} (repeat)]\n")
            return
        self._announce_last[event] = now
        template = ANNOUNCE_TEMPLATES[event]
        with _bounded(self._lock):
            still_ok = self._announce_ok_locked(shash)  # an OFF may have landed since the fast path
            if still_ok:
                self._gen += 1
                gen = self._gen
                self._no_talk_over_gen = gen  # short: heard in full; the wake word still barges in
                self._reply_origin = ""  # an answer landing after an announcement opens no window
                self._spoken_log.append((self._clock(), template))
                seq = self._note_enqueue_locked(shash)
                self._player.enqueue(template)  # queues behind current speech; never stop()
        if not still_ok:
            self._print(f"[announce skipped: {event} (voice off)]\n")
            return
        self._print(f"[announce: {event}]\n")
        self._speech_q.put((gen, False, seq))  # an announcement never opens a window

    def _on_listener_event(self, event: ListenerEvent) -> None:
        if event.kind in ("wake", "talk_over", "follow_up"):
            # One lock section reads the flag and the counter together (every writer holds
            # this lock), so a mute+unmute can never slip between the two reads.
            with _bounded(self._lock):
                muted = self._muted
                self._turn_mute_gen = self._mute_gen  # the turn's mute epoch
            if muted:
                return  # no cue, no barge-in, no turn while muted
        if event.kind == "wake":
            own_voice = (
                self._player.busy
                or (self._clock() - self._last_busy) <= self._self_wake_guard_s
            )
            self._turn_via = "wake_barge_in" if own_voice else "wake"
            self._echo_ref = self._echo_hist = ""
            self._cue("wake")
            self._maybe_barge_in()
        elif event.kind == "talk_over":
            self._turn_via = "talk_over"
            self._echo_hist = self._recent_spoken()
            # Snapshot before the barge-in bumps the generation.
            with _bounded(self._lock):
                self._echo_ref = (
                    self._spoken_text if self._spoken_gen == self._gen else ""
                )
            self._maybe_barge_in()  # no cue: the user is already talking
        elif event.kind == "follow_up":
            self._turn_via, self._echo_ref = "follow_up", ""
            self._echo_hist = self._recent_spoken()
            self._cue("follow_up")
        elif event.kind == "utterance":
            self._on_utterance(event.text or "")
        # wake_no_speech / dropped_empty: nothing to do

    def _turn_muted_locked(self) -> bool:
        """Session lock held. The ONE mute rule for an utterance: muted now, or any mute
        change (even a quick mute->unmute) since the turn opened."""
        return self._muted or self._mute_gen != self._turn_mute_gen

    def _unsent(self, line: str, then: Callable[[], None] | None = None) -> None:
        """A non-send outcome (echo drop, own-reply, too-soon hold). If the turn was muted, only
        `[muted: not sent]` is printed. Accepted: audio captured BEFORE the mute may print in the
        few bytecodes after this check; nothing is sent and no post-mute audio is involved."""
        with _bounded(self._lock):
            muted = self._turn_muted_locked()
        if muted:
            self._print("[muted: not sent]\n")
            return
        self._print(line)
        if then is not None:
            then()

    def _on_utterance(self, text: str) -> None:
        via = self._turn_via
        # 1. Decide everything first: no prints, no enqueues. Lines are buffered.
        if via in ("talk_over", "follow_up") and self._echo_hist and is_echo(text, self._echo_hist):
            # Drop only; never alters a grant decision. A talk-over that
            # already stopped playback stays stopped: the player has no
            # resume, and replaying a half-heard reply is worse than silence.
            self._unsent(f"you [{via}]: {self._redact(text)} [dropped: echo of own speech]\n")
            return
        removed_line = ""
        if via == "talk_over" and self._echo_ref:
            kept = strip_echo(text, self._echo_ref)
            if not re.search(r"[a-z0-9]", kept, re.IGNORECASE):
                self._unsent(f'[ignored as my own reply: "{self._redact(text)}"]\n')
                return
            if kept != text:
                removed_line = f'[removed my own words: "{self._redact(text)}" -> "{self._redact(kept)}"]\n'
                text = kept
        if via == "wake_barge_in":
            with _bounded(self._lock):
                held = self._confirm_is_pending()
            if held:
                # Could be our own voice, so it can never grant; sending it
                # would only use up the challenge. Say why instead.
                def _say_too_soon() -> None:
                    self._note_spoken(TOO_SOON_SPEECH)
                    self._player.enqueue(TOO_SOON_SPEECH, own=True)

                self._unsent(
                    "[too soon after I spoke, so not sent: wait a moment, "
                    "then say the wake word again]\n",
                    _say_too_soon,
                )
                return
        tag = f" [{via}]" if via not in ("", "wake") else ""
        you_line = f"you{tag}: {self._redact(text)}\n"
        payload = {"type": "utterance", "text": text, "via": via}
        # 2. The final check, then the send as the very next statement: no I/O between them.
        with _bounded(self._lock):
            muted = self._turn_muted_locked()
            if not muted:
                self._gen += 1
                conn = self._conn
                any_sessions = bool(self._sessions)
                # Before the send: the reply can race the lock below. The
                # credit is bound only when exactly one turn is outstanding.
                self._outstanding += 1
                self._reply_origin = via if self._outstanding == 1 else ""
                self._reply_origin_at = self._clock()
        if muted:  # send nothing, touch no turn flags, print no transcript
            self._print("[muted: not sent]\n")
            return
        # The one remaining mute window: a mute landing between the in-lock check above and
        # this send (a few bytecodes, no I/O).
        sent = conn is not None and conn.send(payload)
        # 3. State that depends on the outcome is settled BEFORE any print, so a failing
        # print can never strand it; then the transcript prints (a print failure there is dropped silently).
        with _bounded(self._lock):
            if sent:
                self._confirm_pending = False
            else:
                self._outstanding = max(0, self._outstanding - 1)  # a failed send is not a turn
                self._reply_origin = ""
        with contextlib.suppress(Exception):
            if removed_line:
                self._print(removed_line)
            self._print(you_line)
        if sent:
            return
        if conn is None and any_sessions:
            self._print("[no session has voice on]\n")
            self._note_spoken(ALL_OFF_SPEECH)
            self._player.enqueue(ALL_OFF_SPEECH)
            return
        self._print("[no Claude session attached]\n")
        self._note_spoken(NO_SESSION_SPEECH)
        self._player.enqueue(NO_SESSION_SPEECH)

    def _note_spoken(self, text: str) -> None:
        """Remember something eridian said, for the echo text guard. Session lock NOT held."""
        with _bounded(self._lock):
            self._spoken_log.append((self._clock(), text))

    def _recent_spoken(self) -> str:
        """Spoken text to check a no-wake turn against; "" once the guard window has passed."""
        now = self._clock()
        if not self._player.busy and now - self._last_busy > ECHO_GUARD_S:
            return ""
        with _bounded(self._lock):
            return " ".join(t for at, t in self._spoken_log if now - at <= ECHO_HISTORY_S)

    def _maybe_barge_in(self) -> None:
        # Decide AND bump in one locked section (session lock, then the
        # player's); stop() can wait on `say` dying, so it runs outside.
        # `busy` is read best-effort: a missed barge-in is acceptable (the
        # player may flip right after) and stop() is idempotent.
        with _bounded(self._lock):
            if not self._player.busy:
                return
            if self._no_talk_over_gen == self._gen:
                # `busy` can lag the stop; keep talk-over off for the next gen
                self._no_talk_over_gen = self._gen + 1
            self._gen += 1
            self._reply_origin = ""
        self._player.stop()
        self._print("[interrupted]\n")

    # ---- shared helpers ------------------------------------------------

    def _is_current(self, gen: int) -> bool:
        with _bounded(self._lock):
            return gen == self._gen

    def _redact(self, text: str) -> str:
        return voice_guard.redact_safe_phrase(self._home or paths.home_dir(), text)

    def _print(self, text: str) -> None:
        with _bounded(self._print_lock):
            self._out.write(sanitize_terminal(text))
            self._out.flush()

    # ---- accept / reader threads ---------------------------------------

    def _accept_loop(self) -> None:
        server = self._server
        assert server is not None
        errors = setup_failures = 0
        while not self._closed:
            try:
                sock, _ = server.accept()
            except socket.timeout:
                errors = 0
                continue
            except OSError as exc:
                if self._closed:
                    return  # server closed
                errors += 1
                if errors == 1:  # one line per streak
                    self._print(f"[voice] accept error: {type(exc).__name__}: {exc}\n")
                if errors >= ACCEPT_MAX_ERRORS:
                    self._print("[voice socket stopped accepting: no new session can attach]\n")
                    return
                time.sleep(_POLL_S)  # transient (EMFILE...): keep serving, never spin hot
                continue
            errors = 0
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
                    target=self._reader, args=(conn,), name="eridian-voice-reader", daemon=True
                )
                self._track_thread(reader)
                reader.start()
                setup_failures = 0
            except Exception as exc:  # noqa: BLE001 - never let accept die
                setup_failures += 1
                if setup_failures == 1:  # one line per streak of failing connections
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
        deadline = time.monotonic() + self._no_hello_idle_s
        while not self._closed:
            if not conn.hello and time.monotonic() > deadline:
                return  # no valid hello in time: a hard deadline, no line resets it
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

    # ---- control socket ------------------------------------------------
    # Newline-delimited JSON, one response line per request line. It only
    # toggles state and reports it: nothing here can create an utterance, open a
    # listening turn or grant anything. Replies are fixed text, never request content.

    def _control_accept_loop(self) -> None:
        server = self._control_server
        assert server is not None
        errors = setup_failures = 0
        while not self._closed:
            try:
                sock, _ = server.accept()
            except socket.timeout:
                errors = 0
                continue
            except OSError as exc:
                if self._closed:
                    return  # server closed
                errors += 1
                if errors == 1:  # one line per streak
                    self._print(f"[control] accept error: {type(exc).__name__}: {exc}\n")
                if errors >= ACCEPT_MAX_ERRORS:
                    self._control_failed()
                    return
                time.sleep(_POLL_S)  # transient (EMFILE...): keep serving, never spin hot
                continue
            errors = 0
            try:
                sock.settimeout(_IO_TIMEOUT_S)
                conn = _Conn(sock)
                with _bounded(self._lock):
                    if self._closed:
                        conn.close()
                        return
                    over_cap = len(self._control_conns) >= CONTROL_MAX_CONNECTIONS
                    if not over_cap:
                        self._control_conns.add(conn)
                if over_cap:
                    conn.close()
                    continue
                reader = threading.Thread(
                    target=self._control_reader, args=(conn,), name="eridian-control-reader", daemon=True
                )
                self._track_thread(reader)
                reader.start()
                setup_failures = 0
            except Exception as exc:  # noqa: BLE001 - never let accept die
                setup_failures += 1
                if setup_failures == 1:  # one line per streak of failing connections
                    self._print(f"[control] accept error: {type(exc).__name__}: {exc}\n")
                with contextlib.suppress(Exception):
                    sock.close()

    def _control_failed(self) -> None:
        """The control accept loop gave up: the mute switch is gone, so fail toward not listening."""
        with contextlib.suppress(Exception):
            self.set_muted(True)  # a failed write still mutes in memory
        # Remove the dead socket file (only if it is still ours) so a client gets an
        # immediate "no such file" instead of hanging on a listener nobody serves.
        with contextlib.suppress(OSError):
            if self._control_path is not None and self._control_ident is not None:
                st = os.lstat(self._control_path)
                if stat.S_ISSOCK(st.st_mode) and (st.st_dev, st.st_ino) == self._control_ident:
                    self._control_path.unlink()
                    self._control_bound = False
        self._print("[control socket failed; muted]\n")

    def _control_reader(self, conn: _Conn) -> None:
        try:
            self._control_read_lines(conn)
        except Exception as exc:  # noqa: BLE001 - one bad connection must never kill us
            with contextlib.suppress(Exception):
                self._print(f"[control] connection error: {type(exc).__name__}: {exc}\n")
        finally:
            with contextlib.suppress(Exception):
                with _bounded(self._lock):
                    self._control_conns.discard(conn)
            conn.close()

    def _control_read_lines(self, conn: _Conn) -> None:
        """Serve requests on one connection. Slow-drip safe: a line must finish within
        `_control_idle_s` of its first byte, idle between requests is capped at the same
        value, the connection lives at most CONTROL_LIFETIME_S and serves at most
        CONTROL_MAX_REQUESTS requests."""
        buf = b""
        began = idle_since = line_began = time.monotonic()
        served = 0
        while not self._closed:
            now = time.monotonic()
            if now - began > CONTROL_LIFETIME_S:
                return
            if now - (line_began if buf else idle_since) > self._control_idle_s:
                return
            try:
                ready, _, _ = select.select([conn.sock], [], [], _POLL_S)
                if not ready:
                    continue
                data = conn.sock.recv(4096)
            except (OSError, ValueError):
                return
            if not data:
                return
            if not buf:
                line_began = time.monotonic()
            buf += data
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line, buf = buf[:nl], buf[nl + 1 :]
                if len(line) > CONTROL_MAX_LINE_BYTES:
                    return
                if not conn.send(self._control_response(line)):
                    return
                served += 1
                if served >= CONTROL_MAX_REQUESTS:
                    return
                line_began = idle_since = time.monotonic()
            if len(buf) > CONTROL_MAX_LINE_BYTES:
                return  # oversized line with no newline yet

    def _control_response(self, line: bytes) -> dict:
        try:
            return self._control_dispatch(line)
        except TimeoutError:  # a wedged lock (_bounded): answer, don't drop the connection
            return {"ok": False, "error": CONTROL_ERRORS["save"]}

    def _control_dispatch(self, line: bytes) -> dict:
        bad = {"ok": False, "error": CONTROL_BAD_REQUEST}
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return bad
        if not isinstance(msg, dict):
            return bad
        op = msg.get("op")
        if op == "status":
            health = self.voice_state_health()
            return {
                "ok": True,
                "muted": self.is_muted(),
                "state_error": health["state_error"],
                "saved": health["saved"],
                "sessions": self.sessions_status(),
            }
        if op in ("mute", "unmute"):
            return {"ok": True} if self.set_muted(op == "mute") else {"ok": False, "error": CONTROL_ERRORS["save"]}
        if op in ("voice_on", "voice_off"):
            session = msg.get("session")
            if not isinstance(session, str) or not 0 < len(session) <= 64:
                return bad
            on = op == "voice_on"
            if self.set_voice(session, on):
                return {"ok": True}
            with _bounded(self._lock):
                matches = self._matches_locked(session)
            if not matches:
                reason = "no_session"
            elif on and not any(c.session for c in matches):
                reason = "no_token"
            else:
                reason = "save"
            return {"ok": False, "error": CONTROL_ERRORS[reason]}
        return bad

    def _handle_line(self, conn: _Conn, line: bytes) -> None:
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        if kind == "hello":
            if conn.hello:
                self._print(f"[ignored a second hello from session pid {conn.pid}]\n")
                return
            pid, cwd = msg.get("pid"), msg.get("cwd")
            if isinstance(pid, int) and not isinstance(pid, bool) and isinstance(cwd, str):
                session = msg.get("session")
                valid = isinstance(session, str) and TOKEN_RE.fullmatch(session)
                theirs = msg.get("wake")
                theirs = theirs.lower() if isinstance(theirs, str) else wake.DEFAULT_PHRASE
                ours = wake.current().phrase
                if theirs != ours:
                    # Fail closed: this session would tell Claude a different wake word
                    # than the one we listen for. Stay unattached (replies are ignored).
                    now = self._clock()
                    with _bounded(self._lock):
                        last = self._mismatch_spoken_at
                        speak = last is None or now - last >= WAKE_MISMATCH_REPEAT_S
                        if speak:
                            self._mismatch_spoken_at = now
                    if speak:  # printed and spoken on one timer: a channel retrying every few s stays quiet
                        self._print(
                            f"[refused session pid {pid}: it expects wake word {self._redact(theirs[:60])!r} "
                            f"but this voice process listens for {ours!r}; restart one of them]\n"
                        )
                        self._control.put(("wake_mismatch", None))
                    return
                self._attach(conn, pid, cwd, session if valid else "")
        elif kind == "announce":
            self._on_announce_line(msg.get("event"), msg.get("session"))
        elif kind == "reply":
            text = msg.get("text")
            if isinstance(text, str):
                reply_kind = msg.get("kind", "answer")
                reply_kind = reply_kind if isinstance(reply_kind, str) else "?"
                check = msg.get("check")
                if not (
                    reply_kind == "confirm"
                    and isinstance(check, str)
                    and 0 < len(check) <= MAX_CHECK_CHARS
                ):
                    check = None
                self._on_reply(conn, text, reply_kind, check)

    def _on_announce_line(self, event: object, session: object) -> None:
        """Reader thread: validate and hand off; never speaks or touches the player."""
        if not isinstance(event, str) or event not in ANNOUNCE_TEMPLATES:
            return
        if not isinstance(session, str) or not TOKEN_RE.fullmatch(session):
            return
        with _bounded(self._lock):
            conn = self._conn
            expected = conn.session if conn is not None else ""
            if not expected or not hmac.compare_digest(session, expected):
                return
            shash = _sid_hash(expected)
            if event == "finished":
                # The Stop hook fired: Claude's turn ended, so nothing is
                # outstanding and no credit survives -- whether or not the
                # level speaks it, and before any skip rule can drop it.
                self._outstanding, self._reply_origin = 0, ""
        if event in self._announce:
            self._control.put(("announce", (event, shash)))

    def _attach(self, conn: _Conn, pid: int, cwd: str, session: str = "") -> None:
        replaced = None
        with _bounded(self._lock):
            conn.pid, conn.cwd, conn.hello, conn.session = pid, cwd, True, session
            if conn not in self._sessions:
                if session:
                    replaced = next(
                        (c for c in self._sessions if c.session and hmac.compare_digest(c.session, session)),
                        None,
                    )
                if replaced is not None:  # the same session reconnected
                    self._sessions.remove(replaced)
                self._sessions.append(conn)
            self._retarget_locked()
            off = self._is_off_locked(conn)
        if replaced is not None:
            replaced.send({"type": "detached"})
            replaced.close()
        self._print(f"[attached: session pid {pid} in {cwd}]\n")
        if not session:
            self._print(f"[session pid {pid} sent no valid token: voice stays off for it]\n")
        elif off:
            self._print(f"[session pid {pid} has voice off]\n")

    def _drop(self, conn: _Conn) -> None:
        conn.close()
        with _bounded(self._lock):
            self._conns.discard(conn)
            was_current = self._conn is conn
            was_session = conn in self._sessions
            if was_session:
                self._sessions.remove(conn)
            if was_current:
                self._conn = None
                self._reset_turn_flags_locked()
            if was_session:
                self._retarget_locked()
            target = self._conn
            closed = self._closed
        if was_current and not closed:
            self._print("[detached]\n")
            if target is not None:
                self._print(f"[voice target: session pid {target.pid}]\n")

    def _not_spoken_reason_locked(self, conn: _Conn) -> str | None:
        """Session lock held. None if `conn` may speak; "" if it is unknown (stay silent);
        else why its reply is not spoken."""
        if conn is self._conn:
            return None
        if conn not in self._sessions:
            return ""  # a replaced / never-hello'd connection has no voice
        return "is off" if self._is_off_locked(conn) else "is not the voice target"

    def _on_reply(
        self, conn: _Conn, text: str, kind: str = "answer", check: str | None = None
    ) -> None:
        with _bounded(self._lock):
            why = self._not_spoken_reason_locked(conn)
        if why is not None:
            if why:
                self._print(f"[not spoken: session pid {conn.pid} {why}]\n")
            return
        if check is not None:
            self._print(f"eridian: {check}\n")
        self._print(f"claude: {text}\n")
        with _bounded(self._chunk_lock):
            self._chunker.reset()
            sentences = self._chunker.feed(text) + self._chunker.flush()
            check_sentences: list[str] = []
            if check is not None:
                self._chunker.reset()
                check_sentences = self._chunker.feed(check) + self._chunker.flush()
        with _bounded(self._lock):
            # Re-check: an OFF / retarget may have landed since the check above.
            why = self._not_spoken_reason_locked(conn)
            if why is None:
                gen = self._gen
                seq = self._note_enqueue_locked(_sid_hash(conn.session) if conn.session else None)
                if kind != "ack":
                    self._answered = True
                if kind not in ("answer", "ack"):
                    self._confirm_pending = True
                    self._confirm_pending_at = self._clock()
                    self._no_talk_over_gen = gen  # set before enqueuing: never "turn"
                if self._spoken_gen != gen:
                    self._spoken_gen, self._spoken_text = gen, ""
                self._spoken_text += " ".join(check_sentences + sentences) + " "
                self._spoken_log.append((self._clock(), " ".join(check_sentences + sentences)))
                # One-shot credit bound when the utterance was sent: only the first
                # answer to a wake-word turn opens a window. ack keeps the credit;
                # every other kind (answer, confirm, unknown) spends it, fail closed.
                opens_window = (
                    kind == "answer"
                    and self._reply_origin in WAKE_TURN_VIAS
                    and self._clock() - self._reply_origin_at <= REPLY_WINDOW_CREDIT_TTL_S
                )
                if kind != "ack":
                    self._reply_origin = ""
                    self._outstanding = max(0, self._outstanding - 1)
                for sentence in check_sentences:
                    self._player.enqueue(sentence, own=True)
                for sentence in sentences:
                    self._player.enqueue(sentence)
        if why is not None:
            if why:
                self._print(f"[not spoken: session pid {conn.pid} {why}]\n")
            return
        if kind == "confirm":
            self._print(f'[confirmation needed: when I finish, say "{wake.current().phrase}" and the phrase]\n')
        self._speech_q.put((gen, opens_window, seq))

    # ---- speech watcher thread -----------------------------------------

    def _settle(self, seq: int) -> None:
        """The player is idle and `seq` was the latest session enqueue when its item was posted.
        If no later enqueue bumped the counter, nobody has speech queued any more; otherwise
        that later enqueue's own item clears the set."""
        with _bounded(self._lock):
            if seq == self._enq_seq:
                self._speakers.clear()

    def _speech_watcher(self) -> None:
        while True:
            item = self._speech_q.get()
            if item is None:
                return
            gen, opens_window, seq = item
            while not self._closed:
                if self._player.wait_idle(_POLL_S):
                    self._settle(seq)
                    # Only an answer to a wake-word turn opens the open-mic window.
                    if opens_window:
                        self._control.put(("reply_done", gen))
                    break


def self_wake_guard_from_env(environ=os.environ, err: TextIO = sys.stderr) -> float:
    """Guard window from ERIDIAN_SELF_WAKE_GUARD_S (seconds).

    For AirPlay or other high-latency outputs, set 3. Values below the default
    would weaken the guard, so 1.0 is a hard floor; the cap is 10.0.
    """
    name = "ERIDIAN_SELF_WAKE_GUARD_S"
    raw = environ.get(name, "")
    if raw == "":
        return SELF_WAKE_GUARD_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < SELF_WAKE_GUARD_S:
        print(
            f"eridian: ignoring {name}={raw!r}; "
            f"using {SELF_WAKE_GUARD_S} (minimum {SELF_WAKE_GUARD_S})",
            file=err,
        )
        return SELF_WAKE_GUARD_S
    return min(value, _SELF_WAKE_GUARD_MAX_S)


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


def run_attach(
    *,
    no_beep: bool,
    follow_up_s: float,
    no_aec: bool = False,
    tts: str = "kokoro",
    announce: str = "all",
) -> int:
    """The real wiring behind `eridian attach` (real mic; not unit-tested)."""
    # Lazy imports for the same reason as cli._cmd_chat: keep `--help` fast and
    # the heavy audio/model deps out of import time. cli is imported lazily too
    # (it imports this module).
    import time

    from . import cli
    from .stt import ModelNotCachedError, ParakeetSTT

    try:
        stt = ParakeetSTT()
    except ModelNotCachedError as exc:
        print(f"eridian: {exc}", file=sys.stderr)
        return 1

    from pysilero_vad import SileroVoiceActivityDetector

    vad = SileroVoiceActivityDetector()
    cues = {"wake": cli._WAKE_CUE, "follow_up": cli._FOLLOW_UP_CUE}

    try:
        io = cli.make_audio_io(no_aec, tts=tts)
    except cli.OwnVoiceError as exc:
        print(f"eridian: {exc}", file=sys.stderr)
        return 1
    bridge = VoiceBridge(
        wake_factory=cli._make_wake_factory(),
        vad=lambda chunk: vad(chunk),
        stt=stt,
        clock=time.monotonic,
        socket_path=paths.voice_socket_path(),
        vad_reset=vad.reset,
        follow_up_s=follow_up_s,
        cue=None if no_beep else (lambda kind: cli._play_cue(cues[kind])),
        player_factory=io.player_factory,
        talk_over=io.talk_over,
        self_wake_guard_s=self_wake_guard_from_env(),
        announce=ANNOUNCE_LEVELS[announce],
    )
    pid = os.getpid()

    def _on_sigterm(signum: int, frame: object) -> None:
        # `eridian stop` sends SIGTERM; unwind through the finally below so the
        # socket and pidfile are removed.
        raise KeyboardInterrupt

    # Installed before start() so no SIGTERM can land outside the try/finally.
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        if bridge.start() != 0:
            return 1
        print(cli.banner_line(io))
        paths.write_attach_pid(pid)
        print(
            f"eridian attach: listening for '{wake.current().phrase}'. Start a Claude session with "
            "`eridian session` in another terminal. Ctrl-C to quit.",
            flush=True,
        )
        try:
            io.start()
        except Exception as exc:  # noqa: BLE001 - any device/engine failure
            print(cli.AUDIO_START_ERROR.format(exc=exc), file=sys.stderr)
            return 1
        bridge.run(io.frames())
    except KeyboardInterrupt:
        pass
    finally:
        # A second SIGTERM must not interrupt cleanup.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        # Bridge first: that stops the player, which clears the sink.
        bridge.close()
        io.close()
        paths.remove_attach_pid(pid)
    return 0

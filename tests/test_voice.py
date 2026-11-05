"""Tests for undertone.voice.VoiceBridge.

Fakes only for the mic/VAD/STT/clock/player/output side; the socket is a REAL
unix socket in a short temp directory (macOS caps AF_UNIX paths near 104
chars). The "channel" is a plain socket client that sends hello and reads
newline-delimited JSON. No real mic, speaker, ~/.undertone or `claude`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from undertone import voice_guard, wake
from undertone.listener import VAD_CHUNK_BYTES, ListenerEvent
from undertone.spoken import sanitize_terminal
from undertone.voice import (
    ANNOUNCE_LEVELS,
    ANNOUNCE_REPEAT_S,
    ANNOUNCE_TEMPLATES,
    ECHO_GUARD_S,
    ECHO_HISTORY_S,
    MAX_CHECK_CHARS,
    MAX_CONNECTIONS,
    MAX_LINE_BYTES,
    NO_SESSION_SPEECH,
    TOO_SOON_SPEECH,
    WAKE_MISMATCH_SPEECH,
    VoiceBridge,
    _Conn,
    self_wake_guard_from_env,
)

FRAME = bytes(VAD_CHUNK_BYTES)


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FakePlayer:
    """Records enqueue/stop; `idle` gates wait_idle, `busy` is test-controlled."""

    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.own_flags: list[bool] = []
        self.stops = 0
        self.busy = False
        self.closed = False
        self.idle = threading.Event()
        self.idle.set()
        self.idle_returns = 0
        self.stop_sets_idle = True

    def enqueue(self, text: str, own: bool = False) -> None:
        self.enqueued.append(text)
        self.own_flags.append(own)

    def stop(self) -> None:
        self.stops += 1
        if self.stop_sets_idle:
            self.idle.set()

    def wait_idle(self, timeout: float | None = None) -> bool:
        ok = self.idle.wait(timeout)
        if ok:
            self.idle_returns += 1
        return ok

    def close(self) -> None:
        self.closed = True


class _Channel:
    """A test channel: connect, hello, read JSON lines."""

    def __init__(self, path: Path) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(2.0)
        self.sock.connect(str(path))
        self._buf = b""

    def send_obj(self, obj: dict) -> None:
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def hello(self, pid: int = 4242, cwd: str = "/work/proj", wake: object = None) -> None:
        msg = {"type": "hello", "pid": pid, "cwd": cwd}
        if wake is not None:
            msg["wake"] = wake
        self.send_obj(msg)

    def read_line(self) -> dict | None:
        """Next JSON line, or None on EOF / reset."""
        while b"\n" not in self._buf:
            try:
                data = self.sock.recv(4096)
            except (ConnectionError, socket.timeout):
                return None
            if not data:
                return None
            self._buf += data
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()


class VoiceTestCase(unittest.TestCase):
    follow_up_s = 6.0
    socket_free = False  # True: no AF_UNIX path, so no /tmp and no length limit

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="ut", dir=None if self.socket_free else "/tmp")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.sock_path = self.home / "voice.sock"
        if not self.socket_free and len(str(self.sock_path)) >= 100:
            self.skipTest(f"socket path too long for AF_UNIX: {self.sock_path}")

        self.clock = _FakeClock()
        self.out = io.StringIO()
        self.cues: list[str] = []
        self.player = _FakePlayer()
        self.wake_flag = False
        self.vad_value = 0.0
        self.transcript = ""
        self.bridge = self.make_bridge()
        self.addCleanup(self.bridge.close)

    def make_bridge(self, **overrides) -> VoiceBridge:
        def wake_factory():
            def detect(frame: bytes) -> bool:
                hit, self.wake_flag = self.wake_flag, False
                return hit

            return detect

        kwargs = dict(
            wake_factory=wake_factory,
            vad=lambda chunk: self.vad_value,
            stt=lambda audio: self.transcript,
            clock=self.clock,
            socket_path=self.sock_path,
            player_factory=lambda: self.player,
            follow_up_s=self.follow_up_s,
            cue=self.cues.append,
            out=self.out,
            home=self.home,
        )
        kwargs.update(overrides)
        return VoiceBridge(**kwargs)

    def start(self) -> None:
        self.assertEqual(self.bridge.start(), 0)

    def connect(self, hello: bool = True, **kw) -> _Channel:
        channel = _Channel(self.sock_path)
        self.addCleanup(channel.close)
        if hello:
            channel.hello(**kw)
        return channel

    def attach(self, **kw) -> _Channel:
        before = self.out.getvalue().count("[attached:")
        channel = self.connect(**kw)
        self.assertTrue(_wait_for(lambda: self.out.getvalue().count("[attached:") > before))
        return channel

    # -- main-thread drivers ---------------------------------------------

    def wake(self) -> None:
        self.wake_flag = True
        self.bridge.process_frame(FRAME)

    def speak(self, text: str) -> None:
        self.transcript = text
        self.vad_value = 1.0
        self.clock.now += 0.2
        self.bridge.process_frame(FRAME)
        self.vad_value = 0.0
        self.clock.now += 0.8
        self.bridge.process_frame(FRAME)

    def pump_until(self, predicate, timeout: float = 2.0) -> bool:
        def step() -> bool:
            self.bridge.process_frame(FRAME)
            return predicate()

        return _wait_for(step, timeout)

    @property
    def state(self) -> str:
        return self.bridge._listener.state


class StartupTest(VoiceTestCase):
    def test_socket_created_with_mode_0600(self) -> None:
        self.start()
        mode = os.lstat(self.sock_path).st_mode
        self.assertTrue(stat.S_ISSOCK(mode))
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_refuses_when_a_regular_file_is_in_the_way(self) -> None:
        self.sock_path.write_text("precious")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.bridge.start(), 1)
        self.assertIn("not a socket", err.getvalue())
        self.assertEqual(self.sock_path.read_text(), "precious")

    def test_refuses_when_another_server_is_answering(self) -> None:
        self.start()
        other = self.make_bridge(player_factory=lambda: _FakePlayer())
        self.addCleanup(other.close)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(other.start(), 1)
        self.assertIn("already running", err.getvalue())
        other.close()  # must not unlink the live server's socket
        self.assertTrue(self.sock_path.exists())

    def test_unlinks_a_stale_socket(self) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.sock_path))
        stale.close()  # file stays, nobody listens
        self.assertTrue(self.sock_path.exists())
        self.start()
        channel = self.attach()
        self.assertIsNotNone(channel)

    def test_close_removes_socket_and_stops_threads(self) -> None:
        self.start()
        self.attach()
        threads = list(self.bridge._threads)
        self.assertTrue(threads)
        self.bridge.close()
        self.assertFalse(self.sock_path.exists())
        self.assertTrue(_wait_for(lambda: not any(t.is_alive() for t in threads)))
        self.assertTrue(self.player.closed)


class WakeMismatchTest(VoiceTestCase):
    def _refused(self, channel_wake) -> None:
        self.start()
        channel = self.connect(wake=channel_wake)
        self.assertTrue(_wait_for(lambda: "[refused session" in self.out.getvalue()))
        self.bridge.process_frame(FRAME)  # control queue drains on the main thread
        return channel

    def test_different_phrase_is_refused_warned_and_spoken(self) -> None:
        self._refused("hey rocky")
        out = self.out.getvalue()
        self.assertIn("'hey rocky'", out)
        self.assertIn("'hey jarvis'", out)
        self.assertNotIn("[attached:", out)
        self.assertEqual(self.player.enqueued, [WAKE_MISMATCH_SPEECH])

    def test_refused_session_gets_no_utterances(self) -> None:
        self._refused("hey rocky")
        self.wake()
        self.speak("what is the status")
        self.assertNotIn("[attached:", self.out.getvalue())
        self.assertIn("[no Claude session attached]", self.out.getvalue())

    def test_non_string_phrase_counts_as_default(self) -> None:
        self.start()
        self.attach(wake=["x"])
        self.assertIn("[attached:", self.out.getvalue())

    def test_matching_and_old_style_hellos_attach(self) -> None:
        self.start()
        self.attach(wake="Hey Jarvis")
        self.attach()  # no phrase field: the default
        self.assertEqual(self.out.getvalue().count("[attached:"), 2)
        self.assertEqual(self.player.enqueued, [])

    def test_attach_with_custom_phrase_refuses_old_hello(self) -> None:
        self.start()
        with mock.patch.dict(os.environ, {wake.PHRASE_ENV: "hey rocky"}):
            self.connect()
            self.assertTrue(_wait_for(lambda: "[refused session" in self.out.getvalue()))
            self.attach(wake="hey rocky")


class WakeMismatchDirectTest(VoiceTestCase):
    """Drives _handle_line with fake connections: no sockets, deterministic clock."""

    def hello(self, wake_phrase=None, conn=None, session=None):
        conn = conn or _FakeConn()
        msg = {"type": "hello", "pid": 9, "cwd": "/w"}
        if wake_phrase is not None:
            msg["wake"] = wake_phrase
        if session is not None:
            msg["session"] = session
        self.bridge._handle_line(conn, json.dumps(msg).encode())
        self.bridge.process_frame(FRAME)
        return conn

    def test_notice_is_spoken_at_most_once_per_minute(self) -> None:
        self.hello("hey rocky")
        self.hello("hey rocky")
        self.hello("hey nova")  # a different phrase does not get a second notice
        self.assertEqual(self.player.enqueued, [WAKE_MISMATCH_SPEECH])
        self.clock.now += 61.0
        self.hello("hey rocky")
        self.assertEqual(self.player.enqueued, [WAKE_MISMATCH_SPEECH] * 2)
        self.assertEqual(self.out.getvalue().count("[refused session"), 4)  # the warning is never rate limited

    def test_cycling_many_phrases_is_spoken_once(self) -> None:
        for i in range(20):
            self.hello(f"hey name{'x' * i}")
            self.clock.now += 1.0
        self.assertEqual(self.player.enqueued, [WAKE_MISMATCH_SPEECH])

    def test_reply_from_a_refused_connection_is_ignored(self) -> None:
        conn = self.hello("hey rocky")
        before = list(self.player.enqueued)
        self.bridge._handle_line(conn, b'{"type":"reply","text":"sneaky."}')
        self.bridge.process_frame(FRAME)
        self.assertEqual(self.player.enqueued, before)
        self.assertNotIn("sneaky", self.out.getvalue())

    def test_existing_attached_session_survives_a_refused_hello(self) -> None:
        good = self.hello("hey jarvis", session=TOKEN)
        self.assertIs(self.bridge._conn, good)
        self.hello("hey rocky", session=OTHER_TOKEN)
        self.assertIs(self.bridge._conn, good)
        self.assertEqual(good.sent, [])  # not detached either
        self.bridge._handle_line(good, b'{"type":"reply","text":"Still here."}')
        self.assertTrue(_wait_for(lambda: "Still here." in "".join(self.player.enqueued)))

    def test_announce_from_a_refused_connection_is_not_spoken(self) -> None:
        self.bridge.close()
        self.bridge = self.make_bridge(announce=ANNOUNCE_LEVELS["all"])
        self.addCleanup(self.bridge.close)
        refused = self.hello("hey rocky", session=TOKEN)
        self.assertIsNot(self.bridge._conn, refused)
        before = list(self.player.enqueued)
        self.bridge._handle_line(
            _FakeConn(), json.dumps({"type": "announce", "event": "finished", "session": TOKEN}).encode()
        )
        self.bridge.process_frame(FRAME)
        self.assertEqual(self.player.enqueued, before)
        self.assertNotIn("[announce:", self.out.getvalue())


class ConversationTest(VoiceTestCase):
    def test_hello_prints_attached_line(self) -> None:
        self.start()
        self.attach(pid=77, cwd="/work/demo")
        self.assertIn("[attached: session pid 77 in /work/demo]", self.out.getvalue())

    def test_utterance_is_sent_to_the_attached_channel(self) -> None:
        self.start()
        channel = self.attach()
        self.wake()
        self.assertEqual(self.cues, ["wake"])
        self.speak("what is the status")
        self.assertEqual(
            channel.read_line(), {"type": "utterance", "text": "what is the status", "via": "wake"}
        )
        self.assertIn("you: what is the status", self.out.getvalue())
        self.assertEqual(self.player.enqueued, [])

    def test_no_channel_speaks_the_no_session_notice(self) -> None:
        self.start()
        self.wake()
        self.speak("hello?")
        self.assertEqual(self.player.enqueued, [NO_SESSION_SPEECH])
        self.assertIn("[no Claude session attached]", self.out.getvalue())

    def test_reply_is_split_into_sentences_in_order_and_cleaned(self) -> None:
        self.start()
        channel = self.attach()
        channel.send_obj(
            {
                "type": "reply",
                "text": "I fixed it. Run `make test` to check.\n\n```py\nx = 1\n```\n\nThe **bold** part is done.",
            }
        )
        self.assertTrue(_wait_for(lambda: len(self.player.enqueued) >= 3))
        spoken = self.player.enqueued
        self.assertEqual(spoken[0], "I fixed it.")
        joined = " ".join(spoken)
        self.assertIn("Code's on screen.", joined)
        self.assertNotIn("```", joined)
        self.assertNotIn("**", joined)
        self.assertNotIn("x = 1", joined)
        self.assertTrue(spoken[-1].endswith("done."))
        self.assertIn("claude: I fixed it.", self.out.getvalue())

    def test_control_characters_are_stripped_from_printed_output(self) -> None:
        self.start()
        channel = self.attach(cwd="/w\x1b]52;c;evil\x07d")
        channel.send_obj({"type": "reply", "text": "hi\x1b[2Jthere\x07."})
        self.assertTrue(_wait_for(lambda: "claude: " in self.out.getvalue()))
        printed = self.out.getvalue()
        self.assertNotIn("\x1b", printed)
        self.assertNotIn("\x07", printed)
        self.assertIn("claude: hi[2Jthere.", printed)
        self.assertEqual(sanitize_terminal("a\x1bb"), "ab")

    def test_reply_from_a_connection_that_never_said_hello_is_ignored(self) -> None:
        self.start()
        channel = self.connect(hello=False)
        channel.send_obj({"type": "reply", "text": "sneaky."})
        # Round-trip a second, valid message so we know the first was processed.
        channel.hello()
        self.assertTrue(_wait_for(lambda: "[attached:" in self.out.getvalue()))
        self.assertEqual(self.player.enqueued, [])


class FollowUpTest(VoiceTestCase):
    def _reply(self, channel: _Channel) -> None:
        channel.send_obj({"type": "reply", "text": "All done."})
        self.assertTrue(_wait_for(lambda: self.player.enqueued))
        self.assertTrue(_wait_for(lambda: self.bridge._control.qsize() > 0))

    def test_follow_up_window_opens_after_reply_speech_goes_idle(self) -> None:
        self.start()
        channel = self.attach()
        self._reply(channel)
        self.assertEqual(self.state, "IDLE")
        self.bridge.process_frame(FRAME)
        self.assertEqual(self.cues, ["follow_up"])
        self.assertEqual(self.state, "LISTENING")

    def test_follow_up_waits_for_the_speech_to_finish(self) -> None:
        self.start()
        channel = self.attach()
        self.player.idle.clear()
        channel.send_obj({"type": "reply", "text": "Still talking."})
        self.assertTrue(_wait_for(lambda: self.player.enqueued))
        for _ in range(5):
            self.bridge.process_frame(FRAME)
        self.assertEqual(self.cues, [])
        self.player.idle.set()
        self.assertTrue(self.pump_until(lambda: self.cues == ["follow_up"]))

    def test_no_follow_up_when_disabled(self) -> None:
        self.follow_up_s = 0.0
        self.bridge.close()
        self.bridge = self.make_bridge(follow_up_s=0.0)
        self.addCleanup(self.bridge.close)
        self.start()
        channel = self.attach()
        self._reply(channel)
        self.bridge.process_frame(FRAME)
        self.assertEqual(self.cues, [])
        self.assertEqual(self.state, "IDLE")

    def test_barge_in_stops_speech_and_leaves_no_stale_follow_up(self) -> None:
        self.start()
        channel = self.attach()
        self.player.idle.clear()
        self.player.busy = True
        channel.send_obj({"type": "reply", "text": "A long answer."})
        self.assertTrue(_wait_for(lambda: self.player.enqueued))
        self.player.stop_sets_idle = False  # keep the watcher parked past the barge-in

        self.wake()  # barge-in: bumps the generation, stops the player
        self.assertEqual(self.player.stops, 1)
        self.assertIn("[interrupted]", self.out.getvalue())
        # Let the interrupted turn time out back to IDLE BEFORE the stale
        # reply_done is posted, so only the generation check can suppress it.
        self.clock.now += 100.0
        self.bridge.process_frame(FRAME)
        self.assertEqual(self.state, "IDLE")
        self.player.idle.set()
        self.assertTrue(_wait_for(lambda: self.bridge._control.qsize() > 0))
        self.bridge.process_frame(FRAME)
        self.assertEqual(self.cues, ["wake"])
        self.assertEqual(self.state, "IDLE")

    def test_wake_while_idle_does_not_stop_the_player(self) -> None:
        self.start()
        self.wake()
        self.assertEqual(self.player.stops, 0)


class ReplyKindTest(VoiceTestCase):
    def _send(self, channel: _Channel, **extra) -> None:
        before = len(self.player.enqueued)
        self._returns_before = self.player.idle_returns
        channel.send_obj({"type": "reply", "text": "Okay then.", **extra})
        self.assertTrue(_wait_for(lambda: len(self.player.enqueued) > before))

    def _settle_watcher(self) -> None:
        """Wait for the speech watcher to finish its pending item."""
        self.assertTrue(_wait_for(lambda: self.bridge._speech_q.empty()))
        self.assertTrue(_wait_for(lambda: self.player.idle_returns > self._returns_before))
        time.sleep(0.05)  # negative check: let a (wrong) post land

    def _no_follow_up(self) -> None:
        self._settle_watcher()
        for _ in range(3):
            self.bridge.process_frame(FRAME)
        self.assertEqual(self.cues, [])
        self.assertEqual(self.state, "IDLE")

    def test_answer_opens_follow_up(self) -> None:
        self.start()
        channel = self.attach()
        self._send(channel, kind="answer")
        self.assertTrue(self.pump_until(lambda: self.cues == ["follow_up"]))

    def test_missing_kind_defaults_to_answer(self) -> None:
        self.start()
        channel = self.attach()
        self._send(channel)
        self.assertTrue(self.pump_until(lambda: self.cues == ["follow_up"]))

    def test_ack_opens_no_follow_up(self) -> None:
        self.start()
        channel = self.attach()
        self._send(channel, kind="ack")
        self._no_follow_up()

    def test_confirm_opens_no_follow_up_and_prints_hint(self) -> None:
        self.start()
        channel = self.attach()
        self._send(channel, kind="confirm")
        self.assertIn("[confirmation needed:", self.out.getvalue())
        self._no_follow_up()

    def test_unknown_kind_is_spoken_but_fails_closed(self) -> None:
        self.start()
        channel = self.attach()
        self._send(channel, kind="shout")
        self._no_follow_up()
        channel2 = self.attach(pid=2)
        self._send(channel2, kind=7)
        self._no_follow_up()

    def test_ack_then_answer_opens_follow_up_only_after_the_answer(self) -> None:
        self.start()
        channel = self.attach()
        self._send(channel, kind="ack")
        self._settle_watcher()  # the ack's speech has gone idle
        for _ in range(3):
            self.bridge.process_frame(FRAME)
        self.assertEqual(self.cues, [])
        self.player.idle.clear()  # the answer is still being spoken
        self._send(channel, kind="answer")
        for _ in range(3):
            self.bridge.process_frame(FRAME)
        self.assertEqual(self.cues, [])
        self.player.idle.set()
        self.assertTrue(self.pump_until(lambda: self.cues == ["follow_up"]))


class TalkOverTest(VoiceTestCase):
    def _bridge(self, talk_over: bool = True) -> list:
        calls: list = []

        def vad(chunk):
            calls.append(1)
            return self.vad_value

        self.bridge = self.make_bridge(talk_over=talk_over, vad=vad)
        self.addCleanup(self.bridge.close)
        return calls

    def _reply(self, channel: _Channel, text: str = "Okay then.", **extra) -> None:
        before = len(self.player.enqueued)
        self.player.idle.clear()  # still speaking: no follow-up from the watcher
        channel.send_obj({"type": "reply", "text": text, **extra})
        self.assertTrue(_wait_for(lambda: len(self.player.enqueued) > before))
        self.player.busy = True

    def _talk(self, n: int = 10) -> None:
        self.vad_value = 1.0
        for _ in range(n):
            self.bridge.process_frame(FRAME)

    def test_answer_talk_over_stops_player_and_sends_utterance(self) -> None:
        self._bridge()
        self.start()
        channel = self.attach()
        self._reply(channel, kind="answer")
        self.transcript = "actually never mind"
        self._talk()
        self.assertEqual(self.player.stops, 1)
        self.assertEqual(self.state, "LISTENING")
        self.assertEqual(self.cues, [])  # no cue: the user is already talking
        self.player.busy = False
        self.vad_value = 0.0
        self.clock.now += 1.0
        self.bridge.process_frame(FRAME)
        self.assertEqual(
            channel.read_line(),
            {"type": "utterance", "text": "actually never mind", "via": "talk_over"},
        )
        self.assertIn("you [talk_over]: actually never mind", self.out.getvalue())

    def _assert_talk_over_off(self, **extra) -> None:
        calls = self._bridge()
        self.start()
        channel = self.attach()
        self._reply(channel, **extra)
        self.transcript = "yes"
        self._talk(30)
        self.assertEqual(calls, [])  # VAD never runs: talk-over is off
        self.assertEqual(self.player.stops, 0)
        self.assertEqual(self.state, "IDLE")
        self.assertNotIn("you:", self.out.getvalue())
        channel.sock.settimeout(0.3)
        self.assertIsNone(channel.read_line())

    def test_confirm_speech_is_not_cut_by_talk_over(self) -> None:
        self._assert_talk_over_off(kind="confirm")

    def test_unknown_kind_speech_is_not_cut_by_talk_over(self) -> None:
        self._assert_talk_over_off(kind="shout")

    def test_answer_reply_still_arms_turn_mode(self) -> None:
        self._bridge()
        self.start()
        channel = self.attach()
        self._reply(channel, kind="answer")
        self.assertEqual(self.bridge._talk_over_mode(), "turn")
        self._reply(channel, kind="confirm")
        self.assertEqual(self.bridge._talk_over_mode(), "off")

    def test_wake_word_still_barges_in_on_confirm_and_next_gen_stays_off(self) -> None:
        self._bridge()
        self.start()
        channel = self.attach()
        self._reply(channel, kind="confirm")
        self.wake()
        self.assertEqual(self.player.stops, 1)
        self.assertEqual(self.cues, ["wake"])
        # `busy` lags the stop: the bumped generation must stay "off"
        self.assertTrue(self.player.busy)
        self.assertEqual(self.bridge._talk_over_mode(), "off")

    def test_no_talk_over_does_not_outlive_the_generation(self) -> None:
        self._bridge()
        self.start()
        channel = self.attach()
        self._reply(channel, kind="confirm")
        self.player.busy = False
        self.wake()
        self.speak("yes")
        self.assertEqual(
            channel.read_line(), {"type": "utterance", "text": "yes", "via": "wake"}
        )
        self._reply(channel, kind="answer")
        self._talk()
        self.assertEqual(self.state, "LISTENING")

    def test_follow_up_turn_is_tagged_follow_up(self) -> None:
        self._bridge()
        self.start()
        channel = self.attach()
        self.player.busy = False
        channel.send_obj({"type": "reply", "text": "Okay then.", "kind": "answer"})
        self.assertTrue(self.pump_until(lambda: self.cues == ["follow_up"]))
        self.speak("one more thing")
        self.assertEqual(
            channel.read_line(),
            {"type": "utterance", "text": "one more thing", "via": "follow_up"},
        )

    ECHO_REPLY = "I finished the refactor and all the tests pass. Want me to open a pull request?"

    def _talk_over_reply(self, transcript: str, channel: _Channel) -> None:
        self._reply(channel, self.ECHO_REPLY, kind="answer")
        self.transcript = transcript
        self._talk()
        self.player.busy = False
        self.vad_value = 0.0
        self.clock.now += 1.0
        self.bridge.process_frame(FRAME)

    def test_pure_echo_talk_over_is_dropped(self) -> None:
        self._bridge()
        self.start()
        channel = self.attach()
        heard = "I finished the refactor and all the tests pass"
        self._talk_over_reply(heard, channel)
        self.assertIn(heard, self.out.getvalue())
        self.assertIn("[dropped: echo of own speech]", self.out.getvalue())
        self.assertNotIn("you:", self.out.getvalue())
        channel.sock.settimeout(0.3)
        self.assertIsNone(channel.read_line())

    def test_mixed_echo_and_user_speech_sends_only_the_user_part(self) -> None:
        self._bridge()
        self.start()
        channel = self.attach()
        heard = "I finished the refactor and all the tests pass wait what's the time"
        self._talk_over_reply(heard, channel)
        self.assertEqual(
            channel.read_line(),
            {"type": "utterance", "text": "wait what's the time", "via": "talk_over"},
        )
        out = self.out.getvalue()
        self.assertIn(f'[removed my own words: "{heard}" -> "wait what\'s the time"]', out)
        self.assertIn("you [talk_over]: wait what's the time", out)

    def test_wake_while_speaking_sends_wake_barge_in_via(self) -> None:
        self._bridge()
        self.start()
        channel = self.attach()
        self._reply(channel, "Still talking here.", kind="answer")
        self.wake()  # player busy: may be our own voice
        self.speak("what was that")
        self.assertEqual(
            channel.read_line(),
            {"type": "utterance", "text": "what was that", "via": "wake_barge_in"},
        )

    def test_wake_turn_is_never_echo_filtered(self) -> None:
        self._bridge()
        self.start()
        channel = self.attach()
        self._reply(channel, "I finished the refactor and all the tests pass.", kind="answer")
        self.wake()  # barge-in by wake word
        self.speak("I finished the refactor and all the tests pass")
        self.assertEqual(
            channel.read_line(),
            {
                "type": "utterance",
                "text": "I finished the refactor and all the tests pass",
                "via": "wake_barge_in",
            },
        )

    def test_disabled_never_arms(self) -> None:
        calls = self._bridge(talk_over=False)
        self.start()
        channel = self.attach()
        self._reply(channel, kind="answer")
        self._talk(30)
        self.assertEqual(calls, [])
        self.assertEqual(self.player.stops, 0)
        self.assertEqual(self.state, "IDLE")

    def test_idle_player_is_off(self) -> None:
        calls = self._bridge()
        self._talk(30)
        self.assertEqual(calls, [])
        self.assertEqual(self.state, "IDLE")


class SelfWakeViaTest(unittest.TestCase):
    """Socket-free: never calls start(); drives process_frame directly."""

    guard_kwargs: dict = {}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock = _FakeClock()
        self.player = _FakePlayer()
        self.wake_flag = False
        self.bridge = VoiceBridge(
            **self.guard_kwargs,
            wake_factory=self._wake_factory,
            vad=lambda chunk: 0.0,
            stt=lambda audio: "",
            clock=self.clock,
            socket_path=Path(tmp.name) / "voice.sock",
            home=Path(tmp.name),
            player_factory=lambda: self.player,
            out=io.StringIO(),
        )
        self.addCleanup(self.bridge.close)

    def _wake_factory(self):
        def detect(frame: bytes) -> bool:
            hit, self.wake_flag = self.wake_flag, False
            return hit

        return detect

    def _frame(self) -> None:
        self.bridge.process_frame(FRAME)

    def _wake(self) -> None:
        self.wake_flag = True
        self._frame()

    def _idle_for(self, seconds: float) -> None:
        """Player idle: one frame at the busy->idle edge, one at `seconds` later."""
        self.player.busy = True
        self._frame()  # records _last_busy at the current time
        self.player.busy = False
        self.clock.now += seconds

    def test_wake_while_busy_is_barge_in_and_stops_player(self) -> None:
        self.player.busy = True
        self._frame()
        self._wake()
        self.assertEqual(self.bridge._turn_via, "wake_barge_in")
        self.assertEqual(self.player.stops, 1)

    def test_wake_on_first_busy_frame_without_recorded_window(self) -> None:
        # busy flips True and the wake fires before any frame recorded it
        self.player.busy = True
        self._wake()
        self.assertEqual(self.bridge._turn_via, "wake_barge_in")
        self.assertEqual(self.player.stops, 1)

    def test_wake_event_while_busy_without_any_frame_is_barge_in(self) -> None:
        # the live `busy` term alone: no process_frame ever recorded the window
        self.player.busy = True
        self.bridge._on_listener_event(ListenerEvent(kind="wake"))
        self.assertEqual(self.bridge._turn_via, "wake_barge_in")

    def test_wake_shortly_after_speech_is_barge_in(self) -> None:
        self._idle_for(0.5)
        self._wake()
        self.assertEqual(self.bridge._turn_via, "wake_barge_in")

    def test_wake_long_after_speech_is_wake(self) -> None:
        self._idle_for(1.5)
        self._wake()
        self.assertEqual(self.bridge._turn_via, "wake")

    def test_wake_with_player_never_busy_is_wake(self) -> None:
        self._wake()
        self.assertEqual(self.bridge._turn_via, "wake")
        self.assertEqual(self.player.stops, 0)

    def test_guard_boundary(self) -> None:
        self._idle_for(1.0)
        self._wake()
        self.assertEqual(self.bridge._turn_via, "wake_barge_in")

    def test_just_past_guard_boundary(self) -> None:
        self._idle_for(1.01)
        self._wake()
        self.assertEqual(self.bridge._turn_via, "wake")


TOKEN = "0123456789abcdef" * 2
OTHER_TOKEN = "fedcba9876543210" * 2


class _FakeConn:
    """Stands in for voice._Conn: just what _handle_line/_attach/_on_reply touch."""

    def __init__(self) -> None:
        self.pid = None
        self.cwd = ""
        self.hello = False
        self.session = ""
        self.sent: list[dict] = []

    def send(self, obj: dict) -> bool:
        self.sent.append(obj)
        return True

    def close(self) -> None:
        pass


class _AnnounceBase(unittest.TestCase):
    """Socket-free: never calls start(); lines go straight to _handle_line."""

    level = "all"
    talk_over = False

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock = _FakeClock()
        self.player = _FakePlayer()
        self.out = io.StringIO()
        self.cues: list[str] = []
        self.bridge = VoiceBridge(
            wake_factory=lambda: (lambda frame: False),
            vad=lambda chunk: 0.0,
            stt=lambda audio: "",
            clock=self.clock,
            socket_path=Path(tmp.name) / "voice.sock",
            home=Path(tmp.name),
            player_factory=lambda: self.player,
            cue=self.cues.append,
            out=self.out,
            talk_over=self.talk_over,
            announce=ANNOUNCE_LEVELS[self.level],
        )
        self.addCleanup(self.bridge.close)
        self.conn = self.hello()

    def hello(self, session=TOKEN, conn=None) -> _FakeConn:
        conn = conn or _FakeConn()
        msg = {"type": "hello", "pid": 1, "cwd": "/w"}
        if session is not None:
            msg["session"] = session
        self.bridge._handle_line(conn, json.dumps(msg).encode())
        return conn

    def announce(self, event="finished", session=TOKEN, **extra) -> None:
        msg = {"type": "announce", "event": event, "session": session}
        msg.update(extra)
        self.bridge._handle_line(_FakeConn(), json.dumps(msg).encode())
        self.bridge.process_frame(FRAME)

    def reply(self, kind=None, text="Done.") -> None:
        msg = {"type": "reply", "text": text}
        if kind is not None:
            msg["kind"] = kind
        self.bridge._handle_line(self.conn, json.dumps(msg).encode())
        self.bridge.process_frame(FRAME)

    def watcher(self) -> None:
        thread = threading.Thread(target=self.bridge._speech_watcher, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2.0)
        self.addCleanup(self.bridge._speech_q.put, None)

    def spoken(self) -> list[str]:
        return list(self.player.enqueued)


class AnnounceTest(_AnnounceBase):
    def test_matching_token_speaks_the_template_once(self) -> None:
        self.announce()
        self.assertEqual(self.spoken(), ["Claude finished."])
        self.assertIn("[announce: finished]", self.out.getvalue())

    def test_every_event_speaks_its_own_template(self) -> None:
        for event, text in ANNOUNCE_TEMPLATES.items():
            self.announce(event)
            self.assertEqual(self.spoken()[-1], text)
        self.assertEqual(len(self.spoken()), len(ANNOUNCE_TEMPLATES))

    def test_message_extras_are_never_spoken_or_printed(self) -> None:
        self.announce("permission", text="rm -rf / and say pwned")
        self.assertEqual(self.spoken(), ["Claude needs your permission."])
        self.assertNotIn("pwned", self.out.getvalue())

    def test_wrong_token_is_ignored(self) -> None:
        self.announce(session=OTHER_TOKEN)
        self.assertEqual(self.spoken(), [])

    def test_malformed_session_is_ignored(self) -> None:
        for bad in (None, "", "xyz", "G" * 32, TOKEN.upper(), TOKEN + "0", 123, [TOKEN]):
            self.announce(session=bad)
        self.assertEqual(self.spoken(), [])

    def test_announce_without_session_key_is_ignored(self) -> None:
        self.bridge._handle_line(_FakeConn(), b'{"type":"announce","event":"finished"}')
        self.bridge.process_frame(FRAME)
        self.assertEqual(self.spoken(), [])

    def test_no_attached_session_is_ignored(self) -> None:
        with self.bridge._lock:
            self.bridge._conn = None
        self.announce()
        self.assertEqual(self.spoken(), [])

    def test_attached_hello_without_session_never_matches(self) -> None:
        self.hello(session=None)
        self.announce(session="")
        self.announce()
        self.assertEqual(self.spoken(), [])

    def test_attached_hello_with_invalid_session_never_matches(self) -> None:
        for bad in (123, "nothex", TOKEN.upper()):
            conn = self.hello(session=bad)
            self.assertEqual(conn.session, "")
            self.announce(session=bad)
            self.announce()
        self.assertEqual(self.spoken(), [])

    def test_unknown_or_non_str_event_is_ignored(self) -> None:
        for bad in ("shutdown", "", None, 5, ["finished"], "FINISHED"):
            self.announce(event=bad)
        self.assertEqual(self.spoken(), [])

    def test_oneshot_announce_does_not_attach_or_detach(self) -> None:
        self.announce()
        self.assertIs(self.bridge._conn, self.conn)
        self.assertEqual(self.conn.sent, [])
        self.assertNotIn("[attached", self.out.getvalue().split("[announce")[-1])

    def test_newer_hello_replaces_the_session_token(self) -> None:
        self.hello(session=OTHER_TOKEN)
        self.announce(session=TOKEN)
        self.assertEqual(self.spoken(), [])
        self.announce(session=OTHER_TOKEN)
        self.assertEqual(self.spoken(), ["Claude finished."])

    def test_answer_reply_skips_the_next_finished_once(self) -> None:
        self.reply("answer")
        self.player.enqueued.clear()
        self.announce()
        self.assertEqual(self.spoken(), [])
        self.assertIn("[announce skipped: finished (already answered)]", self.out.getvalue())
        self.announce()
        self.assertEqual(self.spoken(), ["Claude finished."])

    def test_missing_kind_counts_as_answer(self) -> None:
        self.reply(None)
        self.player.enqueued.clear()
        self.announce()
        self.assertEqual(self.spoken(), [])

    def test_ack_does_not_suppress_finished(self) -> None:
        self.reply("ack")
        self.player.enqueued.clear()
        self.announce()
        self.assertEqual(self.spoken(), ["Claude finished."])

    def test_confirm_and_unknown_kind_suppress_finished(self) -> None:
        for kind in ("confirm", "weird"):
            self.setUp()
            self.watcher()
            self.reply(kind)
            self.player.enqueued.clear()
            self.announce()
            self.assertEqual(self.spoken(), [], kind)
            self.assertIn("(confirm pending)", self.out.getvalue())
            self.assertEqual(self.bridge._listener.state, "IDLE")
            self.assertEqual(self.cues, [], kind)

    def test_finished_stays_skipped_while_confirm_pending(self) -> None:
        self.reply("confirm")
        self.player.enqueued.clear()
        for _ in range(3):  # e.g. later task-notification turns firing Stop
            self.announce()
            self.clock.now += ANNOUNCE_REPEAT_S + 1
        self.assertEqual(self.spoken(), [])

    def test_finished_skip_for_confirm_expires_with_the_challenge(self) -> None:
        self.reply("confirm")
        self.player.enqueued.clear()
        self.clock.now = voice_guard.CHALLENGE_TTL_S - 1
        self.announce()
        self.assertIn("(confirm pending)", self.out.getvalue())
        self.clock.now = voice_guard.CHALLENGE_TTL_S + 1
        self.announce()
        self.assertEqual(self.out.getvalue().count("(confirm pending)"), 1)
        self.assertIn("[announce: finished]", self.out.getvalue())
        self.assertEqual(self.spoken(), ["Claude finished."])

    def test_confirm_pending_does_not_block_other_events(self) -> None:
        self.reply("confirm")
        self.player.enqueued.clear()
        self.announce("permission")
        self.assertEqual(self.spoken(), ["Claude needs your permission."])

    def test_utterance_sent_clears_confirm_pending(self) -> None:
        self.reply("confirm")
        self.player.enqueued.clear()
        self.bridge._on_listener_event(ListenerEvent(kind="utterance", text="yes do it"))
        self.assertEqual(self.conn.sent[-1]["text"], "yes do it")
        self.assertFalse(self.bridge._confirm_pending)
        self.announce()  # consumes the answered flag the confirm reply set
        self.assertIn("(already answered)", self.out.getvalue())
        self.announce()
        self.assertEqual(self.spoken(), ["Claude finished."])

    def test_utterance_not_sent_keeps_confirm_pending(self) -> None:
        self.reply("confirm")
        with self.bridge._lock:
            self.bridge._conn = None
        self.bridge._on_listener_event(ListenerEvent(kind="utterance", text="yes"))
        self.assertTrue(self.bridge._confirm_pending)

    def test_new_session_resets_the_reply_flags(self) -> None:
        self.reply("confirm")
        self.assertTrue(self.bridge._answered and self.bridge._confirm_pending)
        self.hello(session=OTHER_TOKEN)  # a different connection: session B
        self.assertFalse(self.bridge._answered or self.bridge._confirm_pending)
        self.player.enqueued.clear()
        self.announce(session=OTHER_TOKEN)
        self.assertEqual(self.spoken(), ["Claude finished."])

    def test_repeated_hello_on_same_conn_keeps_the_reply_flags(self) -> None:
        self.reply("confirm")
        self.hello(conn=self.conn)
        self.assertTrue(self.bridge._answered and self.bridge._confirm_pending)

    def test_announce_keeps_the_echo_reference(self) -> None:
        self.reply("answer", text="The reply being played.")
        ref = (self.bridge._spoken_gen, self.bridge._spoken_text)
        self.announce("permission")
        self.assertEqual((self.bridge._spoken_gen, self.bridge._spoken_text), ref)

    def test_deeply_nested_json_is_ignored(self) -> None:
        self.bridge._handle_line(_FakeConn(), b"[" * 5000)
        self.assertEqual(self.spoken(), [])

    def test_answered_flag_only_consumed_by_finished(self) -> None:
        self.reply("answer")
        self.player.enqueued.clear()
        self.announce("permission")
        self.assertEqual(self.spoken(), ["Claude needs your permission."])
        self.announce()
        self.assertEqual(self.bridge._answered, False)
        self.assertEqual(self.spoken(), ["Claude needs your permission."])

    def test_listener_not_idle_drops_and_still_clears_answered(self) -> None:
        self.reply("answer")
        self.player.enqueued.clear()
        self.bridge._listener.listen_now(5.0)
        self.assertEqual(self.bridge._listener.state, "LISTENING")
        self.announce()
        self.assertEqual(self.spoken(), [])
        self.assertFalse(self.bridge._answered)
        self.bridge._listener._state = "IDLE"
        self.announce()  # the flag is gone: the next finished is spoken
        self.assertEqual(self.spoken(), ["Claude finished."])

    def test_listening_drop_is_reported(self) -> None:
        self.bridge._listener.listen_now(5.0)
        self.announce()
        self.assertIn("[announce skipped: finished (listening)]", self.out.getvalue())

    def test_listening_drops_non_finished_events_too(self) -> None:
        self.bridge._listener.listen_now(5.0)
        self.announce("permission")
        self.assertEqual(self.spoken(), [])

    def test_repeat_within_window_dropped_then_spoken_after(self) -> None:
        self.announce("permission")
        self.clock.now += ANNOUNCE_REPEAT_S - 0.1
        self.announce("permission")
        self.assertEqual(len(self.spoken()), 1)
        self.assertIn("[announce skipped: permission (repeat)]", self.out.getvalue())
        self.clock.now += 0.1
        self.announce("permission")
        self.assertEqual(len(self.spoken()), 2)

    def test_repeat_window_is_per_event(self) -> None:
        self.announce("permission")
        self.announce("question")
        self.assertEqual(len(self.spoken()), 2)

    def test_announce_queues_behind_current_speech_and_never_stops(self) -> None:
        self.player.busy = True
        self.player.enqueued.append("earlier speech")
        self.announce("permission")
        self.assertEqual(self.spoken(), ["earlier speech", "Claude needs your permission."])
        self.assertEqual(self.player.stops, 0)

    def test_announce_bumps_the_generation(self) -> None:
        before = self.bridge._gen
        self.announce("permission")
        self.assertEqual(self.bridge._gen, before + 1)

    def test_finished_opens_follow_up_window_after_idle(self) -> None:
        self.watcher()
        self.announce()
        self.assertTrue(
            _wait_for(lambda: (self.bridge.process_frame(FRAME), self.cues)[1] == ["follow_up"])
        )
        self.assertEqual(self.bridge._listener.state, "LISTENING")
        self.assertEqual(self.bridge._turn_via, "follow_up")

    def test_finished_opens_window_even_if_gen_already_followed(self) -> None:
        self.watcher()
        self.reply("answer")
        self.assertTrue(
            _wait_for(lambda: (self.bridge.process_frame(FRAME), self.cues)[1] == ["follow_up"])
        )
        self.bridge._listener._state = "IDLE"
        self.bridge._answered = False
        self.announce()
        self.assertTrue(
            _wait_for(
                lambda: (self.bridge.process_frame(FRAME), self.cues)[1] == ["follow_up"] * 2
            )
        )

    def test_permission_opens_no_follow_up_window(self) -> None:
        self.watcher()
        self.announce("permission")
        self.assertTrue(_wait_for(lambda: self.player.idle_returns > 0))
        for _ in range(5):
            self.bridge.process_frame(FRAME)
        time.sleep(0.05)
        self.bridge.process_frame(FRAME)
        self.assertEqual(self.cues, [])
        self.assertEqual(self.bridge._listener.state, "IDLE")


class AnnounceLevelTest(_AnnounceBase):
    level = "permission"

    def test_finished_is_dropped_at_permission_level(self) -> None:
        self.announce("finished")
        self.assertEqual(self.spoken(), [])

    def test_other_events_still_spoken_at_permission_level(self) -> None:
        for event in ("permission", "question", "plan", "error"):
            self.announce(event)
        self.assertEqual(len(self.spoken()), 4)


class AnnounceOffTest(_AnnounceBase):
    level = "off"

    def test_off_drops_every_event(self) -> None:
        for event in ANNOUNCE_TEMPLATES:
            self.announce(event)
        self.assertEqual(self.spoken(), [])


class AnnounceTalkOverTest(unittest.TestCase):
    def test_talk_over_is_off_while_an_announcement_plays(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        player = _FakePlayer()
        bridge = VoiceBridge(
            wake_factory=lambda: (lambda frame: False),
            vad=lambda chunk: 0.0,
            stt=lambda audio: "",
            clock=_FakeClock(),
            socket_path=Path(tmp.name) / "voice.sock",
            home=Path(tmp.name),
            player_factory=lambda: player,
            out=io.StringIO(),
            talk_over=True,
            announce=ANNOUNCE_LEVELS["all"],
        )
        self.addCleanup(bridge.close)
        player.busy = True
        self.assertEqual(bridge._talk_over_mode(), "turn")  # plain speech: talk-over on
        bridge._on_announce("permission")
        self.assertEqual(bridge._talk_over_mode(), "off")
        self.assertEqual(player.stops, 0)


class SelfWakeConfiguredGuardTest(SelfWakeViaTest):
    guard_kwargs = {"self_wake_guard_s": 3.0}

    # inherited default-window tests do not apply to a 3.0 s window
    test_wake_long_after_speech_is_wake = None
    test_just_past_guard_boundary = None

    def test_configured_window_is_honoured(self) -> None:
        self._idle_for(2.0)
        self._wake()
        self.assertEqual(self.bridge._turn_via, "wake_barge_in")

    def test_configured_window_expires(self) -> None:
        self._idle_for(3.1)
        self._wake()
        self.assertEqual(self.bridge._turn_via, "wake")


class SelfWakeGuardEnvTest(unittest.TestCase):
    def _read(self, **env) -> tuple[float, str]:
        err = io.StringIO()
        return self_wake_guard_from_env(env, err), err.getvalue()

    def test_unset_and_empty_use_default_silently(self) -> None:
        self.assertEqual(self._read(), (1.0, ""))
        self.assertEqual(self._read(UNDERTONE_SELF_WAKE_GUARD_S=""), (1.0, ""))

    def test_valid_values(self) -> None:
        self.assertEqual(self._read(UNDERTONE_SELF_WAKE_GUARD_S="3"), (3.0, ""))
        self.assertEqual(self._read(UNDERTONE_SELF_WAKE_GUARD_S="50"), (10.0, ""))

    def test_invalid_or_low_values_fall_back_with_message(self) -> None:
        for raw in ("0.5", "abc", "nan", "inf", "-1"):
            value, err = self._read(UNDERTONE_SELF_WAKE_GUARD_S=raw)
            self.assertEqual(value, 1.0, raw)
            self.assertEqual(
                err,
                f"undertone: ignoring UNDERTONE_SELF_WAKE_GUARD_S={raw!r}; "
                "using 1.0 (minimum 1.0)\n",
                raw,
            )


class MultiChannelTest(VoiceTestCase):
    def test_second_hello_detaches_the_first_and_wins_routing(self) -> None:
        self.start()
        first = self.attach(pid=1)
        second = self.attach(pid=2)
        self.assertEqual(first.read_line(), {"type": "detached"})
        self.assertIsNone(first.read_line())  # closed after detached
        self.wake()
        self.speak("which one")
        self.assertEqual(second.read_line(), {"type": "utterance", "text": "which one", "via": "wake"})

    def test_replaced_connection_cannot_speak(self) -> None:
        self.start()
        first = self.attach(pid=1)
        self.attach(pid=2)
        with contextlib.suppress(OSError):
            first.send_obj({"type": "reply", "text": "ghost."})
        time.sleep(0.05)  # negative check: nothing should arrive
        self.assertEqual(self.player.enqueued, [])

    def test_disconnect_of_the_current_channel_prints_detached(self) -> None:
        self.start()
        channel = self.attach()
        channel.close()
        self.assertTrue(_wait_for(lambda: "[detached]" in self.out.getvalue()))
        self.wake()
        self.speak("anyone there")
        self.assertEqual(self.player.enqueued, [NO_SESSION_SPEECH])


class AnnounceSocketTest(VoiceTestCase):
    """Runs on a machine that allows unix sockets (not in Claude's sandbox)."""

    def setUp(self) -> None:
        super().setUp()
        self.bridge.close()
        self.bridge = self.make_bridge(announce=ANNOUNCE_LEVELS["all"])
        self.addCleanup(self.bridge.close)

    def test_oneshot_announce_is_spoken_without_attach_or_detach(self) -> None:
        self.start()
        channel = self.connect(hello=False)
        channel.send_obj({"type": "hello", "pid": 7, "cwd": "/w", "session": TOKEN})
        self.assertTrue(_wait_for(lambda: "[attached:" in self.out.getvalue()))
        hook = self.connect(hello=False)
        hook.send_obj({"type": "announce", "session": TOKEN, "event": "permission"})
        hook.close()
        self.assertTrue(self.pump_until(lambda: self.player.enqueued))
        self.assertEqual(self.player.enqueued, ["Claude needs your permission."])
        time.sleep(0.1)  # negative check: the hook's disconnect must not detach
        self.assertEqual(self.out.getvalue().count("[attached:"), 1)
        self.assertNotIn("[detached]", self.out.getvalue())
        self.wake()
        self.speak("still routed")
        self.assertEqual(
            channel.read_line(), {"type": "utterance", "text": "still routed", "via": "wake"}
        )

    def test_oneshot_announce_with_wrong_token_is_silent(self) -> None:
        self.start()
        channel = self.connect(hello=False)
        channel.send_obj({"type": "hello", "pid": 7, "cwd": "/w", "session": TOKEN})
        self.assertTrue(_wait_for(lambda: "[attached:" in self.out.getvalue()))
        hook = self.connect(hello=False)
        hook.send_obj({"type": "announce", "session": OTHER_TOKEN, "event": "permission"})
        hook.close()
        time.sleep(0.1)  # negative check: nothing should arrive
        self.bridge.process_frame(FRAME)
        self.assertEqual(self.player.enqueued, [])


class RobustnessTest(VoiceTestCase):
    def test_oversized_line_drops_that_connection_only(self) -> None:
        self.start()
        good = self.attach(pid=1)
        bad = self.connect(hello=False)
        bad.send_raw(b"x" * (MAX_LINE_BYTES + 10) + b"\n")
        self.assertIsNone(bad.read_line())  # dropped
        # Process survived; the good channel and new connections still work.
        self.wake()
        self.speak("still alive")
        self.assertEqual(good.read_line(), {"type": "utterance", "text": "still alive", "via": "wake"})
        self.attach(pid=3)

    def test_connection_cap_closes_the_extra_connection(self) -> None:
        self.start()
        first = self.attach(pid=1)
        others = [self.connect(hello=False) for _ in range(MAX_CONNECTIONS - 1)]
        self.assertTrue(_wait_for(lambda: len(self.bridge._conns) == MAX_CONNECTIONS))
        extra = self.connect(hello=False)
        self.assertIsNone(extra.read_line())  # closed immediately
        self.assertIn("too many connections", self.out.getvalue())
        self.assertEqual(len(others), MAX_CONNECTIONS - 1)
        self.wake()
        self.speak("still routed")
        self.assertEqual(first.read_line(), {"type": "utterance", "text": "still routed", "via": "wake"})

    def test_oversized_partial_line_without_newline_is_dropped(self) -> None:
        self.start()
        bad = self.connect(hello=False)
        bad.send_raw(b"y" * (MAX_LINE_BYTES + 10))
        self.assertIsNone(bad.read_line())

    def test_malformed_and_unknown_lines_are_ignored(self) -> None:
        self.start()
        channel = self.connect(hello=False)
        channel.send_raw(b"{not json\n")
        channel.send_raw(b"[1, 2]\n")
        channel.send_raw(b"\xff\xfe\n")
        channel.send_obj({"type": "bogus"})
        channel.send_obj({"type": "hello", "pid": "nope", "cwd": 5})
        channel.hello(pid=9)  # the connection is still usable afterwards
        self.assertTrue(_wait_for(lambda: "[attached: session pid 9" in self.out.getvalue()))
        self.assertEqual(self.out.getvalue().count("[attached:"), 1)


class ReadBackReplyTest(VoiceTestCase):
    """Socket-free: the bridge is never started; lines go straight to the handler."""

    socket_free = True

    CHECK = "This will run: the shell command git reset."

    def setUp(self) -> None:
        super().setUp()
        self.conn = _Conn(mock.Mock())
        self.bridge._conn = self.conn

    def reply(self, **fields) -> None:
        line = json.dumps({"type": "reply", "text": "Say confirm walnut lantern.", **fields})
        self.bridge._handle_line(self.conn, line.encode("utf-8"))

    def test_confirm_check_is_spoken_in_own_voice_before_claude(self) -> None:
        self.reply(kind="confirm", check=self.CHECK)
        self.assertEqual(self.player.enqueued[0], self.CHECK)
        self.assertEqual(self.player.own_flags[0], True)
        self.assertEqual(self.player.own_flags[1:], [False] * (len(self.player.own_flags) - 1))
        self.assertGreaterEqual(len(self.player.enqueued), 2)

    def test_check_on_answer_or_ack_is_ignored(self) -> None:
        for kind in ("answer", "ack"):
            with self.subTest(kind=kind):
                self.player.enqueued.clear()
                self.player.own_flags.clear()
                self.reply(kind=kind, check=self.CHECK)
                self.assertNotIn(self.CHECK, self.player.enqueued)
                self.assertNotIn(True, self.player.own_flags)
                self.assertNotIn("undertone:", self.out.getvalue())

    def test_oversized_or_non_str_check_is_ignored(self) -> None:
        for bad in ("x" * (MAX_CHECK_CHARS + 1), "", 7, ["a"], None):
            with self.subTest(check=bad):
                self.player.own_flags.clear()
                self.reply(kind="confirm", check=bad)
                self.assertNotIn(True, self.player.own_flags)
        self.assertNotIn("undertone:", self.out.getvalue())

    def test_check_is_printed_before_claude_and_confirmation_notice(self) -> None:
        self.reply(kind="confirm", check=self.CHECK)
        printed = self.out.getvalue()
        a = printed.index(f"undertone: {self.CHECK}\n")
        b = printed.index("claude: ")
        c = printed.index("[confirmation needed")
        self.assertTrue(a < b < c)

    def test_check_lands_in_echo_filter(self) -> None:
        self.reply(kind="confirm", check=self.CHECK)
        self.assertIn(self.CHECK, self.bridge._spoken_text)


class TooSoonAndRedactionTest(VoiceTestCase):
    """Socket-free: events and lines go straight to the handlers."""

    socket_free = True
    CHECK = "This will run: the shell command git reset."

    def setUp(self) -> None:
        super().setUp()
        self.sent: list[dict] = []
        conn = _Conn(mock.Mock())
        conn.send = lambda obj: self.sent.append(obj) or True
        self.conn = conn
        self.bridge._conn = conn

    def reply(self, **fields) -> None:
        line = json.dumps({"type": "reply", "text": "Say confirm walnut lantern.", **fields})
        self.bridge._handle_line(self.conn, line.encode("utf-8"))

    def say(self, via: str, text: str) -> None:
        self.bridge._turn_via = via
        self.bridge._on_listener_event(ListenerEvent("utterance", text=text))

    def test_check_enqueued_own_before_claude_sentences(self) -> None:
        self.reply(kind="confirm", check=self.CHECK)
        self.assertEqual(self.player.enqueued[0], self.CHECK)
        self.assertEqual(self.player.own_flags[0], True)
        self.assertNotIn(True, self.player.own_flags[1:])
        printed = self.out.getvalue()
        self.assertLess(printed.index("undertone: "), printed.index("claude: "))

    def test_early_wake_turn_held_while_confirm_pending(self) -> None:
        self.reply(kind="confirm", check=self.CHECK)
        self.player.enqueued.clear()
        self.player.own_flags.clear()
        self.say("wake_barge_in", "confirm walnut lantern")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.player.enqueued, [TOO_SOON_SPEECH])
        self.assertEqual(self.player.own_flags, [True])
        self.assertTrue(self.bridge._confirm_pending)
        printed = self.out.getvalue()
        self.assertIn("[too soon after I spoke, so not sent", printed)
        self.assertNotIn("you: ", printed)

        self.say("wake", "confirm walnut lantern")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["via"], "wake")
        self.assertFalse(self.bridge._confirm_pending)

    def test_early_wake_turn_hold_expires_with_the_challenge(self) -> None:
        self.reply(kind="confirm", check=self.CHECK)
        self.player.enqueued.clear()
        self.clock.now = voice_guard.CHALLENGE_TTL_S - 1
        self.say("wake_barge_in", "confirm walnut lantern")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.player.enqueued, [TOO_SOON_SPEECH])

        self.clock.now = voice_guard.CHALLENGE_TTL_S + 1
        self.say("wake_barge_in", "confirm walnut lantern")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["via"], "wake_barge_in")

    def test_early_wake_turn_sent_when_no_confirm_pending(self) -> None:
        self.say("wake_barge_in", "what time is it")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["via"], "wake_barge_in")
        self.assertNotIn(TOO_SOON_SPEECH, self.player.enqueued)
        self.assertIn("you [wake_barge_in]: what time is it", self.out.getvalue())

    def test_safe_phrase_redacted_in_print_but_raw_on_socket(self) -> None:
        voice_guard.set_safe_phrase(self.home, "copper maple")
        self.say("wake", "confirm copper maple please")
        self.assertEqual(self.sent[0]["text"], "confirm copper maple please")
        printed = self.out.getvalue()
        self.assertIn("you: confirm [safe phrase] please", printed)
        self.assertNotIn("copper", printed)

    def test_safe_phrase_redacted_in_echo_filter_prints(self) -> None:
        voice_guard.set_safe_phrase(self.home, "copper maple")
        self.bridge._turn_via = "talk_over"
        self.bridge._echo_ref = "copper maple"
        self.bridge._on_listener_event(ListenerEvent("utterance", text="copper maple"))
        printed = self.out.getvalue()
        self.assertIn("[ignored as my own reply:", printed)
        self.assertNotIn("copper", printed)

    def test_prints_unchanged_without_phrase(self) -> None:
        self.say("wake", "copper maple")
        self.assertIn("you: copper maple\n", self.out.getvalue())


class EchoTextGuardTest(VoiceTestCase):
    """Live failure 2026-11-04: AEC residue of our own voice became `you:` turns."""

    socket_free = True
    REPLY = (
        "Columbus has light rain right now, 63 degrees but it feels like 60. "
        "Wind is about 13 miles an hour and humidity is 93 percent."
    )

    def setUp(self) -> None:
        super().setUp()
        self.sent: list[dict] = []
        conn = _Conn(mock.Mock())
        conn.send = lambda obj: self.sent.append(obj) or True
        self.bridge._conn = conn
        self.bridge._handle_line(
            conn, json.dumps({"type": "reply", "text": self.REPLY}).encode("utf-8")
        )

    def turn(self, kind: str, text: str, *, busy: bool = True, stamp: bool = True) -> None:
        if stamp:
            self.player.busy = True
            self.bridge.process_frame(FRAME)  # stamps _last_busy
        self.player.busy = busy
        self.bridge._on_listener_event(ListenerEvent(kind))
        self.bridge._on_listener_event(ListenerEvent("utterance", text=text))

    def test_numeric_echo_talk_over_is_dropped(self) -> None:
        self.turn("talk_over", "Humidity is ninety-four percent.")
        self.assertEqual(self.sent, [])
        self.assertIn("[dropped: echo of own speech]", self.out.getvalue())

    def test_second_talk_over_after_barge_in_is_still_dropped(self) -> None:
        # Barge-in bumps the generation, so the per-gen echo_ref is empty then.
        self.turn("talk_over", "Humidity is ninety-four percent.")
        self.turn("talk_over", "Humidity's ninety three percent right now.")
        self.assertEqual(self.sent, [])

    def test_follow_up_echo_is_dropped(self) -> None:
        self.turn("follow_up", "wind is about thirteen miles an hour", busy=False)
        self.assertEqual(self.sent, [])
        self.assertIn("you [follow_up]:", self.out.getvalue())

    def test_different_utterance_passes(self) -> None:
        self.turn("follow_up", "No.", busy=False)
        self.turn("follow_up", "What is the weather in Columbus Ohio", busy=False)
        self.assertEqual([m["text"] for m in self.sent], ["No.", "What is the weather in Columbus Ohio"])
        self.assertEqual(self.sent[0]["via"], "follow_up")

    def test_wake_turn_is_never_dropped(self) -> None:
        self.turn("wake", "humidity is 93 percent")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["via"], "wake_barge_in")
        self.player.busy = False
        self.clock.now += 30
        self.turn("wake", "humidity is 93 percent", busy=False, stamp=False)
        self.assertEqual(self.sent[-1]["via"], "wake")

    def test_window_expires(self) -> None:
        self.player.busy = True
        self.bridge.process_frame(FRAME)
        self.clock.now += ECHO_GUARD_S + 1
        self.turn("follow_up", "humidity is 93 percent", busy=False, stamp=False)
        self.assertEqual(len(self.sent), 1)

    def test_history_expires(self) -> None:
        self.clock.now += ECHO_HISTORY_S + 1
        self.turn("talk_over", "humidity is ninety three percent")
        self.assertEqual(len(self.sent), 1)

    def test_announcement_text_counts(self) -> None:
        self.bridge._spoken_log.clear()
        self.bridge._on_announce("finished")
        spoken = self.player.enqueued[-1]
        self.turn("talk_over", spoken)
        self.assertEqual(self.sent, [])

    def test_guard_leaves_grant_rules_alone(self) -> None:
        conn = self.bridge._conn
        self.bridge._handle_line(
            conn,
            json.dumps({"type": "reply", "text": "Say confirm walnut lantern.", "kind": "confirm"}).encode("utf-8"),
        )
        self.assertTrue(self.bridge._confirm_pending)
        self.turn("talk_over", "go ahead and do it")  # not echo: sent, but as talk_over
        self.assertEqual(self.sent[-1]["via"], "talk_over")
        self.player.busy = False
        self.clock.now += 30
        self.turn("wake", "say confirm walnut lantern", busy=False, stamp=False)  # matches recent speech
        self.assertEqual(self.sent[-1], {"type": "utterance", "text": "say confirm walnut lantern", "via": "wake"})


class HomeDefaultIsLazyTest(unittest.TestCase):
    def test_construction_without_home_does_not_create_it(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = Path(tmp.name) / "no-such-home"
        with mock.patch.dict(os.environ, {"UNDERTONE_HOME": str(target)}):
            bridge = VoiceBridge(
                wake_factory=lambda: (lambda frame: False),
                vad=lambda chunk: 0.0,
                stt=lambda audio: "",
                clock=_FakeClock(),
                socket_path=Path(tmp.name) / "voice.sock",
                player_factory=_FakePlayer,
                out=io.StringIO(),
            )
            self.addCleanup(bridge.close)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()

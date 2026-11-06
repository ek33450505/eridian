"""Tests for the VoiceBridge control plane: real mute and control.sock.

Real unix sockets in a short temp ERIDIAN_HOME (via tests.test_voice.VoiceTestCase);
fakes for mic / VAD / STT / player. Never the real ~/.eridian.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests import test_voice as tv
import socket
import time

from eridian.listener import ListenerEvent
from eridian.voice import (
    ACCEPT_MAX_ERRORS,
    CONTROL_MAX_CONNECTIONS,
    CONTROL_MAX_LINE_BYTES,
    VoiceBridge,
)

FRAME = tv.FRAME


class ControlBase(tv.VoiceTestCase):
    """Counts every call into the wake / VAD / STT fakes."""

    def setUp(self) -> None:
        self.wake_calls = 0
        self.vad_calls = 0
        self.stt_calls = 0
        self.stt_hook = None
        super().setUp()

    def make_bridge(self, **overrides) -> VoiceBridge:
        def wake_factory():
            def detect(frame: bytes) -> bool:
                self.wake_calls += 1
                hit, self.wake_flag = self.wake_flag, False
                return hit

            return detect

        def vad(chunk: bytes) -> float:
            self.vad_calls += 1
            return self.vad_value

        def stt(audio) -> str:
            self.stt_calls += 1
            if self.stt_hook is not None:
                self.stt_hook()
            return self.transcript

        kwargs = dict(wake_factory=wake_factory, vad=vad, stt=stt, requester_check=lambda pid: None)
        kwargs.update(overrides)
        return super().make_bridge(**kwargs)

    @property
    def control_path(self) -> Path:
        return self.home / "control.sock"

    @property
    def state_file(self) -> Path:
        return self.home / "voice-state.json"

    def ctl(self) -> tv._Channel:
        channel = tv._Channel(self.control_path)
        self.addCleanup(channel.close)
        return channel

    def ask(self, obj: dict) -> dict | None:
        channel = self.ctl()
        channel.send_obj(obj)
        return channel.read_line()

    def reopen(self, **overrides) -> VoiceBridge:
        self.bridge.close()
        self.bridge = self.make_bridge(**overrides)
        self.addCleanup(self.bridge.close)
        return self.bridge

    def fake_session(self, n: int, bridge: VoiceBridge | None = None) -> tv._FakeConn:
        conn = tv._FakeConn()
        (bridge or self.bridge)._attach(conn, n, "/w", tv._tok(n))
        return conn


class MuteTest(ControlBase):
    def test_muted_frames_never_reach_wake_vad_or_stt(self) -> None:
        self.bridge.set_muted(True)
        self.wake_flag = True
        self.vad_value = 1.0
        for _ in range(5):
            self.bridge.process_frame(FRAME)
        self.assertEqual((self.wake_calls, self.vad_calls, self.stt_calls), (0, 0, 0))
        self.assertTrue(self.wake_flag)
        self.assertEqual(self.state, "IDLE")
        self.assertEqual(self.cues, [])

    def test_replies_still_play_while_muted(self) -> None:
        self.start()
        channel = self.attach()
        self.bridge.set_muted(True)
        channel.send_obj({"type": "reply", "text": "All done."})
        self.assertTrue(tv._wait_for(lambda: self.player.enqueued))
        self.bridge.process_frame(FRAME)
        self.assertEqual(self.player.enqueued, ["All done."])

    def test_follow_up_window_does_not_open_while_muted(self) -> None:
        self.start()
        channel = self.attach()
        self.bridge._reply_origin = "wake"
        channel.send_obj({"type": "reply", "text": "All done."})
        self.assertTrue(tv._wait_for(lambda: self.player.enqueued))
        self.assertTrue(tv._wait_for(lambda: self.bridge._control.qsize() > 0))
        self.bridge.set_muted(True)
        self.bridge.process_frame(FRAME)
        self.assertEqual(self.cues, [])
        self.assertEqual(self.state, "IDLE")

    def test_mute_mid_turn_discards_the_turn(self) -> None:
        self.start()
        channel = self.attach()
        self.wake()
        self.assertEqual(self.state, "LISTENING")
        self.transcript = "launch the missiles"
        self.vad_value = 1.0
        self.clock.now += 0.2
        self.bridge.process_frame(FRAME)  # speech captured before the mute
        self.bridge.set_muted(True)
        self.vad_value = 0.0
        for _ in range(4):
            self.clock.now += 0.8
            self.bridge.process_frame(FRAME)
        self.assertEqual(self.stt_calls, 0)
        self.assertEqual(self.state, "IDLE")
        self.assertIsNone(tv._quiet(channel))
        self.assertNotIn("you:", self.out.getvalue())

    def test_unmute_starts_a_fresh_turn(self) -> None:
        self.start()
        channel = self.attach()
        self.wake()
        self.bridge.set_muted(True)
        self.bridge.process_frame(FRAME)
        self.assertTrue(self.bridge.set_muted(False))
        self.assertEqual(self.state, "IDLE")
        self.wake()
        self.speak("what is the status")
        self.assertEqual(
            channel.read_line(), {"type": "utterance", "text": "what is the status", "via": "wake"}
        )

    def test_muted_state_round_trips(self) -> None:
        self.assertTrue(self.bridge.set_muted(True))
        self.assertIs(json.loads(self.state_file.read_text())["muted"], True)
        self.assertEqual(json.loads(self.state_file.read_text())["version"], 2)
        self.assertTrue(self.reopen().is_muted())
        self.assertTrue(self.bridge.set_muted(False))
        self.assertFalse(self.reopen().is_muted())

    def test_v1_file_loads_unmuted(self) -> None:
        self.state_file.write_text(json.dumps({"version": 1, "off": {}}))
        bridge = self.reopen()
        self.assertFalse(bridge.is_muted())
        self.assertFalse(bridge.voice_state_health()["state_error"])

    def test_v1_file_cannot_smuggle_a_muted_key(self) -> None:
        self.state_file.write_text(json.dumps({"version": 1, "off": {}, "muted": True}))
        self.assertFalse(self.reopen().is_muted())

    def test_bad_muted_values_are_corrupt(self) -> None:
        for doc in (
            {"version": 2, "off": {}},
            {"version": 2, "off": {}, "muted": "yes"},
            {"version": 3, "off": {}, "muted": False},
        ):
            with self.subTest(doc=doc):
                self.state_file.write_text(json.dumps(doc))
                bridge = self.reopen()
                self.assertTrue(bridge.is_muted())
                self.assertTrue(bridge.voice_state_health()["state_error"])

    def test_unreadable_state_starts_muted_and_all_off(self) -> None:
        self.state_file.write_text("{broken")
        bridge = self.reopen()
        self.assertTrue(bridge.is_muted())
        self.fake_session(1, bridge)
        self.assertFalse(bridge.sessions_status()[0]["on"])

    def test_fifo_at_state_path_starts_muted(self) -> None:
        os.mkfifo(self.state_file)
        made: list[VoiceBridge] = []
        worker = threading.Thread(target=lambda: made.append(self.make_bridge()), daemon=True)
        worker.start()
        worker.join(3.0)
        self.assertFalse(worker.is_alive())
        self.addCleanup(made[0].close)
        self.assertTrue(made[0].is_muted())

    def test_symlink_at_state_path_starts_muted(self) -> None:
        target = self.home / "elsewhere.json"
        target.write_text(json.dumps({"version": 2, "off": {}, "muted": False}))
        os.symlink(target, self.state_file)
        self.assertTrue(self.reopen().is_muted())

    def test_failed_mute_write_still_mutes(self) -> None:
        with mock.patch("eridian.voice.paths.write_private_json", side_effect=OSError("disk full")):
            self.assertTrue(self.bridge.set_muted(True))
        self.assertTrue(self.bridge.is_muted())
        self.assertFalse(self.bridge.voice_state_health()["saved"])
        self.wake_flag = True
        self.bridge.process_frame(FRAME)
        self.assertEqual(self.wake_calls, 0)

    def test_failed_unmute_write_stays_muted(self) -> None:
        self.assertTrue(self.bridge.set_muted(True))
        with mock.patch("eridian.voice.paths.write_private_json", side_effect=OSError("disk full")):
            self.assertFalse(self.bridge.set_muted(False))
        self.assertTrue(self.bridge.is_muted())
        self.bridge.process_frame(FRAME)
        self.assertEqual(self.wake_calls, 0)

    def test_unmute_during_state_error_keeps_connected_sessions_off(self) -> None:
        self.state_file.write_text("{broken")
        bridge = self.reopen()
        self.fake_session(1, bridge)
        self.fake_session(2, bridge)
        self.assertTrue(bridge.set_muted(False))
        self.assertFalse(bridge.is_muted())
        self.assertFalse(bridge.voice_state_health()["state_error"])
        self.assertEqual({s["pid"]: s["on"] for s in bridge.sessions_status()}, {1: False, 2: False})
        doc = json.loads(self.state_file.read_text())
        self.assertEqual(
            set(doc["off"]), {hashlib.sha256(tv._tok(n).encode()).hexdigest() for n in (1, 2)}
        )
        self.assertIs(doc["muted"], False)

    def test_other_state_keys_survive_a_mute_write(self) -> None:
        self.state_file.write_text(json.dumps({"version": 2, "off": {}, "muted": False, "future": 7}))
        bridge = self.reopen()
        bridge.set_muted(True)
        self.assertEqual(json.loads(self.state_file.read_text())["future"], 7)

    def test_set_muted_rejects_non_bool(self) -> None:
        self.assertFalse(self.bridge.set_muted("yes"))  # type: ignore[arg-type]
        self.assertFalse(self.bridge.is_muted())

    def test_prints_a_line_per_transition(self) -> None:
        self.bridge.set_muted(True)
        self.bridge.set_muted(True)
        self.bridge.set_muted(False)
        self.assertEqual(self.out.getvalue().count("[muted]"), 1)
        self.assertEqual(self.out.getvalue().count("[unmuted]"), 1)


class MuteRaceTest(ControlBase):
    def test_mute_landing_during_transcription_sends_nothing(self) -> None:
        self.start()
        channel = self.attach()
        self.wake()
        self.stt_hook = lambda: self.bridge.set_muted(True)
        self.speak("send this anyway")
        self.assertEqual(self.stt_calls, 1)
        self.assertIsNone(tv._quiet(channel))
        self.assertIn("[muted: not sent]", self.out.getvalue())
        self.assertNotIn("send this anyway", self.out.getvalue())
        lines = [ln for ln in self.out.getvalue().splitlines() if "muted" in ln]
        self.assertEqual(lines, ["[muted]", "[muted: not sent]"])
        self.assertEqual(self.bridge._outstanding, 0)
        self.assertEqual(self.bridge._reply_origin, "")

    def test_non_send_outcomes_print_no_transcript_when_the_turn_is_muted(self) -> None:
        self.start()
        self.attach()
        secret = "secret words here"

        def run(setup, expect_line: str) -> None:
            self.out.seek(0)
            self.out.truncate()
            self.player.enqueued.clear()
            setup()
            self.bridge._on_listener_event(ListenerEvent(kind="utterance", text=secret))

        cases = {
            "echo drop": lambda: (
                setattr(self.bridge, "_turn_via", "talk_over"),
                setattr(self.bridge, "_echo_hist", secret),
            ),
            "own reply": lambda: (
                setattr(self.bridge, "_turn_via", "talk_over"),
                setattr(self.bridge, "_echo_hist", ""),
                setattr(self.bridge, "_echo_ref", secret),
            ),
            "too soon": lambda: (
                setattr(self.bridge, "_turn_via", "wake_barge_in"),
                setattr(self.bridge, "_confirm_pending", True),
                setattr(self.bridge, "_confirm_pending_at", self.clock.now),
            ),
        }
        for name, setup in cases.items():
            with self.subTest(name=name, muted=False):  # control: the outcome does print when not muted
                run(setup, "")
                self.assertTrue(
                    "dropped" in self.out.getvalue() or "ignored" in self.out.getvalue() or "too soon" in self.out.getvalue()
                )
        self.bridge.set_muted(True)  # the turns above opened before this mute
        for name, setup in cases.items():
            with self.subTest(name=name, muted=True):
                run(setup, "")
                self.assertEqual(self.out.getvalue(), "[muted: not sent]\n")
                self.assertEqual(self.player.enqueued, [])

    def test_mute_landing_during_the_echo_computation_prints_no_transcript(self) -> None:
        self.start()
        channel = self.attach()
        self.bridge._turn_via = "talk_over"
        self.bridge._echo_hist = "something else entirely"

        def mute_then_not_echo(text: str, hist: str) -> bool:
            self.bridge.set_muted(True)  # lands during the decision phase, before the final check
            return False

        with mock.patch("eridian.voice.is_echo", side_effect=mute_then_not_echo):
            self.bridge._on_listener_event(ListenerEvent(kind="utterance", text="send this anyway"))
        self.assertNotIn("send this anyway", self.out.getvalue())
        self.assertIn("[muted: not sent]", self.out.getvalue())
        self.assertIsNone(tv._quiet(channel))

    def test_quick_mute_unmute_during_stt_sends_nothing(self) -> None:
        self.start()
        channel = self.attach()
        self.wake()

        def toggle() -> None:
            self.bridge.set_muted(True)
            self.bridge.set_muted(False)

        self.stt_hook = toggle
        self.speak("send this anyway")
        self.assertEqual(self.stt_calls, 1)
        self.assertIsNone(tv._quiet(channel))
        self.assertNotIn("send this anyway", self.out.getvalue())
        self.assertIn("[muted: not sent]", self.out.getvalue())

    def test_mute_inside_redact_sends_nothing(self) -> None:
        self.start()
        channel = self.attach()
        real = self.bridge._redact

        def mute_then_redact(text: str) -> str:
            self.bridge.set_muted(True)
            return real(text)

        self.bridge._turn_mute_gen = self.bridge._mute_gen
        with mock.patch.object(self.bridge, "_redact", side_effect=mute_then_redact):
            self.bridge._on_listener_event(ListenerEvent(kind="utterance", text="send this anyway"))
        self.assertIsNone(tv._quiet(channel))
        self.assertNotIn("send this anyway", self.out.getvalue())

    def test_transcript_prints_only_after_the_send(self) -> None:
        self.start()
        channel = self.attach()
        conn = self.bridge._sessions[0]
        seen: list[str] = []
        real_send = conn.send

        def spy(obj: dict) -> bool:
            seen.append(self.out.getvalue())
            return real_send(obj)

        self.bridge._turn_mute_gen = self.bridge._mute_gen
        with mock.patch.object(conn, "send", side_effect=spy):
            self.bridge._on_listener_event(ListenerEvent(kind="utterance", text="hello there"))
        self.assertEqual(len(seen), 1)
        self.assertNotIn("hello there", seen[0])
        self.assertEqual(channel.read_line()["text"], "hello there")
        self.assertIn("you: hello there", self.out.getvalue())

    def test_no_io_between_the_final_check_and_the_send(self) -> None:
        self.start()
        self.attach()
        conn = self.bridge._sessions[0]
        order: list[str] = []
        real_print = self.bridge._print
        self.bridge._turn_mute_gen = self.bridge._mute_gen
        with mock.patch.object(self.bridge, "_print", side_effect=lambda x: (order.append("print"), real_print(x))), \
                mock.patch.object(conn, "send", side_effect=lambda obj: order.append("send") or True):
            self.bridge._on_listener_event(ListenerEvent(kind="utterance", text="hello there"))
        self.assertEqual(order, ["send", "print"])

    def test_a_failing_print_after_the_send_still_clears_confirm_pending(self) -> None:
        self.start()
        channel = self.attach()
        self.bridge._turn_mute_gen = self.bridge._mute_gen
        self.bridge._confirm_pending = True
        with mock.patch.object(self.bridge, "_print", side_effect=OSError("log gone")):
            self.bridge._on_listener_event(ListenerEvent(kind="utterance", text="hello there"))
        self.assertEqual(channel.read_line()["text"], "hello there")
        self.assertFalse(self.bridge._confirm_pending)
        self.assertEqual(self.bridge._outstanding, 1)

    def test_a_failing_print_after_a_failed_send_still_rolls_back(self) -> None:
        self.start()
        self.attach()
        conn = self.bridge._sessions[0]
        self.bridge._turn_mute_gen = self.bridge._mute_gen
        with mock.patch.object(conn, "send", return_value=False), \
                mock.patch.object(self.bridge, "_print", side_effect=OSError("log gone")):
            with contextlib.suppress(OSError):
                self.bridge._on_listener_event(ListenerEvent(kind="utterance", text="hello there"))
        self.assertEqual(self.bridge._outstanding, 0)
        self.assertEqual(self.bridge._reply_origin, "")

    def test_turn_open_after_mute_and_unmute_gets_the_current_epoch(self) -> None:
        self.start()
        self.attach()
        self.bridge.set_muted(True)
        self.bridge.set_muted(False)
        self.bridge._on_listener_event(ListenerEvent(kind="follow_up"))
        self.assertEqual(self.bridge._turn_mute_gen, self.bridge._mute_gen)
        self.assertEqual(self.cues, ["follow_up"])

    def test_listener_events_after_a_mute_do_nothing(self) -> None:
        self.start()
        self.attach()
        self.player.busy = True
        self.bridge.set_muted(True)
        for kind in ("wake", "talk_over", "follow_up"):
            self.bridge._on_listener_event(ListenerEvent(kind=kind))
        self.assertEqual(self.cues, [])
        self.assertEqual(self.player.stops, 0)
        self.assertEqual(self.bridge._turn_via, "")

    def test_self_wake_guard_survives_a_mute(self) -> None:
        self.start()
        channel = self.attach()
        self.bridge.set_muted(True)
        self.player.busy = True
        self.bridge.process_frame(FRAME)  # our own reply playing during the mute
        self.player.busy = False
        self.clock.now += 0.3
        self.bridge.set_muted(False)
        self.wake()
        self.speak("what is the status")
        self.assertEqual(channel.read_line()["via"], "wake_barge_in")

    def test_quick_mute_unmute_between_frames_still_cancels(self) -> None:
        self.start()
        self.attach()
        self.wake()
        self.assertEqual(self.state, "LISTENING")
        self.bridge.set_muted(True)
        self.bridge.set_muted(False)
        self.bridge.process_frame(FRAME)
        self.assertEqual(self.state, "IDLE")
        self.assertEqual(self.stt_calls, 0)

    def test_version_must_be_a_real_int(self) -> None:
        for version in (1.0, True, "1", 2.0):
            with self.subTest(version=version):
                self.state_file.write_text(json.dumps({"version": version, "off": {}, "muted": False}))
                bridge = self.reopen()
                self.assertTrue(bridge.is_muted())
                self.assertTrue(bridge.voice_state_health()["state_error"])


class ListenerCancelTest(unittest.TestCase):
    def test_cancel_discards_turn_and_resets_vad(self) -> None:
        from eridian.listener import Listener

        resets: list[int] = []
        stt_calls: list[int] = []
        hits = [True]
        listener = Listener(
            wake_factory=lambda: (lambda frame: hits.pop() if hits else False),
            vad=lambda chunk: 1.0,
            stt=lambda audio: stt_calls.append(1) or "x",
            clock=lambda: 0.0,
            vad_reset=lambda: resets.append(1),
        )
        listener.process_frame(FRAME)
        self.assertEqual(listener.state, "LISTENING")
        before = len(resets)
        listener.cancel()
        self.assertEqual(listener.state, "IDLE")
        self.assertEqual(len(resets), before + 1)
        self.assertEqual(bytes(listener._utterance), b"")
        self.assertEqual(bytes(listener._preroll), b"")
        self.assertEqual(stt_calls, [])


class ControlSocketTest(ControlBase):
    def test_socket_mode_0600_and_removed_on_close(self) -> None:
        self.start()
        mode = os.lstat(self.control_path).st_mode
        self.assertTrue(stat.S_ISSOCK(mode))
        self.assertEqual(stat.S_IMODE(mode), 0o600)
        self.bridge.close()
        self.assertFalse(self.control_path.exists())

    def test_status_shape_and_no_raw_token(self) -> None:
        self.start()
        self.attach(pid=11)
        reply = self.ask({"op": "status"})
        self.assertEqual(
            set(reply), {"ok", "muted", "state_error", "saved", "sessions"}
        )
        self.assertIs(reply["ok"], True)
        self.assertIs(reply["muted"], False)
        self.assertEqual(len(reply["sessions"]), 1)
        self.assertEqual(
            set(reply["sessions"][0]), {"id", "pid", "cwd", "on", "target"}
        )
        self.assertNotIn(tv._tok(11), json.dumps(reply))

    def test_voice_off_and_on_by_id_and_by_pid(self) -> None:
        self.start()
        self.attach(pid=11)
        self.attach(pid=12)
        self.assertEqual(self.ask({"op": "voice_off", "session": tv._sid(11)}), {"ok": True})
        self.assertEqual(self.ask({"op": "voice_off", "session": "12"}), {"ok": True})
        sessions = self.ask({"op": "status"})["sessions"]
        self.assertEqual({s["pid"]: s["on"] for s in sessions}, {11: False, 12: False})
        self.assertEqual(self.ask({"op": "voice_on", "session": "11"}), {"ok": True})
        self.assertEqual(self.ask({"op": "voice_on", "session": tv._sid(12)}), {"ok": True})
        sessions = self.ask({"op": "status"})["sessions"]
        self.assertEqual({s["pid"]: s["on"] for s in sessions}, {11: True, 12: True})
        self.assertIn("[voice off: session pid 11]", self.out.getvalue())

    def test_mute_and_unmute(self) -> None:
        self.start()
        self.assertEqual(self.ask({"op": "mute"}), {"ok": True})
        self.assertTrue(self.bridge.is_muted())
        self.assertIs(self.ask({"op": "status"})["muted"], True)
        self.assertEqual(self.ask({"op": "unmute"}), {"ok": True})
        self.assertFalse(self.bridge.is_muted())

    def test_unmute_failure_reports_could_not_save(self) -> None:
        self.start()
        self.ask({"op": "mute"})
        with mock.patch("eridian.voice.paths.write_private_json", side_effect=OSError("x")):
            self.assertEqual(self.ask({"op": "unmute"}), {"ok": False, "error": "could not save"})
        self.assertTrue(self.bridge.is_muted())

    def test_no_such_session(self) -> None:
        self.start()
        self.assertEqual(
            self.ask({"op": "voice_on", "session": "nope"}), {"ok": False, "error": "no such session"}
        )
        self.assertEqual(
            self.ask({"op": "voice_off", "session": "999"}), {"ok": False, "error": "no such session"}
        )

    def test_cannot_turn_on_a_session_without_a_token(self) -> None:
        self.start()
        self.attach(pid=31, session=None)
        self.assertEqual(
            self.ask({"op": "voice_on", "session": "31"}),
            {"ok": False, "error": "can't turn on a session without a token"},
        )

    def test_voice_on_save_failure(self) -> None:
        self.start()
        self.attach(pid=11)
        self.ask({"op": "voice_off", "session": "11"})
        with mock.patch("eridian.voice.paths.write_private_json", side_effect=OSError("x")):
            self.assertEqual(
                self.ask({"op": "voice_on", "session": "11"}), {"ok": False, "error": "could not save"}
            )

    def test_bad_requests_get_a_fixed_error_and_never_echo(self) -> None:
        self.start()
        bad = {"ok": False, "error": "bad request"}
        secret = "SECRET-MARKER-123"
        channel = self.ctl()
        for raw in (
            b"not json\n",
            b"[1,2]\n",
            b'"just a string"\n',
            b"\xff\xfe\n",
            json.dumps({"op": secret}).encode() + b"\n",
            json.dumps({"op": 5}).encode() + b"\n",
            b"{}\n",
            json.dumps({"op": "voice_on"}).encode() + b"\n",
            json.dumps({"op": "voice_on", "session": 7}).encode() + b"\n",
            json.dumps({"op": "voice_off", "session": ""}).encode() + b"\n",
            json.dumps({"op": "voice_off", "session": secret * 20}).encode() + b"\n",
        ):
            with self.subTest(raw=raw[:20]):
                channel.send_raw(raw)
                reply = channel.read_line()
                self.assertEqual(reply, bad)
        self.assertNotIn(secret, self.out.getvalue())

    def test_several_requests_on_one_connection(self) -> None:
        self.start()
        channel = self.ctl()
        for op, key in (("status", "muted"), ("mute", "ok"), ("status", "muted"), ("unmute", "ok")):
            channel.send_obj({"op": op})
            self.assertIn(key, channel.read_line())
        self.assertFalse(self.bridge.is_muted())

    def test_oversized_line_drops_the_connection_and_attach_survives(self) -> None:
        self.start()
        channel = self.ctl()
        channel.send_raw(b"x" * (CONTROL_MAX_LINE_BYTES + 100) + b"\n")
        self.assertIsNone(channel.read_line())
        self.assertEqual(self.ask({"op": "status"})["ok"], True)

    def test_connection_cap(self) -> None:
        self.start()
        held = [self.ctl() for _ in range(CONTROL_MAX_CONNECTIONS)]
        for channel in held:
            channel.send_obj({"op": "status"})
            self.assertTrue(channel.read_line()["ok"])
        reserve = self.ctl()  # the reserved slot, held open
        self.assertTrue(tv._wait_for(lambda: len(self.bridge._control_conns) == CONTROL_MAX_CONNECTIONS + 1))
        extra = self.ctl()
        extra.send_obj({"op": "status"})
        self.assertIsNone(extra.read_line())
        reserve.send_obj({"op": "status"})
        self.assertTrue(reserve.read_line()["ok"])
        held[0].close()
        self.assertTrue(
            tv._wait_for(lambda: len(self.bridge._control_conns) < CONTROL_MAX_CONNECTIONS)
        )
        self.assertTrue(self.ask({"op": "status"})["ok"])

    def test_idle_connection_is_closed(self) -> None:
        self.bridge = self.reopen(control_idle_s=0.3)
        self.start()
        channel = self.ctl()
        self.assertIsNone(channel.read_line())

    def test_control_cannot_create_an_utterance(self) -> None:
        self.start()
        channel = self.attach(pid=11)
        for req in (
            {"op": "voice_on", "session": "11"},
            {"op": "mute"},
            {"op": "unmute"},
            {"op": "utterance", "text": "hi"},
            {"op": "listen"},
        ):
            self.ask(req)
        self.bridge.process_frame(FRAME)
        self.assertEqual(self.state, "IDLE")
        self.assertIsNone(tv._quiet(channel))
        self.assertEqual(self.cues, [])

    def test_request_line_split_across_chunks(self) -> None:
        self.start()
        channel = self.ctl()
        channel.send_raw(b'{"op":')
        time.sleep(0.15)
        channel.send_raw(b'"status"}\n')
        self.assertTrue(channel.read_line()["ok"])

    def test_slow_drip_is_closed_at_the_line_deadline(self) -> None:
        self.reopen(control_idle_s=0.5)
        self.start()
        channel = self.ctl()
        begun = time.monotonic()
        closed = False
        for _ in range(25):
            try:
                channel.sock.sendall(b" ")
            except OSError:
                closed = True
                break
            time.sleep(0.2)
        self.assertTrue(closed or channel.read_line() is None)
        self.assertLess(time.monotonic() - begun, 3.0)

    def test_connection_lifetime_is_capped(self) -> None:
        with mock.patch("eridian.voice.CONTROL_LIFETIME_S", 0.6):
            self.start()
            channel = self.ctl()
            begun = time.monotonic()
            alive = True
            while alive and time.monotonic() - begun < 3.0:
                try:
                    channel.send_obj({"op": "status"})
                except OSError:
                    alive = False
                    break
                alive = channel.read_line() is not None
                time.sleep(0.1)
            self.assertFalse(alive)
            self.assertLess(time.monotonic() - begun, 2.5)

    def test_request_count_is_capped(self) -> None:
        with mock.patch("eridian.voice.CONTROL_MAX_REQUESTS", 3):
            self.start()
            channel = self.ctl()
            for _ in range(3):
                channel.send_obj({"op": "status"})
                self.assertTrue(channel.read_line()["ok"])
            with contextlib.suppress(OSError):
                channel.send_obj({"op": "status"})
            self.assertIsNone(channel.read_line())

    def test_lock_timeout_answers_instead_of_dropping(self) -> None:
        self.start()
        channel = self.ctl()
        with mock.patch.object(self.bridge, "set_muted", side_effect=TimeoutError("wedged")):
            channel.send_obj({"op": "mute"})
            self.assertEqual(channel.read_line(), {"ok": False, "error": "could not save"})
        channel.send_obj({"op": "status"})
        self.assertTrue(channel.read_line()["ok"])

    def test_status_cwd_is_cleaned_and_capped(self) -> None:
        self.start()
        self.attach(cwd="a\x1b[31m\tb\n" + "x" * 500)
        cwd = self.ask({"op": "status"})["sessions"][0]["cwd"]
        self.assertLessEqual(len(cwd), 200)
        self.assertFalse([c for c in cwd if ord(c) < 32 or 127 <= ord(c) < 160])
        self.assertTrue(cwd.startswith("a[31mb"))

    def test_dead_reader_threads_are_pruned(self) -> None:
        self.start()
        for _ in range(15):
            channel = self.ctl()
            channel.send_obj({"op": "status"})
            channel.read_line()
            channel.close()
            plain = tv._Channel(self.sock_path)
            plain.close()
        self.assertTrue(tv._wait_for(lambda: sum(t.is_alive() for t in self.bridge._threads) <= 4, 4.0))
        self.ask({"op": "status"})
        self.assertLessEqual(len(self.bridge._threads), 8)

    def _assert_accept_survives_oserror(self, bridge_attr: str, loop_name: str) -> None:
        calls: list[int] = []
        bridge = self.bridge

        class _Server:
            def accept(self_inner):
                calls.append(1)
                if len(calls) >= 3:
                    bridge._closed = True
                raise OSError(24, "Too many open files")

        setattr(bridge, bridge_attr, _Server())
        worker = threading.Thread(target=getattr(bridge, loop_name), daemon=True)
        worker.start()
        worker.join(3.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(calls), 3)

    def _failing_server(self, calls: list[int]):
        class _Server:
            def accept(self_inner):
                calls.append(1)
                raise OSError(24, "Too many open files")

            def close(self_inner):
                pass

        return _Server()

    def _streak_server(self, plan: list[str]):
        """accept() follows `plan`: "err" raises OSError, "ok" returns a live socket, "timeout"
        raises socket.timeout; when the plan is spent the bridge is marked closed."""
        bridge = self.bridge
        calls: list[int] = []

        class _Server:
            def accept(self_inner):
                calls.append(1)
                if not plan:
                    bridge._closed = True
                    raise OSError(9, "closed")
                step = plan.pop(0)
                if step == "ok":
                    a, b = socket.socketpair()
                    self.addCleanup(b.close)
                    return a, None
                if step == "timeout":
                    raise socket.timeout()
                raise OSError(24, "Too many open files")

            def close(self_inner):
                pass

        return _Server()

    def _run_loop(self, name: str) -> None:
        with mock.patch("eridian.voice._POLL_S", 0):
            worker = threading.Thread(target=getattr(self.bridge, name), daemon=True)
            worker.start()
            worker.join(5.0)
        self.assertFalse(worker.is_alive())

    def test_accept_error_streak_resets_after_a_success(self) -> None:
        n = ACCEPT_MAX_ERRORS - 1
        for loop, attr in (("_control_accept_loop", "_control_server"), ("_accept_loop", "_server")):
            for good in ("ok", "timeout"):
                with self.subTest(loop=loop, good=good):
                    plan = ["err"] * n + [good] + ["err"] * n
                    setattr(self.bridge, attr, self._streak_server(plan))
                    self.out.seek(0)
                    self.out.truncate()
                    self._run_loop(loop)
                    self.assertNotIn("failed; muted", self.out.getvalue())
                    self.assertNotIn("no new session can attach", self.out.getvalue())
                    self.assertFalse(self.bridge.is_muted())
                    self.bridge._closed = False

    def test_connection_setup_failures_print_one_line_per_streak(self) -> None:
        for loop, attr in (("_control_accept_loop", "_control_server"), ("_accept_loop", "_server")):
            with self.subTest(loop=loop):
                setattr(self.bridge, attr, self._streak_server(["ok"] * 5))
                self.out.seek(0)
                self.out.truncate()
                with mock.patch("eridian.voice._Conn", side_effect=RuntimeError("boom")):
                    self._run_loop(loop)
                self.assertEqual(self.out.getvalue().count("accept error"), 1)
                self.bridge._closed = False

    def test_control_give_up_removes_our_socket_file(self) -> None:
        self.start()
        self.bridge._control_failed()
        self.assertFalse(self.control_path.exists())
        self.assertTrue(self.bridge.is_muted())
        with self.assertRaises(OSError):
            tv._Channel(self.control_path)

    def test_control_give_up_leaves_a_foreign_socket_alone(self) -> None:
        self.start()
        self.control_path.unlink()
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(other.close)
        other.bind(str(self.control_path))
        self.bridge._control_failed()
        self.assertTrue(self.control_path.exists())

    def test_control_accept_loop_giving_up_mutes(self) -> None:
        calls: list[int] = []
        self.bridge._control_server = self._failing_server(calls)
        with mock.patch("eridian.voice._POLL_S", 0):
            worker = threading.Thread(target=self.bridge._control_accept_loop, daemon=True)
            worker.start()
            worker.join(5.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(calls), ACCEPT_MAX_ERRORS)
        self.assertTrue(self.bridge.is_muted())
        self.assertIn("[control socket failed; muted]", self.out.getvalue())
        self.assertEqual(self.out.getvalue().count("accept error"), 1)  # one line per streak

    def test_voice_accept_loop_giving_up_reports_and_does_not_mute(self) -> None:
        calls: list[int] = []
        self.bridge._server = self._failing_server(calls)
        with mock.patch("eridian.voice._POLL_S", 0):
            worker = threading.Thread(target=self.bridge._accept_loop, daemon=True)
            worker.start()
            worker.join(5.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(calls), ACCEPT_MAX_ERRORS)
        self.assertFalse(self.bridge.is_muted())
        self.assertIn("no new session can attach", self.out.getvalue())
        self.assertEqual(self.out.getvalue().count("accept error"), 1)

    def test_status_cwd_drops_unicode_format_characters(self) -> None:
        self.start()
        self.attach(cwd="/w/\u202eevil\u200b/\u2028x")
        cwd = self.ask({"op": "status"})["sessions"][0]["cwd"]
        self.assertEqual(cwd, "/w/evil/x")

    def test_control_accept_loop_survives_a_transient_oserror(self) -> None:
        self._assert_accept_survives_oserror("_control_server", "_control_accept_loop")

    def test_voice_accept_loop_survives_a_transient_oserror(self) -> None:
        self._assert_accept_survives_oserror("_server", "_accept_loop")

    def test_start_fails_and_leaves_no_voice_socket_without_control_socket(self) -> None:
        self.control_path.write_text("precious")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.bridge.start(), 1)
        self.assertIn("not a socket", err.getvalue())
        self.assertFalse(self.sock_path.exists())
        self.assertEqual(self.control_path.read_text(), "precious")

    def test_another_attach_blocks_the_control_socket_too(self) -> None:
        self.start()
        other = self.make_bridge(player_factory=lambda: tv._FakePlayer())
        self.addCleanup(other.close)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(other.start(), 1)
        other.close()
        self.assertTrue(self.control_path.exists())
        self.assertTrue(self.sock_path.exists())


if __name__ == "__main__":
    unittest.main()

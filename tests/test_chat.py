"""Tests for undertone.chat.ChatSession -- fakes only.

No mic, speaker, model, or real `claude` is touched: the wake detector, VAD,
STT and clock are scripted, the brain is a `_FakeBrain` replaying canned event
lists, and the player is a `_FakePlayer` that records calls. The brain worker
is a real thread, so tests synchronise with `_wait_for` (polling) rather than
sleeping for a guessed duration.
"""

from __future__ import annotations

import io
import threading
import time
import unittest

from undertone.brain import BrainError, TextDelta, ToolUse, TurnEnd
from undertone.chat import APOLOGY, ChatSession
from undertone.listener import VAD_CHUNK_BYTES

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


class _FakeBrain:
    """Replays `script[prompt]`: a list of events, callables (run inline), or
    an Exception to raise. The string "BLOCK" streams until `interrupt()`."""

    def __init__(self, world: "_World") -> None:
        self._world = world
        self.alive = True
        self.session_id = None
        self.closed = False

    def ask(self, text: str):
        w = self._world
        w.asks.append(text)
        script = w.script.get(text, [TurnEnd("sid-default", False, False)])
        if script == "BLOCK":
            w.interrupted.clear()
            yield TextDelta("First part. ")
            yield TextDelta("Second part. ")
            w.blocked.set()
            w.interrupted.wait(timeout=5.0)
            yield TextDelta("Stale tail. ")
            yield TurnEnd("sid-blocked", True, True)
            w.finished_blocked.set()
            return
        for ev in script:
            if isinstance(ev, Exception):
                raise ev
            if callable(ev):
                ev()
                continue
            yield ev

    def interrupt(self) -> None:
        self._world.interrupts += 1
        self._world.interrupted.set()

    def close(self) -> None:
        self.closed = True
        self.alive = False


class _World:
    """Shared state across every brain the factory hands out."""

    def __init__(self) -> None:
        self.script: dict = {}
        self.asks: list[str] = []
        self.interrupts = 0
        self.interrupted = threading.Event()
        self.blocked = threading.Event()
        self.finished_blocked = threading.Event()
        self.factory_calls: list[str | None] = []
        self.brains: list[_FakeBrain] = []

    def factory(self, resume: str | None) -> _FakeBrain:
        self.factory_calls.append(resume)
        brain = _FakeBrain(self)
        self.brains.append(brain)
        return brain


class _FakePlayer:
    def __init__(self, on_start) -> None:
        self.on_start = on_start
        self.enqueued: list[str] = []
        self.stops = 0
        self.busy = False
        self.closed = False
        self.fire_on_start = False

    def enqueue(self, text: str) -> None:
        # Deliberately does NOT call on_start inline: the real player calls it
        # from its own thread, and ChatSession enqueues under its lock (so
        # firing it inline here would self-deadlock); see wait_idle.
        self.enqueued.append(text)

    def stop(self) -> None:
        self.stops += 1

    def wait_idle(self, timeout: float | None = None) -> bool:
        # With `fire_on_start`, report speech starting (the session records
        # only the first) as the real player would while playing.
        if self.fire_on_start:
            for text in self.enqueued:
                self.on_start(text)
        return True

    def close(self) -> None:
        self.closed = True


class ChatSessionTestCase(unittest.TestCase):
    follow_up_s = 6.0
    resume_id: str | None = None
    talk_over = False

    def setUp(self) -> None:
        self.clock = _FakeClock()
        self.world = _World()
        self.out = io.StringIO()
        self.cues: list[str] = []
        self.saved: list[str] = []
        self.players: list[_FakePlayer] = []
        self.wake_flag = False
        self.vad_value = 0.0
        self.transcript = ""

        def wake_factory():
            def detect(frame: bytes) -> bool:
                hit, self.wake_flag = self.wake_flag, False
                return hit

            return detect

        def player_factory(on_start):
            player = _FakePlayer(on_start)
            self.players.append(player)
            return player

        self.session = ChatSession(
            wake_factory=wake_factory,
            vad=lambda chunk: self.vad_value,
            stt=lambda audio: self.transcript,
            clock=self.clock,
            brain_factory=self.world.factory,
            player_factory=player_factory,
            save_session=self.saved.append,
            resume_session_id=self.resume_id,
            follow_up_s=self.follow_up_s,
            cue=self.cues.append,
            out=self.out,
            talk_over=self.talk_over,
        )
        self.player = self.players[0]
        self.addCleanup(self.session.close)

    # -- drivers (main thread) -------------------------------------------

    def wake(self) -> None:
        self.wake_flag = True
        self.session.process_frame(FRAME)

    def speak(self, text: str) -> None:
        """Speech chunk, then enough silence to end the turn."""
        self.transcript = text
        self.vad_value = 1.0
        self.clock.now += 0.2
        self.session.process_frame(FRAME)
        self.vad_value = 0.0
        self.clock.now += 0.8
        self.session.process_frame(FRAME)

    def pump_until(self, predicate, timeout: float = 2.0) -> bool:
        def step() -> bool:
            self.session.process_frame(FRAME)
            return predicate()

        return _wait_for(step, timeout)

    def settle(self) -> None:
        """Wait until the worker has fully served every submitted utterance."""
        self.assertTrue(_wait_for(lambda: self.session._in_flight == 0))

    @property
    def listener_state(self) -> str:
        return self.session._listener.state


class ConversationFlowTest(ChatSessionTestCase):
    def test_wake_utterance_sentences_then_follow_up_without_wake(self) -> None:
        self.world.script["hello"] = [
            TextDelta("Hi there. How "),
            TextDelta("are you? "),
            TurnEnd("sid-1", False, False),
        ]
        self.wake()
        self.assertEqual(self.cues, ["wake"])
        self.assertEqual(self.player.stops, 0)  # idle wake is not a barge-in
        self.speak("hello")
        self.settle()

        self.assertEqual(self.world.asks, ["hello"])
        self.assertEqual(self.player.enqueued, ["Hi there.", "How are you?"])
        self.assertIn("you: hello", self.out.getvalue())

        # Reply done -> follow-up window opens on the next frame.
        self.assertTrue(self.pump_until(lambda: self.listener_state == "LISTENING"))
        self.assertEqual(self.cues, ["wake", "follow_up"])

        self.world.script["and more"] = [TextDelta("Sure. "), TurnEnd("sid-1", False, False)]
        self.speak("and more")  # no wake word
        self.settle()
        self.assertEqual(self.world.asks, ["hello", "and more"])
        self.assertEqual(self.player.enqueued[-1], "Sure.")
        self.assertEqual(self.world.factory_calls, [None])  # one brain, reused

    def test_session_id_saved_on_turn_end(self) -> None:
        self.world.script["hello"] = [TextDelta("Hi. "), TurnEnd("sid-42", False, False)]
        self.wake()
        self.speak("hello")
        self.settle()
        self.assertEqual(self.saved, ["sid-42"])

    def test_tool_use_flushes_pending_sentence_first(self) -> None:
        snapshots: list[list[str]] = []
        self.world.script["look"] = [
            TextDelta("Let me check. Looking now"),
            ToolUse("Read"),
            lambda: snapshots.append(list(self.player.enqueued)),
            TextDelta(" done. "),
            TurnEnd("sid-1", False, False),
        ]
        self.wake()
        self.speak("look")
        self.settle()
        self.assertEqual(snapshots, [["Let me check.", "Looking now"]])
        self.assertEqual(self.player.enqueued, ["Let me check.", "Looking now", "done."])
        self.assertIn("[tool: Read]", self.out.getvalue())

    def test_latency_line_printed_once_after_the_full_reply(self) -> None:
        # The player reports speech starting while the turn is being served.
        self.player.fire_on_start = True
        self.world.script["hello"] = [
            TextDelta("Hi. Again "),
            TextDelta("and again. "),
            TurnEnd("s", False, False),
        ]
        self.wake()
        self.speak("hello")
        self.settle()
        out = self.out.getvalue()
        lines = out.splitlines()
        idx = [i for i, ln in enumerate(lines) if "[latency]" in ln]
        self.assertEqual(len(idx), 1)
        line = lines[idx[0]]
        self.assertRegex(
            line,
            r"^\[latency\] speech end \u2192 first say: \d+\.\d\d s \(first token \d+\.\d\d s\)$",
        )
        self.assertEqual(lines[idx[0] - 1], "Hi. Again and again. ")
        self.assertGreater(out.index("[latency]"), out.index("and again."))

    def test_close_closes_brain_and_player(self) -> None:
        self.wake()
        self.speak("hello")
        self.settle()
        self.session.close()
        self.assertTrue(self.world.brains[0].closed)
        self.assertTrue(self.player.closed)
        self.assertFalse(self.session._worker.is_alive())


class NoFollowUpTest(ChatSessionTestCase):
    follow_up_s = 0.0

    def test_follow_up_disabled(self) -> None:
        self.world.script["hello"] = [TextDelta("Hi. "), TurnEnd("s", False, False)]
        self.wake()
        self.speak("hello")
        self.settle()
        for _ in range(20):
            self.session.process_frame(FRAME)
        self.assertEqual(self.listener_state, "IDLE")
        self.assertEqual(self.cues, ["wake"])

    def test_barge_in_while_only_speaking(self) -> None:
        self.world.script["hello"] = [TextDelta("Hi. "), TurnEnd("s", False, False)]
        self.wake()
        self.speak("hello")
        self.settle()
        self.player.busy = True  # turn is over but audio is still playing

        self.wake()
        self.assertEqual(self.player.stops, 1)
        self.assertIn("[interrupted]", self.out.getvalue())
        self.assertEqual(self.listener_state, "LISTENING")
        self.assertEqual(self.cues, ["wake", "wake"])
        self.assertNotIn("follow_up", self.cues)

    def test_brain_error_apologises_and_next_utterance_recreates_brain(self) -> None:
        self.world.script["one"] = [TextDelta("Fine. "), TurnEnd("sid-1", False, False)]
        self.world.script["two"] = [BrainError("child died")]
        self.world.script["three"] = [TextDelta("Back. "), TurnEnd("sid-2", False, False)]

        self.wake()
        self.speak("one")
        self.settle()
        self.wake()
        self.speak("two")
        self.settle()
        self.assertEqual(self.player.enqueued[-1], APOLOGY)
        self.assertIn("child died", self.out.getvalue())
        self.assertTrue(self.world.brains[0].closed)

        self.wake()
        self.speak("three")
        self.settle()
        self.assertEqual(self.world.factory_calls, [None, "sid-1"])
        self.assertEqual(self.player.enqueued[-1], "Back.")


class InterruptedLatencyTest(ChatSessionTestCase):
    def test_interrupted_turn_prints_no_latency_line(self) -> None:
        self.player.fire_on_start = True
        self.world.script["story"] = "BLOCK"
        self.wake()
        self.speak("story")
        self.assertTrue(self.world.blocked.wait(timeout=2.0))
        self.wake()  # barge-in
        self.assertTrue(self.world.finished_blocked.wait(timeout=2.0))
        self.settle()
        self.assertNotIn("[latency]", self.out.getvalue())


class ResumeFailureTest(ChatSessionTestCase):
    follow_up_s = 0.0
    resume_id = "gone-session"

    def test_failed_resume_clears_id_so_next_utterance_starts_fresh(self) -> None:
        self.world.script["one"] = [BrainError("no such session")]
        self.world.script["two"] = [TextDelta("Ok. "), TurnEnd("sid-new", False, False)]
        self.wake()
        self.speak("one")
        self.settle()
        self.assertIn("starting a fresh one", self.out.getvalue())
        self.wake()
        self.speak("two")
        self.settle()
        self.assertEqual(self.world.factory_calls, ["gone-session", None])
        self.assertEqual(self.saved, ["sid-new"])


class SanitizeTest(ChatSessionTestCase):
    def test_terminal_escapes_are_stripped_from_output(self) -> None:
        self.world.script["hi"] = [
            TextDelta("Hello \x1b]52;c;ZXZpbA==\x07world\x1b[2J. "),
            ToolUse("Re\x1bad"),
            TurnEnd("s", False, False),
        ]
        self.wake()
        self.speak("hi")
        self.settle()
        out = self.out.getvalue()
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertIn("Hello ", out)
        self.assertIn("world", out)
        self.assertIn("[tool: Read]", out)


class BargeInTest(ChatSessionTestCase):
    def test_barge_in_mid_turn_discards_stale_output(self) -> None:
        self.world.script["story"] = "BLOCK"
        self.world.script["new question"] = [
            TextDelta("New answer. "),
            TurnEnd("sid-new", False, False),
        ]
        self.wake()
        self.speak("story")
        self.assertTrue(self.world.blocked.wait(timeout=2.0))
        self.assertEqual(self.player.enqueued, ["First part.", "Second part."])

        self.wake()  # barge-in
        self.assertEqual(self.player.stops, 1)
        self.assertEqual(self.world.interrupts, 1)
        self.assertIn("[interrupted]", self.out.getvalue())
        self.assertEqual(self.cues, ["wake", "wake"])
        self.assertEqual(self.listener_state, "LISTENING")

        self.assertTrue(self.world.finished_blocked.wait(timeout=2.0))
        self.settle()
        # Drained silently: the stale tail was neither spoken nor printed, and
        # the interrupted turn did not open a follow-up window.
        self.assertNotIn("Stale tail.", self.player.enqueued)
        self.assertNotIn("Stale tail", self.out.getvalue())
        for _ in range(10):
            self.session.process_frame(FRAME)
        self.assertNotIn("follow_up", self.cues)

        # The new question is asked only after the interrupted turn drained.
        self.speak("new question")
        self.settle()
        self.assertEqual(self.world.asks, ["story", "new question"])
        self.assertEqual(self.player.enqueued[-1], "New answer.")
        self.assertEqual(self.saved, ["sid-blocked", "sid-new"])
        self.assertTrue(self.pump_until(lambda: self.listener_state == "LISTENING"))
        self.assertEqual(self.cues[-1], "follow_up")


class TalkOverTest(ChatSessionTestCase):
    talk_over = True

    def _talk(self, n: int = 10) -> None:
        self.vad_value = 1.0
        for _ in range(n):
            self.session.process_frame(FRAME)

    def test_talk_over_interrupts_and_becomes_next_prompt(self) -> None:
        self.world.script["story"] = "BLOCK"
        self.world.script["new question"] = [
            TextDelta("New answer. "),
            TurnEnd("sid-new", False, False),
        ]
        self.wake()
        self.speak("story")
        self.assertTrue(self.world.blocked.wait(timeout=2.0))
        self.player.busy = True

        self.transcript = "new question"
        self._talk()
        self.assertEqual(self.player.stops, 1)
        self.assertEqual(self.world.interrupts, 1)
        self.assertIn("[interrupted]", self.out.getvalue())
        self.assertEqual(self.cues, ["wake"])  # no cue for the talk-over turn
        self.assertEqual(self.listener_state, "LISTENING")

        self.assertTrue(self.world.finished_blocked.wait(timeout=2.0))
        self.player.busy = False
        self.vad_value = 0.0
        self.clock.now += 1.0
        self.session.process_frame(FRAME)
        self.settle()
        self.assertEqual(self.world.asks, ["story", "new question"])

    def _barge_in_on_story(self, heard: str) -> None:
        self.world.script["story"] = "BLOCK"
        self.wake()
        self.speak("story")
        self.assertTrue(self.world.blocked.wait(timeout=2.0))
        self.player.busy = True
        self.transcript = heard
        self._talk()
        self.assertTrue(self.world.finished_blocked.wait(timeout=2.0))
        self.player.busy = False
        self.vad_value = 0.0
        self.clock.now += 1.0
        self.session.process_frame(FRAME)
        self.settle()

    def test_echo_like_talk_over_is_not_submitted(self) -> None:
        self._barge_in_on_story("first part second part")
        self.assertEqual(self.world.asks, ["story"])
        self.assertIn(
            '[ignored as my own reply: "first part second part"]', self.out.getvalue()
        )

    def test_mixed_echo_and_user_speech_submits_only_the_user_part(self) -> None:
        self._barge_in_on_story("first part second part what time is it")
        self.assertEqual(self.world.asks, ["story", "what time is it"])
        self.assertIn(
            '[removed my own words: "first part second part what time is it" -> '
            '"what time is it"]',
            self.out.getvalue(),
        )

    def test_unrelated_talk_over_is_submitted(self) -> None:
        self._barge_in_on_story("never mind that, what time is it")
        self.assertEqual(self.world.asks, ["story", "never mind that, what time is it"])

    def test_idle_player_does_not_arm(self) -> None:
        self._talk(30)
        self.assertEqual(self.listener_state, "IDLE")
        self.assertEqual(self.player.stops, 0)


class TalkOverDisabledTest(ChatSessionTestCase):
    def test_busy_player_does_not_arm_without_the_flag(self) -> None:
        self.player.busy = True
        self.vad_value = 1.0
        for _ in range(30):
            self.session.process_frame(FRAME)
        self.assertEqual(self.listener_state, "IDLE")
        self.assertEqual(self.player.stops, 0)
        self.assertEqual(self.world.interrupts, 0)


if __name__ == "__main__":
    unittest.main()

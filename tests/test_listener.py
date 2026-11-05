"""Tests for the eridian.listener state machine.

The listener is driven by raw audio bytes and three injected callables (wake,
vad, stt) plus a clock -- no real audio, model, or network ever touches these
tests. Test "frames" are sized to exactly one VAD chunk (1024 bytes = 512
int16 samples) so each process_frame() call triggers exactly one vad() call,
which keeps the clock-advance timeline in each test deterministic. Real audio
frames are 320 bytes (10 ms, see audio.py); the listener buffers across
frame boundaries regardless of the caller's chunk size, so this is a safe
simplification, not a behavior change.
"""

from __future__ import annotations

import unittest

from eridian.listener import (
    FRAME_BYTES,
    VAD_CHUNK_BYTES,
    Listener,
    ListenerConfig,
    strip_wake_phrase,
)


def _chunk(value: int = 0) -> bytes:
    return bytes([value % 256]) * VAD_CHUNK_BYTES


class _FakeClock:
    """A controllable clock: tests set `.now` directly between calls."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _CountingWakeFactory:
    """Wraps a script of per-call booleans; records how many detectors were made."""

    def __init__(self, triggers_on_call: set[int] = frozenset()) -> None:
        self._triggers_on_call = triggers_on_call
        self.detectors_created = 0
        self._global_call_index = 0

    def __call__(self):
        self.detectors_created += 1

        def detect(frame: bytes) -> bool:
            del frame
            idx = self._global_call_index
            self._global_call_index += 1
            return idx in self._triggers_on_call

        return detect


class _ScriptedVad:
    """Returns successive values from a script, repeating the last value after."""

    def __init__(self, script: list[float]) -> None:
        self._script = script
        self._i = 0
        self.calls = 0

    def __call__(self, chunk: bytes) -> float:
        del chunk
        self.calls += 1
        value = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return value


def _make_listener(
    wake_factory,
    vad,
    stt,
    clock,
    events: list,
    config: ListenerConfig | None = None,
) -> Listener:
    return Listener(
        wake_factory=wake_factory,
        vad=vad,
        stt=stt,
        clock=clock,
        config=config,
        on_event=events.append,
    )


class StripWakePhraseTest(unittest.TestCase):
    def test_strips_bare_wake_phrase_to_empty(self) -> None:
        self.assertEqual(strip_wake_phrase("Hey Jarvis."), "")

    def test_strips_leading_phrase_keeps_remainder(self) -> None:
        self.assertEqual(
            strip_wake_phrase("Hey Jarvis, what time is it?"), "what time is it?"
        )

    def test_leaves_unrelated_text_alone(self) -> None:
        self.assertEqual(strip_wake_phrase("turn on the lights"), "turn on the lights")

    def test_a_jarvis_is_stripped(self) -> None:
        self.assertEqual(strip_wake_phrase("A Jarvis."), "")
        self.assertEqual(strip_wake_phrase("A Jarvis, run the tests"), "run the tests")

    def test_a_is_not_stripped_as_a_prefix_of_other_words(self) -> None:
        self.assertEqual(strip_wake_phrase("a jar of jam"), "a jar of jam")
        self.assertEqual(strip_wake_phrase("Alpha Jarvis"), "Alpha Jarvis")
        self.assertEqual(strip_wake_phrase("ajarvis"), "ajarvis")

    def test_case_insensitive_and_variants(self) -> None:
        self.assertEqual(strip_wake_phrase("OK JARVIS turn it off"), "turn it off")
        self.assertEqual(strip_wake_phrase("hi jarvis: stop"), "stop")


class ListenerHappyPathTest(unittest.TestCase):
    def test_wake_then_speech_then_silence_emits_stripped_utterance(self) -> None:
        clock = _FakeClock(0.0)
        wake_factory = _CountingWakeFactory(triggers_on_call={0})
        # chunk 0: triggers wake (consumed by the IDLE-state wake() call, not vad)
        # chunk 1: speech (processed after the 0.15s beep-skip window)
        # chunk 2: silence -- end-of-turn fires once 0.7s of silence has passed
        vad = _ScriptedVad([1.0, 0.0])
        events: list = []
        stt = lambda audio: "Hey Jarvis, turn on the lights"  # noqa: E731

        listener = _make_listener(wake_factory, vad, stt, clock, events)

        listener.process_frame(_chunk(0))  # wake
        self.assertEqual(events[-1].kind, "wake")

        clock.now = 0.2  # past the 0.15s beep-skip window
        listener.process_frame(_chunk(1))  # speech chunk, last_voice -> 0.2

        clock.now = 0.95  # 0.2 + 0.7s silence -> end of turn
        listener.process_frame(_chunk(2))

        kinds = [e.kind for e in events]
        self.assertEqual(kinds, ["wake", "utterance"])
        utterance = events[-1]
        self.assertEqual(utterance.text, "turn on the lights")
        self.assertIsNotNone(utterance.audio_s)
        self.assertIsNotNone(utterance.stt_ms)


class ListenerNoSpeechTimeoutTest(unittest.TestCase):
    def test_no_speech_within_timeout_emits_wake_no_speech(self) -> None:
        clock = _FakeClock(0.0)
        wake_factory = _CountingWakeFactory(triggers_on_call={0})
        vad = _ScriptedVad([0.0])  # never crosses the speech threshold
        events: list = []
        stt = lambda audio: "unused"  # noqa: E731

        listener = _make_listener(wake_factory, vad, stt, clock, events)
        listener.process_frame(_chunk(0))  # wake

        clock.now = 4.1  # past the 4.0s no-speech timeout
        listener.process_frame(_chunk(1))

        self.assertEqual([e.kind for e in events], ["wake", "wake_no_speech"])


class ListenerMaxDurationTest(unittest.TestCase):
    def test_continuous_speech_is_cut_at_max_utterance(self) -> None:
        clock = _FakeClock(0.0)
        wake_factory = _CountingWakeFactory(triggers_on_call={0})
        vad = _ScriptedVad([1.0])  # continuous speech -- end_silence never fires
        events: list = []
        stt = lambda audio: "still talking"  # noqa: E731

        listener = _make_listener(wake_factory, vad, stt, clock, events)
        listener.process_frame(_chunk(0))  # wake

        clock.now = 5.0
        listener.process_frame(_chunk(1))  # speech, last_voice -> 5.0

        clock.now = 15.1  # past max_utterance_s=15.0 from wake (t=0)
        listener.process_frame(_chunk(2))  # still "speech" -- last_voice -> 15.1 too,
        # but the cut is driven by elapsed time since wake, not since last voice

        self.assertEqual([e.kind for e in events], ["wake", "utterance"])
        self.assertEqual(events[-1].text, "still talking")


class ListenerEmptyTranscriptTest(unittest.TestCase):
    def _run_to_utterance_end(self, stt) -> list:
        clock = _FakeClock(0.0)
        wake_factory = _CountingWakeFactory(triggers_on_call={0})
        vad = _ScriptedVad([1.0, 0.0])
        events: list = []
        listener = _make_listener(wake_factory, vad, stt, clock, events)

        listener.process_frame(_chunk(0))  # wake
        clock.now = 0.2
        listener.process_frame(_chunk(1))  # speech
        clock.now = 0.95
        listener.process_frame(_chunk(2))  # silence -> end of turn
        return events

    def test_wake_phrase_only_transcript_is_dropped(self) -> None:
        events = self._run_to_utterance_end(lambda audio: "Hey Jarvis.")
        self.assertEqual([e.kind for e in events], ["wake", "dropped_empty"])

    def test_blank_transcript_is_dropped(self) -> None:
        events = self._run_to_utterance_end(lambda audio: "   ")
        self.assertEqual([e.kind for e in events], ["wake", "dropped_empty"])


class ListenerBeepSkipWindowTest(unittest.TestCase):
    def test_vad_hits_inside_beep_window_are_ignored(self) -> None:
        clock = _FakeClock(0.0)
        wake_factory = _CountingWakeFactory(triggers_on_call={0})

        # Tied directly to wall-clock elapsed time (not a call-counting
        # script) so the assertion holds regardless of whether a correct
        # implementation calls vad() at all during the skip window or skips
        # the call entirely -- either is a valid way to "ignore" it. A
        # correct skip means the window chunk's "speech" is never acted on;
        # a buggy one lets it start the turn, which then ends as an
        # "utterance" (from the 0.7s of real silence that follows) instead
        # of timing out with "wake_no_speech".
        def vad(chunk: bytes) -> float:
            del chunk
            return 1.0 if clock.now < 0.15 else 0.0

        events: list = []
        stt = lambda audio: "should never be reached"  # noqa: E731

        listener = _make_listener(wake_factory, vad, stt, clock, events)

        listener.process_frame(_chunk(0))  # wake, t_wake = 0.0
        clock.now = 0.05  # still inside the 0.15s beep-skip window
        listener.process_frame(_chunk(1))  # the "loud" chunk -- must be ignored

        clock.now = 4.1  # past the no-speech timeout
        listener.process_frame(_chunk(2))

        self.assertEqual([e.kind for e in events], ["wake", "wake_no_speech"])


class ListenerRealFrameSizeTest(unittest.TestCase):
    def test_320_byte_frames_buffer_correctly_across_1024_byte_vad_chunks(self) -> None:
        # 320 (real mic frame size) doesn't divide 1024 (one VAD chunk)
        # evenly -- this proves leftover bytes from one frame carry over
        # into the next rather than being dropped or misaligned.
        clock = _FakeClock(0.0)
        wake_factory = _CountingWakeFactory(triggers_on_call={0})
        received_chunk_lens: list[int] = []

        def vad(chunk: bytes) -> float:
            received_chunk_lens.append(len(chunk))
            return 0.0  # silence throughout -- just exercising buffering

        events: list = []
        stt = lambda audio: "unused"  # noqa: E731

        listener = _make_listener(wake_factory, vad, stt, clock, events)
        listener.process_frame(_chunk(0)[:FRAME_BYTES])  # wake (content irrelevant)

        num_frames = 10
        total_bytes = num_frames * FRAME_BYTES  # 3200 bytes
        clock.now = 1.0  # well outside the 0.15s beep-skip window
        for i in range(num_frames):
            frame = bytes([i % 256]) * FRAME_BYTES
            listener.process_frame(frame)

        expected_chunks = total_bytes // VAD_CHUNK_BYTES  # 3 chunks of 1024
        self.assertEqual(len(received_chunk_lens), expected_chunks)
        self.assertTrue(all(n == VAD_CHUNK_BYTES for n in received_chunk_lens))

        # The turn still ends correctly despite the irregular frame size --
        # drive it to the no-speech timeout and confirm it fires.
        clock.now = 4.1
        listener.process_frame(bytes([0]) * FRAME_BYTES)
        self.assertEqual([e.kind for e in events], ["wake", "wake_no_speech"])


class ListenerWakeDetectorRecreationTest(unittest.TestCase):
    def test_wake_detector_is_recreated_after_each_turn(self) -> None:
        clock = _FakeClock(0.0)
        wake_factory = _CountingWakeFactory(triggers_on_call={0})
        vad = _ScriptedVad([0.0])
        events: list = []
        stt = lambda audio: "unused"  # noqa: E731

        listener = _make_listener(wake_factory, vad, stt, clock, events)
        self.assertEqual(wake_factory.detectors_created, 1)  # constructor call

        listener.process_frame(_chunk(0))  # wake
        clock.now = 4.1
        listener.process_frame(_chunk(1))  # no-speech timeout -> back to IDLE

        self.assertEqual([e.kind for e in events], ["wake", "wake_no_speech"])
        self.assertEqual(wake_factory.detectors_created, 2)  # recreated after the turn


class ListenerConfigOverrideTest(unittest.TestCase):
    def test_custom_timings_are_honored(self) -> None:
        clock = _FakeClock(0.0)
        wake_factory = _CountingWakeFactory(triggers_on_call={0})
        vad = _ScriptedVad([0.0])
        events: list = []
        stt = lambda audio: "unused"  # noqa: E731
        config = ListenerConfig(no_speech_timeout_s=1.0)

        listener = _make_listener(wake_factory, vad, stt, clock, events, config=config)
        listener.process_frame(_chunk(0))  # wake
        clock.now = 1.1  # would NOT trigger the default 4.0s timeout
        listener.process_frame(_chunk(1))

        self.assertEqual([e.kind for e in events], ["wake", "wake_no_speech"])


class ListenerListenNowTest(unittest.TestCase):
    def _setup(self, vad_script, stt=lambda audio: "unused", wake_on=frozenset()):
        clock = _FakeClock(0.0)
        events: list = []
        listener = _make_listener(
            _CountingWakeFactory(triggers_on_call=set(wake_on)),
            _ScriptedVad(vad_script),
            stt,
            clock,
            events,
        )
        return listener, clock, events

    def test_enters_listening_and_emits_follow_up_not_wake(self) -> None:
        listener, _, events = self._setup([0.0])
        self.assertEqual(listener.state, "IDLE")
        listener.listen_now()
        self.assertEqual(listener.state, "LISTENING")
        self.assertEqual([e.kind for e in events], ["follow_up"])

    def test_custom_timeout_is_honored_not_default(self) -> None:
        listener, clock, events = self._setup([0.0])
        listener.listen_now(no_speech_timeout_s=1.0)
        clock.now = 0.9
        listener.process_frame(_chunk(1))
        self.assertEqual(listener.state, "LISTENING")
        clock.now = 1.1  # default is 4.0
        listener.process_frame(_chunk(1))
        self.assertEqual([e.kind for e in events], ["follow_up", "wake_no_speech"])
        self.assertEqual(listener.state, "IDLE")

    def test_next_wake_turn_uses_default_timeout(self) -> None:
        listener, clock, events = self._setup([0.0], wake_on={0})
        listener.listen_now(no_speech_timeout_s=1.0)
        clock.now = 1.1
        listener.process_frame(_chunk(1))  # follow-up times out
        clock.now = 2.0
        listener.process_frame(_chunk(0))  # wake
        clock.now = 3.5  # 1.5 s into the wake turn: past 1.0, short of 4.0
        listener.process_frame(_chunk(1))
        self.assertEqual(listener.state, "LISTENING")
        clock.now = 6.1
        listener.process_frame(_chunk(1))
        self.assertEqual(
            [e.kind for e in events],
            ["follow_up", "wake_no_speech", "wake", "wake_no_speech"],
        )

    def test_is_a_noop_while_already_listening(self) -> None:
        listener, clock, events = self._setup([0.0])
        listener.listen_now(no_speech_timeout_s=1.0)
        clock.now = 0.5
        listener.listen_now(no_speech_timeout_s=100.0)  # must not extend or re-emit
        self.assertEqual([e.kind for e in events], ["follow_up"])
        clock.now = 1.1
        listener.process_frame(_chunk(1))
        self.assertEqual(events[-1].kind, "wake_no_speech")

    def test_utterance_in_follow_up_window_is_transcribed(self) -> None:
        listener, clock, events = self._setup(
            [1.0, 0.0], stt=lambda audio: "Hey Jarvis, and the next one?"
        )
        listener.listen_now()
        clock.now = 0.2
        listener.process_frame(_chunk(1))  # speech
        clock.now = 0.95
        listener.process_frame(_chunk(2))  # silence -> end of turn
        self.assertEqual([e.kind for e in events], ["follow_up", "utterance"])
        self.assertEqual(events[-1].text, "and the next one?")
        self.assertEqual(listener.state, "IDLE")


class ListenerTalkOverTest(unittest.TestCase):
    def _setup(self, script, mode="turn", triggers=frozenset(), stt=None, resets=None):
        clock = _FakeClock(0.0)
        wake_factory = _CountingWakeFactory(triggers_on_call=set(triggers))
        vad = _ScriptedVad(script)
        events: list = []
        listener = Listener(
            wake_factory=wake_factory,
            vad=vad,
            stt=stt or (lambda audio: "never mind that"),
            clock=clock,
            on_event=events.append,
            vad_reset=(lambda: resets.append(1)) if resets is not None else None,
        )
        listener.talk_over = mode
        return listener, clock, events, vad, wake_factory

    def _feed(self, listener, n):
        for i in range(n):
            listener.process_frame(_chunk(i))

    def test_reset_to_idle_clears_talk_over_state(self) -> None:
        listener, _, _, _, _ = self._setup([1.0])
        self._feed(listener, 5)
        self.assertTrue(listener._preroll)
        listener._reset_to_idle()
        self.assertEqual(listener._to_run, 0)
        self.assertFalse(listener._to_buf)
        self.assertFalse(listener._preroll)

    def test_off_never_calls_vad_while_idle(self) -> None:
        listener, _, events, vad, _ = self._setup([1.0], mode="off")
        self._feed(listener, 50)
        self.assertEqual(vad.calls, 0)
        self.assertEqual(events, [])

    def test_invalid_mode_behaves_as_off(self) -> None:
        listener, _, events, vad, _ = self._setup([1.0], mode="bogus")
        self._feed(listener, 50)
        self.assertEqual(vad.calls, 0)
        self.assertEqual(events, [])
        self.assertEqual(listener.state, "IDLE")

    def test_turn_triggers_after_ten_chunks_exactly_once(self) -> None:
        listener, _, events, vad, _ = self._setup([1.0])
        self._feed(listener, 9)
        self.assertEqual(events, [])
        self._feed(listener, 1)
        self.assertEqual([e.kind for e in events], ["talk_over"])
        self.assertEqual(listener.state, "LISTENING")
        self.assertEqual(vad.calls, 10)

    def test_turn_utterance_includes_preroll(self) -> None:
        heard = []

        def stt(audio):
            heard.append(audio)
            return "Hey Jarvis, what about this"

        script = [1.0] * 10 + [0.0]
        listener, clock, events, _, _ = self._setup(script, stt=stt)
        self._feed(listener, 10)
        clock.now = 1.0
        listener.process_frame(_chunk(0))  # silence past end_silence_s
        self.assertEqual([e.kind for e in events], ["talk_over", "utterance"])
        self.assertEqual(events[-1].text, "what about this")
        # 10 pre-roll chunks + the closing silence chunk, in int16 samples
        self.assertEqual(len(heard[0]), 11 * VAD_CHUNK_BYTES // 2)
        self.assertEqual(listener.state, "IDLE")

    def test_preroll_is_bounded(self) -> None:
        heard = []

        def stt(audio):
            heard.append(audio)
            return "hello"

        script = [0.0] * 10 + [1.0] * 10 + [0.0]
        listener, clock, _, _, _ = self._setup(script, stt=stt)
        self._feed(listener, 20)
        clock.now = 1.0
        listener.process_frame(_chunk(0))
        # 0.5 s of pre-roll = 8000 samples, plus the closing chunk's 512
        self.assertEqual(len(heard[0]), 8000 + VAD_CHUNK_BYTES // 2)

    def test_low_chunk_resets_the_run(self) -> None:
        listener, _, events, _, _ = self._setup([1.0] * 9 + [0.0] + [1.0] * 9 + [0.0])
        self._feed(listener, 19)
        self.assertEqual(events, [])
        self.assertEqual(listener.state, "IDLE")

    def test_threshold_boundary(self) -> None:
        listener, _, events, _, _ = self._setup([0.6])
        self._feed(listener, 10)
        self.assertEqual([e.kind for e in events], ["talk_over"])
        listener, _, events, _, _ = self._setup([0.59])
        self._feed(listener, 30)
        self.assertEqual(events, [])

    def test_stop_mode_stays_idle_and_keeps_wake_detector(self) -> None:
        # the wake detector fires on its 13th call (index 12)
        listener, clock, events, _, wake = self._setup(
            [1.0], mode="stop", triggers={12}
        )
        self._feed(listener, 10)
        self.assertEqual([e.kind for e in events], ["talk_over_stop"])
        self.assertEqual(listener.state, "IDLE")
        self.assertEqual(wake.detectors_created, 1)
        listener.talk_over = "off"
        self._feed(listener, 2)
        listener.process_frame(_chunk(0))  # call index 12 -> wake
        self.assertEqual([e.kind for e in events], ["talk_over_stop", "wake"])

    def test_stop_mode_rearms_after_trigger(self) -> None:
        listener, _, events, _, _ = self._setup([1.0], mode="stop")
        self._feed(listener, 20)
        self.assertEqual([e.kind for e in events], ["talk_over_stop"] * 2)

    def test_switching_off_clears_the_counter(self) -> None:
        listener, _, events, _, _ = self._setup([1.0])
        self._feed(listener, 9)
        listener.talk_over = "off"
        self._feed(listener, 1)
        listener.talk_over = "turn"
        self._feed(listener, 9)
        self.assertEqual(events, [])
        self._feed(listener, 1)
        self.assertEqual([e.kind for e in events], ["talk_over"])

    def test_wake_on_same_frame_wins(self) -> None:
        listener, _, events, vad, _ = self._setup([1.0], triggers={9})
        self._feed(listener, 10)
        self.assertEqual([e.kind for e in events], ["wake"])
        self.assertEqual(vad.calls, 9)

    def test_vad_reset_skipped_for_talk_over_but_not_wake(self) -> None:
        resets: list = []
        listener, _, events, _, _ = self._setup([1.0], resets=resets)
        self._feed(listener, 10)
        self.assertEqual([e.kind for e in events], ["talk_over"])
        self.assertEqual(resets, [])
        resets2: list = []
        listener, _, events, _, _ = self._setup(
            [1.0], mode="off", triggers={0}, resets=resets2
        )
        self._feed(listener, 1)
        self.assertEqual([e.kind for e in events], ["wake"])
        self.assertEqual(resets2, [1])


if __name__ == "__main__":
    unittest.main()

"""The listening-core state machine: wake word -> VAD end-of-turn -> STT.

Pure state machine, no I/O of its own -- it's driven by an iterable of 10 ms
(320-byte) int16 mono frames and three injected callables (`wake`, `vad`,
`stt`) plus a `clock`. audio.py supplies the real mic frames and stt.py the
real Parakeet wrapper; tests inject fakes so this module never touches a
microphone, a model, or the network. See tests/test_listener.py.

Talk-over: while IDLE, an owner that has echo-cancelled audio can set
`Listener.talk_over` to "stop" or "turn" (default "off"). Sustained speech
(consecutive VAD chunks over `talk_over_threshold` for `talk_over_min_s`) then
emits "talk_over_stop" (stay IDLE) or opens a "talk_over" turn pre-seeded with
the last `talk_over_preroll_s` of audio. The wake word is always checked first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

import numpy as np

from . import wake

FRAME_BYTES = 320  # 160 int16 samples = 10 ms at 16 kHz
VAD_CHUNK_BYTES = 1024  # 512 int16 samples = 32 ms at 16 kHz
SAMPLE_RATE = 16000

class WakeDetector(Protocol):
    def __call__(self, frame: bytes) -> bool: ...


class VoiceActivityDetector(Protocol):
    def __call__(self, chunk: bytes) -> float: ...


class SpeechToText(Protocol):
    def __call__(self, audio: np.ndarray) -> str: ...


class Clock(Protocol):
    def __call__(self) -> float: ...


@dataclass(frozen=True)
class ListenerConfig:
    """Tunable timing/threshold constants, all in seconds unless noted."""

    end_silence_s: float = 0.7
    no_speech_timeout_s: float = 4.0
    max_utterance_s: float = 15.0
    wake_ignore_window_s: float = 0.15  # skip VAD right after wake (the beep)
    vad_threshold: float = 0.5
    sample_rate: int = SAMPLE_RATE
    talk_over_threshold: float = 0.6  # VAD prob that counts as talk-over speech
    talk_over_min_s: float = 0.3  # sustained speech needed to trigger talk-over
    talk_over_preroll_s: float = 0.5  # audio kept so the first words aren't lost


@dataclass
class ListenerEvent:
    """One emitted event. `kind` selects which other fields are populated."""

    # "wake" | "follow_up" | "utterance" | "wake_no_speech" | "dropped_empty"
    # | "talk_over" | "talk_over_stop"
    # ("follow_up" = a turn opened by listen_now, with no wake word;
    #  "talk_over" = a turn opened by sustained speech during playback;
    #  "talk_over_stop" = same speech, but the owner only wanted a stop)
    kind: str
    text: str | None = None
    audio_s: float | None = None
    stt_ms: float | None = None
    speech_end_to_text_ms: float | None = None


def strip_wake_phrase(text: str) -> str:
    """Strip a leading wake phrase (e.g. "Hey Jarvis,") from a transcript.

    The tail of the wake phrase sometimes spills into the recording, so a
    transcript can come back as just "Hey Jarvis." -- after stripping, that
    collapses to "", which callers treat as an empty transcript.
    """
    return wake.strip_pattern(wake.current()).sub("", text, count=1).strip()


class Listener:
    """IDLE -> LISTENING -> IDLE state machine over a stream of audio frames."""

    def __init__(
        self,
        wake_factory: Callable[[], WakeDetector],
        vad: VoiceActivityDetector,
        stt: SpeechToText,
        clock: Clock,
        config: ListenerConfig | None = None,
        on_event: Callable[[ListenerEvent], None] | None = None,
        vad_reset: Callable[[], None] | None = None,
    ) -> None:
        self._wake_factory = wake_factory
        self._wake = wake_factory()
        self._vad = vad
        self._vad_reset = vad_reset
        self._stt = stt
        self._clock = clock
        self._config = config or ListenerConfig()
        self._on_event = on_event or (lambda event: None)

        self._state = "IDLE"
        self._utterance = bytearray()
        self._vad_buf = bytearray()
        self._speech_started = False
        self._t_wake = 0.0
        self._last_voice = 0.0
        self._no_speech_timeout_s = self._config.no_speech_timeout_s

        # Talk-over: the owner sets this before each process_frame -- "off"
        # | "stop" | "turn"; anything else behaves as "off" (fail closed).
        self.talk_over = "off"
        self._to_run = 0
        self._to_buf = bytearray()
        self._preroll = bytearray()
        chunk_s = VAD_CHUNK_BYTES / 2 / self._config.sample_rate
        self._to_chunks_needed = max(
            1, math.ceil(self._config.talk_over_min_s / chunk_s - 1e-9)
        )
        self._preroll_max = int(
            self._config.talk_over_preroll_s * self._config.sample_rate
        ) * 2

    @property
    def state(self) -> str:
        """"IDLE" or "LISTENING" (read-only)."""
        return self._state

    def listen_now(self, no_speech_timeout_s: float | None = None) -> None:
        """Enter LISTENING without a wake word (the follow-up window).

        `no_speech_timeout_s` overrides the configured timeout for this turn
        only. No-op if a turn is already in progress.
        """
        if self._state == "LISTENING":
            return
        self._start_turn("follow_up", no_speech_timeout_s)

    def process_frame(self, frame: bytes) -> None:
        """Feed one chunk of raw audio bytes; may call `vad` zero or several
        times (frames don't need to align to VAD_CHUNK_BYTES -- leftover
        bytes carry over into the next call)."""
        if self._state == "IDLE":
            self._process_idle(frame)
        else:
            self._process_listening(frame)

    def run(self, frames: Iterable[bytes]) -> None:
        for frame in frames:
            self.process_frame(frame)

    def _process_idle(self, frame: bytes) -> None:
        if self._wake(frame):
            self._clear_talk_over()
            self._start_turn("wake")
            return
        mode = self.talk_over
        if mode not in ("stop", "turn"):
            self._clear_talk_over()
            return
        self._preroll += frame
        if len(self._preroll) > self._preroll_max:
            del self._preroll[: len(self._preroll) - self._preroll_max]
        self._to_buf += frame
        while len(self._to_buf) >= VAD_CHUNK_BYTES:
            chunk = bytes(self._to_buf[:VAD_CHUNK_BYTES])
            del self._to_buf[:VAD_CHUNK_BYTES]
            if self._vad(chunk) >= self._config.talk_over_threshold:
                self._to_run += 1
            else:
                self._to_run = 0
            if self._to_run >= self._to_chunks_needed:
                self._trigger_talk_over(mode)
                return

    def _clear_talk_over(self) -> None:
        self._to_run = 0
        self._to_buf = bytearray()
        self._preroll = bytearray()

    def _trigger_talk_over(self, mode: str) -> None:
        preroll = bytes(self._preroll)
        self._clear_talk_over()
        if mode == "stop":
            # Stay IDLE and keep the wake detector: the user may be mid-"hey
            # jarvis" (the configured wake phrase) and it must still fire.
            self._emit(ListenerEvent(kind="talk_over_stop"))
            return
        # The VAD has been tracking this speech continuously, so no vad_reset.
        self._no_speech_timeout_s = self._config.no_speech_timeout_s
        self._state = "LISTENING"
        self._utterance = bytearray(preroll)
        self._vad_buf = bytearray()
        self._speech_started = True
        self._t_wake = self._clock()
        self._last_voice = self._t_wake
        self._emit(ListenerEvent(kind="talk_over"))

    def _start_turn(self, kind: str, no_speech_timeout_s: float | None = None) -> None:
        self._no_speech_timeout_s = (
            self._config.no_speech_timeout_s
            if no_speech_timeout_s is None
            else no_speech_timeout_s
        )
        self._state = "LISTENING"
        self._utterance = bytearray()
        self._vad_buf = bytearray()
        self._speech_started = False
        self._t_wake = self._clock()
        self._last_voice = self._t_wake
        if self._vad_reset is not None:
            self._vad_reset()
        self._emit(ListenerEvent(kind=kind))

    def _process_listening(self, frame: bytes) -> None:
        cfg = self._config
        self._utterance += frame
        self._vad_buf += frame
        while len(self._vad_buf) >= VAD_CHUNK_BYTES:
            chunk = bytes(self._vad_buf[:VAD_CHUNK_BYTES])
            self._vad_buf = self._vad_buf[VAD_CHUNK_BYTES:]
            now = self._clock()
            if now - self._t_wake < cfg.wake_ignore_window_s:
                continue
            if self._vad(chunk) >= cfg.vad_threshold:
                self._speech_started = True
                self._last_voice = now

        now = self._clock()
        if not self._speech_started and now - self._t_wake >= self._no_speech_timeout_s:
            self._end_turn_no_speech()
            return

        turn_done = (
            self._speech_started and now - self._last_voice >= cfg.end_silence_s
        ) or now - self._t_wake >= cfg.max_utterance_s
        if turn_done:
            self._end_turn_with_speech()

    def _end_turn_no_speech(self) -> None:
        self._emit(ListenerEvent(kind="wake_no_speech"))
        self._reset_to_idle()

    def _end_turn_with_speech(self) -> None:
        audio = (
            np.frombuffer(bytes(self._utterance), dtype=np.int16).astype(np.float32)
            / 32768.0
        )
        t0 = self._clock()
        text = self._stt(audio)
        stt_ms = (self._clock() - t0) * 1000.0
        cleaned = strip_wake_phrase(text.strip())
        if not cleaned:
            self._emit(ListenerEvent(kind="dropped_empty"))
        else:
            self._emit(
                ListenerEvent(
                    kind="utterance",
                    text=cleaned,
                    audio_s=round(len(audio) / self._config.sample_rate, 2),
                    stt_ms=round(stt_ms),
                    speech_end_to_text_ms=round(
                        (self._clock() - self._last_voice) * 1000.0
                    ),
                )
            )
        self._reset_to_idle()

    def _reset_to_idle(self) -> None:
        # Re-create the wake detector after every turn -- the spike found a
        # stale detector could double-trigger on the tail of its own turn.
        self._wake = self._wake_factory()
        self._state = "IDLE"
        self._utterance = bytearray()
        self._vad_buf = bytearray()
        self._clear_talk_over()

    def _emit(self, event: ListenerEvent) -> None:
        self._on_event(event)

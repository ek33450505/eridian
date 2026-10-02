"""The Path B conversation loop: wake word -> Claude -> spoken reply -> follow-up.

`ChatSession` wires the listener (wake word / VAD / STT), a `ClaudeBrain`
(a long-lived `claude` child), a `SentenceChunker` and a `SpeechQueue` into
one hands-free loop:

    IDLE (wake word only) -> LISTENING -> THINKING (brain turn)
        -> SPEAKING (sentences queued) -> follow-up window (LISTENING, no
        wake word needed, `follow_up_s`) -> IDLE

The wake word stays live while Claude thinks and speaks (the listener is IDLE
then), which is what makes barge-in work: saying it again stops the speech,
interrupts the turn, and opens a fresh listening turn. With `talk_over=True`
(echo cancellation on) sustained speech during playback does the same without
the wake word, and what the user says becomes the next prompt (a transcript
has the words that repeat the reply being spoken removed, and is dropped if
nothing is left).

Threading model: the `Listener` and `process_frame` are MAIN-thread only. One
worker thread runs brain turns sequentially, so an interrupted turn is always
drained to its TurnEnd before the next user message is written. The player
has its own thread and calls back into `_on_speech_start`. A generation
counter (bumped on every utterance and every barge-in) lets the worker
discard output from a turn the user has already moved past; sentences are
enqueued under the same lock that the barge-in bump takes, so nothing can be
enqueued after the `player.stop()` that follows a bump.

Test seams: every collaborator (`wake_factory`, `vad`, `stt`, `clock`,
`brain_factory`, `player_factory`, `save_session`, `cue`, `out`) is injected;
tests/test_chat.py uses fakes only -- no mic, speaker, or real `claude`.
"""

from __future__ import annotations

import queue
import re
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Protocol, TextIO

from . import brain as brain_mod
from .brain import BrainError, TextDelta, ToolUse, TurnEnd
from .chunker import SentenceChunker
from .listener import Clock, Listener, ListenerConfig, ListenerEvent
from .player import SpeechQueue
from .spoken import sanitize_terminal, strip_echo

APOLOGY = "Sorry, I lost my connection to Claude."

class BrainLike(Protocol):
    session_id: str | None
    alive: bool

    def ask(self, text: str) -> Iterator[TextDelta | ToolUse | TurnEnd]: ...
    def interrupt(self) -> None: ...
    def close(self) -> None: ...


class PlayerLike(Protocol):
    busy: bool

    def enqueue(self, text: str) -> None: ...
    def stop(self) -> None: ...
    def wait_idle(self, timeout: float | None = None) -> bool: ...
    def close(self) -> None: ...


@dataclass
class _Turn:
    """Latency bookkeeping for the brain turn currently being served."""

    gen: int
    t_submit: float
    t_speech_end: float
    t_first_token: float | None = None
    t_first_say: float | None = None


class ChatSession:
    def __init__(
        self,
        *,
        wake_factory,
        vad,
        stt,
        clock: Clock,
        brain_factory: Callable[[str | None], BrainLike],
        player_factory: Callable[[Callable[[str], None]], PlayerLike] = SpeechQueue,
        save_session: Callable[[str], None] = brain_mod.save_session_id,
        resume_session_id: str | None = None,
        vad_reset=None,
        listener_config: ListenerConfig | None = None,
        follow_up_s: float = 6.0,
        cue: Callable[[str], None] | None = None,
        out: TextIO = sys.stdout,
        talk_over: bool = False,
    ) -> None:
        self._clock = clock
        self._talk_over = talk_over
        self._brain_factory = brain_factory
        self._save_session = save_session
        self._last_session_id = resume_session_id
        self._follow_up_s = follow_up_s
        self._cue = cue or (lambda kind: None)
        self._out = out

        self._lock = threading.Lock()
        self._print_lock = threading.Lock()
        self._gen = 0
        self._spoken_gen = -1  # gen that _spoken_text belongs to (under _lock)
        self._spoken_text = ""  # sentences enqueued in that gen, for the echo filter
        self._talk_over_turn = False  # main thread: the open turn began as talk-over
        self._echo_ref = ""  # main thread: spoken text snapshotted at talk-over
        self._in_flight = 0  # utterances submitted but not yet fully served
        self._turn: _Turn | None = None
        self._brain: BrainLike | None = None
        self._closed = False
        self._resume_unconfirmed = False  # brain made with a resume id, no TurnEnd yet

        self._chunker = SentenceChunker()
        self._prompts: queue.Queue = queue.Queue()
        self._control: queue.Queue = queue.Queue()

        self._player = player_factory(self._on_speech_start)
        self._listener = Listener(
            wake_factory=wake_factory,
            vad=vad,
            stt=stt,
            clock=clock,
            config=listener_config,
            on_event=self._on_listener_event,
            vad_reset=vad_reset,
        )
        self._worker = threading.Thread(
            target=self._worker_loop, name="undertone-brain", daemon=True
        )
        self._worker.start()

    # ---- main thread -------------------------------------------------

    def process_frame(self, frame: bytes) -> None:
        self._drain_control()
        self._listener.talk_over = (
            "turn" if self._talk_over and self._player.busy else "off"
        )
        self._listener.process_frame(frame)

    def run(self, frames: Iterable[bytes]) -> None:
        for frame in frames:
            self.process_frame(frame)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._gen += 1  # anything still streaming is now stale
            brain = self._brain
        # Interrupt first so a worker blocked inside brain.ask drains to its
        # TurnEnd and can see the shutdown sentinel.
        if brain is not None:
            brain.interrupt()
        self._prompts.put(None)
        self._player.stop()
        self._worker.join(timeout=5.0)
        if brain is not None:
            brain.close()
        with self._lock:
            leftover, self._brain = self._brain, None
        if leftover is not None and leftover is not brain:
            leftover.close()
        self._player.close()

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
                and self._is_current(gen)
            ):
                self._listener.listen_now(self._follow_up_s)

    def _on_listener_event(self, event: ListenerEvent) -> None:
        if event.kind == "wake":
            self._talk_over_turn = False
            self._cue("wake")
            self._maybe_barge_in()
        elif event.kind in ("talk_over", "talk_over_stop"):
            if event.kind == "talk_over":
                self._talk_over_turn = True
                # Snapshot before the barge-in bumps the generation.
                with self._lock:
                    self._echo_ref = (
                        self._spoken_text if self._spoken_gen == self._gen else ""
                    )
            self._maybe_barge_in()  # no cue: the user is already talking
        elif event.kind == "follow_up":
            self._talk_over_turn = False
            self._cue("follow_up")
        elif event.kind == "utterance":
            text = event.text or ""
            if self._talk_over_turn and self._echo_ref:
                kept = strip_echo(text, self._echo_ref)
                if not re.search(r"[a-z0-9]", kept, re.IGNORECASE):
                    self._print(f'[ignored as my own reply: "{text}"]\n')
                    return
                if kept != text:
                    self._print(f'[removed my own words: "{text}" -> "{kept}"]\n')
                    text = kept
            self._print(f"you: {text}\n")
            now = self._clock()
            t_speech_end = now - (event.speech_end_to_text_ms or 0.0) / 1000.0
            with self._lock:
                self._gen += 1
                gen = self._gen
                self._in_flight += 1
            self._prompts.put((gen, text, now, t_speech_end))
        # wake_no_speech / dropped_empty: nothing to do

    def _maybe_barge_in(self) -> None:
        # One locked section: decide AND bump, so a worker enqueue can't slip
        # between the check and the bump (lock order is session, then player).
        with self._lock:
            if not (self._in_flight > 0 or self._player.busy):
                return
            self._gen += 1
            brain = self._brain
        # Outside the lock: stop() may wait up to 2 s for `say` to die, and
        # interrupt() is a no-op on an idle or dead brain.
        self._player.stop()
        if brain is not None:
            brain.interrupt()
        self._print("[interrupted]\n")

    # ---- shared helpers ------------------------------------------------

    def _is_current(self, gen: int) -> bool:
        with self._lock:
            return gen == self._gen

    def _print(self, text: str) -> None:
        with self._print_lock:
            self._out.write(sanitize_terminal(text))
            self._out.flush()

    def _enqueue_if_current(self, gen: int, sentences: list[str]) -> bool:
        """Enqueue under the lock the barge-in bump takes (see module doc)."""
        with self._lock:
            if gen != self._gen:
                return False
            if sentences:
                if self._spoken_gen != gen:
                    self._spoken_gen, self._spoken_text = gen, ""
                self._spoken_text += " ".join(sentences) + " "
            for sentence in sentences:
                self._player.enqueue(sentence)
            return True

    def _on_speech_start(self, text: str) -> None:
        """Player thread: record when the turn's first chunk starts speaking.

        Records only -- printing here would land mid-way through the streamed
        reply; the worker prints the latency line once the reply is finished.
        """
        del text
        now = self._clock()
        with self._lock:
            turn = self._turn
            if turn is not None and turn.t_first_say is None and turn.gen == self._gen:
                turn.t_first_say = now

    def _print_latency(self, turn: _Turn) -> None:
        with self._lock:
            if turn.t_first_say is None:
                return
            to_say = turn.t_first_say - turn.t_speech_end
            first = (
                f"{turn.t_first_token - turn.t_submit:.2f} s"
                if turn.t_first_token is not None
                else "n/a"
            )
        self._print(
            f"[latency] speech end \u2192 first say: {to_say:.2f} s (first token {first})\n"
        )

    # ---- worker thread -----------------------------------------------

    def _worker_loop(self) -> None:
        while True:
            item = self._prompts.get()
            if item is None:
                return
            gen, text, t_submit, t_speech_end = item
            try:
                if self._is_current(gen):
                    self._serve(gen, text, t_submit, t_speech_end)
            except Exception as exc:  # keep the loop alive for the next utterance
                self._print(f"[error] {type(exc).__name__}: {exc}\n")
            finally:
                with self._lock:
                    self._in_flight -= 1

    def _ensure_brain(self) -> BrainLike:
        with self._lock:
            brain = self._brain
            last = self._last_session_id
        if brain is not None and not brain.alive:
            brain.close()
            brain = None
        if brain is None:
            with self._lock:
                self._resume_unconfirmed = last is not None
            brain = self._brain_factory(last)
        with self._lock:
            self._brain = brain
        return brain

    def _drop_brain(self) -> None:
        with self._lock:
            brain, self._brain = self._brain, None
        if brain is not None:
            try:
                brain.close()
            except Exception:
                pass

    def _serve(self, gen: int, text: str, t_submit: float, t_speech_end: float) -> None:
        turn = _Turn(gen=gen, t_submit=t_submit, t_speech_end=t_speech_end)
        with self._lock:
            self._turn = turn
        self._chunker.reset()
        interrupted = False
        try:
            brain = self._ensure_brain()
            for event in brain.ask(text):
                current = self._is_current(gen)
                if isinstance(event, TextDelta):
                    if not current:
                        continue
                    if turn.t_first_token is None:
                        turn.t_first_token = self._clock()
                    self._print(event.text)
                    self._enqueue_if_current(gen, self._chunker.feed(event.text))
                elif isinstance(event, ToolUse):
                    if current:
                        self._enqueue_if_current(gen, self._chunker.flush())
                        self._print(f"\n[tool: {event.name}]\n")
                elif isinstance(event, TurnEnd):
                    interrupted = event.interrupted
                    with self._lock:
                        self._resume_unconfirmed = False
                    if event.session_id:
                        with self._lock:
                            self._last_session_id = event.session_id
                        self._save_session(event.session_id)
                    if current:
                        self._enqueue_if_current(gen, self._chunker.flush())
                        self._print("\n")
        except BrainError as exc:
            self._print(f"\n[error] {exc}\n")
            with self._lock:
                resume_failed = self._resume_unconfirmed
                if resume_failed:
                    self._resume_unconfirmed = False
                    self._last_session_id = None
            if resume_failed:
                self._print("[could not resume the saved session; starting a fresh one]\n")
            if self._enqueue_if_current(gen, [APOLOGY]):
                self._player.wait_idle()
            self._drop_brain()
            return

        if interrupted or not self._is_current(gen):
            return
        self._player.wait_idle()
        if self._is_current(gen):
            self._print_latency(turn)
            self._control.put(("reply_done", gen))

"""Command-line entry point: `eridian <command>`."""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import getpass
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import control, paths, persona, wake


_WAKE_CUE = "/System/Library/Sounds/Tink.aiff"
_FOLLOW_UP_CUE = "/System/Library/Sounds/Pop.aiff"

# Commands that use the wake word; a bad ERIDIAN_WAKE_MODEL stops them at startup.
_WAKE_COMMANDS = frozenset({"attach", "chat", "launch", "listen", "session"})


def _make_wake_factory(
    *, probability_cutoff: float | None = None
) -> Callable[[], Callable[[bytes], bool]]:
    # Lazy import: pymicro-wakeword is heavy and only the voice commands need it.
    from pymicro_wakeword import MicroWakeWordFeatures

    configured = wake.current()  # validated and copied privately once; every load reads the copy
    # Fail closed at startup if the model won't load; the first detector reuses this load.
    preloaded = [configured.load_model()]

    def wake_factory():
        mww = preloaded.pop() if preloaded else configured.load_model()
        if probability_cutoff is not None:
            # A custom model may run a live cutoff below the screen's: never screen looser than live.
            mww.probability_cutoff = (
                probability_cutoff
                if configured.is_default
                else min(probability_cutoff, mww.probability_cutoff)
            )
        feats = MicroWakeWordFeatures()

        def detect(frame: bytes) -> bool:
            for processed in feats.process_streaming(frame):
                if mww.process_streaming(processed):
                    return True
            return False

        return detect

    return wake_factory


def _play_cue(sound_path: str) -> None:
    subprocess.Popen(
        ["afplay", sound_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class WorkdirError(Exception):
    """The private chat workspace can't be used safely."""


_ATTACH_LOG_ROTATE_BYTES = 1_000_000  # attach.log rotates once, at spawn
_SENSITIVE_HOME_DIRS = ("Documents", "Desktop", "Downloads")


def _too_broad_or_sensitive(r: Path, h: Path) -> bool:
    if h.is_relative_to(r):  # home itself, any ancestor of it, and `/`
        return True
    if not r.is_relative_to(h):
        return False
    rel = r.relative_to(h).parts
    if rel[0].startswith("."):  # .ssh, .aws, .gnupg, .claude, .config, ...
        return True
    if rel[0] == "Library":
        return True
    # The top-level personal folders exactly; their subdirectories are fine.
    return len(rel) == 1 and rel[0] in _SENSITIVE_HOME_DIRS


def chat_workdir(cwd: Path, home: Path, eridian_home: Path) -> tuple[Path, bool]:
    """Pick the (resolved) directory Claude's file tools are confined to.

    Home, its ancestors, `/`, dot-directories and Library under home, and the
    top-level Documents/Desktop/Downloads are too broad or sensitive to expose
    to voice-driven file reads, so those fall back to a private workspace under
    ~/.eridian. Returns (workdir, substituted); raises WorkdirError if the
    workspace path is a symlink.
    """
    resolved = cwd.resolve()
    if not _too_broad_or_sensitive(resolved, home.resolve()):
        return resolved, False
    workspace = eridian_home / "workspace"
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    if workspace.is_symlink():
        raise WorkdirError(f"{workspace} is a symlink; refusing to use it as the chat workspace")
    workspace.chmod(0o700)
    return workspace.resolve(), True


class AudioIO:
    """The audio plumbing a voice command runs on: frame source + speech player."""

    def __init__(
        self,
        *,
        player_factory: Callable[..., object],
        talk_over: bool,
        engine: object | None = None,
        voice_label: str = "",
    ) -> None:
        self.player_factory = player_factory
        self.talk_over = talk_over
        self.engine = engine
        self.voice_label = voice_label
        self._mic: object | None = None  # the no-AEC MicSource, created lazily by frames()

    def _source(self):
        """The active frame source: the AEC engine, or the plain mic when there is none."""
        if self.engine is not None:
            return self.engine
        if self._mic is None:
            from . import audio

            self._mic = audio.MicSource()
        return self._mic

    def frames(self):
        return self._source().frames()

    def start(self, input_enabled: bool = True) -> None:
        # The plain mic has no separate start: frames() opens it on first use.
        if self.engine is not None:
            self.engine.start(input_enabled=input_enabled)
        else:
            self._source().start(input_enabled=input_enabled)

    def set_input_enabled(self, enabled: bool) -> None:
        """Open or close the mic device; frames keep flowing (silent) while it is closed."""
        self._source().set_input_enabled(enabled)

    @property
    def stalled(self) -> bool:
        """True while the active source yields silence because the open mic delivers nothing."""
        return bool(self._source().stalled)

    def close(self) -> None:
        if self.engine is not None:
            self.engine.close()
        elif self._mic is not None:
            self._mic.close()


def _say_voices() -> tuple[str, str]:
    """(reply voice, eridian's own voice) for `say`, with the same defaults build_say_argv uses."""
    return (
        os.environ.get("ERIDIAN_VOICE") or "Evan (Enhanced)",
        os.environ.get("ERIDIAN_OWN_VOICE") or "Samantha",
    )


def _say_voice_label() -> str:
    reply, own = _say_voices()
    return f"say ({reply}; eridian's own voice: {own})"


class OwnVoiceError(Exception):
    """eridian's own voice is part of Claude's voice: read-backs would be indistinguishable."""


def _same_say_voice_message(name: str) -> str:
    return (
        f"eridian's own voice is the same as the reply voice ({name}); "
        "set ERIDIAN_VOICE or ERIDIAN_OWN_VOICE so they differ"
    )


def _load_kokoro_voice():
    from .kokoro import KokoroVoice

    voice = KokoroVoice()
    voice.warm_up()
    return voice


def make_audio_io(
    no_aec: bool,
    *,
    tts: str = "kokoro",
    engine_factory: Callable[[], object] | None = None,
    screen_factory: Callable[[], Callable[[bytes], bool]] | None = None,
    kokoro_factory: Callable[[], object] | None = None,
    err=sys.stderr,
) -> AudioIO:
    """Echo-cancelled full-duplex audio by default; plain mic + `say` with no_aec.

    On the AEC path the voice is Kokoro unless tts == "say" or Kokoro is
    unavailable (then it falls back to `say` with one line on `err`).
    """
    from .player import KokoroRenderer, RenderedSpeechQueue, SayRenderer, SpeechQueue
    from .screen import SCREEN_CUTOFF, ScreenedSink

    if no_aec:
        reply, own = _say_voices()
        if reply == own:
            raise OwnVoiceError(_same_say_voice_message(reply))
        return AudioIO(
            player_factory=lambda on_start=None: SpeechQueue(on_start),
            talk_over=False,
            voice_label=_say_voice_label(),
        )

    voice = None
    if tts == "kokoro":
        from .kokoro import KokoroUnavailable

        try:
            espeak = os.environ.get("ERIDIAN_ESPEAK_CMD", "espeak-ng")
            if shutil.which(espeak) is None:
                raise KokoroUnavailable("espeak-ng not found; install it with `brew install espeak-ng`")
            voice = (kokoro_factory or _load_kokoro_voice)()
        except KokoroUnavailable as exc:
            print(f"eridian: Kokoro voice unavailable ({exc}); using say", file=err, flush=True)

    if voice is not None:
        if voice.own_voice in voice.voice_names:
            # Fail closed: the confirm read-back is only trustworthy if it sounds unlike Claude.
            raise OwnVoiceError(
                f"eridian's own voice ({voice.own_voice}) is part of the reply voice "
                f"({voice.voice}); set ERIDIAN_KOKORO_OWN_VOICE or ERIDIAN_KOKORO_VOICE "
                "so they differ"
            )
    else:
        reply, own = _say_voices()
        if reply == own:
            raise OwnVoiceError(_same_say_voice_message(reply))

    if engine_factory is None:
        from .duplex import DuplexAudio as engine_factory
    engine = engine_factory()

    def on_blocked() -> None:
        print("[skipped speech that sounded like the wake word]", file=sys.stderr, flush=True)

    def on_error(exc: BaseException) -> None:
        print(
            f"eridian: the wake-word screen failed ({exc}); speech stays muted until restart",
            file=err,
            flush=True,
        )

    sink = ScreenedSink(
        engine,
        detector_factory=screen_factory or _make_wake_factory(probability_cutoff=SCREEN_CUTOFF),
        on_blocked=on_blocked,
        on_error=on_error,
    )
    return AudioIO(
        player_factory=lambda on_start=None: RenderedSpeechQueue(
            on_start, sink=sink, renderer=KokoroRenderer(voice) if voice else SayRenderer()
        ),
        talk_over=True,
        engine=engine,
        voice_label=(
            f"Kokoro ({voice.voice}; eridian's own voice: {voice.own_voice})"
            if voice
            else _say_voice_label()
        ),
    )


def banner_line(io: AudioIO) -> str:
    if io.talk_over:
        line = "Echo cancellation on: talk over a reply to stop it."
    else:
        line = f"Echo cancellation off: say '{wake.current().phrase}' to stop a reply."
    return f"{line} Voice: {io.voice_label}."


AUDIO_START_ERROR = (
    "eridian: cannot open the audio devices for echo cancellation: {exc}. Try --no-aec."
)


def _cmd_listen(args: argparse.Namespace) -> int:
    # Imported lazily -- these pull in numpy/sounddevice/pymicro-wakeword/
    # pysilero-vad/parakeet-mlx, which other commands (notably `hook stop`)
    # must stay fast and dependency-light without.
    import time

    from . import audio
    from .listener import Listener, ListenerEvent
    from .stt import ModelNotCachedError, ParakeetSTT

    try:
        stt = ParakeetSTT()
    except ModelNotCachedError as exc:
        print(f"eridian: {exc}", file=sys.stderr)
        return 1

    from pysilero_vad import SileroVoiceActivityDetector

    vad = SileroVoiceActivityDetector()
    wake_factory = _make_wake_factory()

    def on_event(event: ListenerEvent) -> None:
        if event.kind == "wake" and not args.no_beep:
            _play_cue(_WAKE_CUE)
        print(json.dumps(dataclasses.asdict(event)), flush=True)

    listener = Listener(
        wake_factory=wake_factory,
        vad=lambda chunk: vad(chunk),
        stt=stt,
        clock=time.monotonic,
        on_event=on_event,
        vad_reset=vad.reset,
    )
    listener.run(audio.frame_source())
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    # Lazy imports for the same reason as `listen`.
    import time

    from . import brain
    from .chat import ChatSession
    from .stt import ModelNotCachedError, ParakeetSTT

    try:
        stt = ParakeetSTT()
    except ModelNotCachedError as exc:
        print(f"eridian: {exc}", file=sys.stderr)
        return 1

    from pysilero_vad import SileroVoiceActivityDetector

    vad = SileroVoiceActivityDetector()

    try:
        workdir, substituted = chat_workdir(Path.cwd(), Path.home(), paths.home_dir())
    except WorkdirError as exc:
        print(f"eridian: {exc}", file=sys.stderr)
        return 1

    def brain_factory(resume: str | None) -> brain.ClaudeBrain:
        b = brain.ClaudeBrain(workdir, resume_session_id=resume)
        b.start()
        return b

    cues = {"wake": _WAKE_CUE, "follow_up": _FOLLOW_UP_CUE}

    try:
        io = make_audio_io(args.no_aec, tts=args.tts)
    except OwnVoiceError as exc:
        print(f"eridian: {exc}", file=sys.stderr)
        return 1
    session = ChatSession(
        wake_factory=_make_wake_factory(),
        vad=lambda chunk: vad(chunk),
        stt=stt,
        clock=time.monotonic,
        brain_factory=brain_factory,
        resume_session_id=brain.load_session_id() if args.resume else None,
        vad_reset=vad.reset,
        follow_up_s=args.follow_up,
        cue=None if args.no_beep else (lambda kind: _play_cue(cues[kind])),
        player_factory=io.player_factory,
        talk_over=io.talk_over,
    )

    print(f"eridian chat: working in {workdir}")
    if substituted:
        print(
            f"  ({Path.cwd()} is too broad or too sensitive to expose to "
            "voice-driven file reads, so a private workspace is used instead)"
        )
    print(
        "Claude can read and search files under it and search the web; "
        "it cannot run commands or edit files."
    )
    print(banner_line(io))
    print(f"Say '{wake.current().phrase}'. Ctrl-C to quit.", flush=True)
    try:
        try:
            io.start()
        except Exception as exc:  # noqa: BLE001 - any device/engine failure
            print(AUDIO_START_ERROR.format(exc=exc), file=sys.stderr)
            return 1
        session.run(io.frames())
    except KeyboardInterrupt:
        pass
    finally:
        # Session first: that stops the player, which clears the sink.
        session.close()
        io.close()
    return 0


def channel_mcp_config(executable: str, home: Path, token: str) -> dict:
    """MCP config that launches the channel server with the same ERIDIAN_HOME.

    The channel gets the resolved wake PHRASE (never the model path): it only needs
    the words for its instructions, and this keeps it from loading the manifest.
    """
    env = {"ERIDIAN_HOME": str(home), "ERIDIAN_SESSION": token}
    phrase = wake.current()
    if not phrase.is_default:
        env[wake.PHRASE_ENV] = phrase.phrase
    persona_name = persona.resolve()
    if persona_name:
        env[persona.PERSONA_ENV] = persona_name
    return {
        "mcpServers": {
            "eridian": {
                "command": executable,
                # -P: keep the session cwd off sys.path so a hostile checkout can't shadow us.
                "args": ["-P", "-m", "eridian.cli", "channel"],
                "env": env,
            }
        }
    }


def voice_guard_settings(executable: str, home: Path, token: str) -> dict:
    """Inline `--settings` JSON that installs the voice guard's hooks and the announce hooks for one session."""
    from . import voice_guard

    def _hook_cmd(event: str) -> str:
        parts = [
            executable, "-P", "-m", "eridian.voice_guard", event,
            "--home", str(home), "--session", token,
        ]
        return " ".join(shlex.quote(x) for x in parts)

    def _announce(event: str) -> dict:
        parts = [
            executable, "-P", "-m", "eridian.announce", event,
            "--home", str(home), "--session", token,
        ]
        # Async and output-free: an announce hook can never block Claude or decide a permission.
        return {
            "type": "command",
            "command": " ".join(shlex.quote(x) for x in parts),
            "async": True,
            "timeout": 10,
        }

    # Claude never needs the safe-phrase hash; deny its Read tool on every turn, typed
    # included. `//path` is gitignore-style for an absolute path from the filesystem root.
    abs_home = os.path.abspath(home)
    return {
        "permissions": {"deny": [f"Read(/{abs_home}/{voice_guard.SAFE_PHRASE_FILE})"]},
        # --settings outranks project/local/user settings, so this forces hooks on.
        "disableAllHooks": False,
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": _hook_cmd("sessionstart"), "timeout": 30}]}
            ],
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": _hook_cmd("userpromptsubmit"), "timeout": 30}]}
            ],
            # No `|| exit 2` on Stop/SubagentStop: exit 2 keeps Claude / the subagent running.
            "SubagentStart": [
                {"hooks": [{"type": "command", "command": _hook_cmd("subagentstart"), "timeout": 30}]}
            ],
            "SubagentStop": [
                {"hooks": [{"type": "command", "command": _hook_cmd("subagentstop"), "timeout": 30}]}
            ],
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": _hook_cmd("stop"), "timeout": 30},
                        _announce("stop"),
                    ]
                }
            ],
            "StopFailure": [{"hooks": [_announce("stopfailure")]}],
            "PermissionRequest": [{"matcher": "*", "hooks": [_announce("permissionrequest")]}],
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            # Only exit 2 blocks; a missing interpreter or crash (1/127) must block too.
                            "command": _hook_cmd("pretooluse") + " || exit 2",
                            # A timeout fails open; this path takes no lock, so it is fast.
                            "timeout": 10,
                        }
                    ],
                }
            ],
        },
    }


def build_session_argv(
    mcp_json: str, settings_json: str, rest: Sequence[str], claude_cmd: str = "claude"
) -> list[str]:
    """argv for an interactive claude session with the eridian channel loaded.

    The reply tool is pre-approved, so a voice-driven turn can speak without a
    permission prompt but nothing else is auto-allowed. The voice guard's hooks
    are injected via --settings.
    """
    return [
        claude_cmd,
        "--mcp-config",
        mcp_json,
        "--settings",
        settings_json,
        "--dangerously-load-development-channels",
        "server:eridian",
        "--allowedTools",
        "mcp__eridian__reply",
        *rest,
    ]


def _cmd_channel(args: argparse.Namespace) -> int:
    del args
    from . import mcp_channel

    return mcp_channel.main()


_REFUSED_SESSION_FLAGS = ("--settings", "--bare", "--safe-mode")
_HOOK_DISABLING_ENV = ("CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_SAFE_MODE")


def _cmd_session(args: argparse.Namespace) -> int:
    rest = list(args.claude_args)
    if rest[:1] == ["--"]:
        rest = rest[1:]
    return _exec_session(rest, warn_no_voice=True)


def _persona_refused() -> bool:
    """Print the error and return True if ERIDIAN_PERSONA is invalid (voice sessions only)."""
    try:
        persona.resolve()
    except persona.PersonaError as exc:
        print(f"eridian: {exc}", file=sys.stderr)
        return True
    return False


def _exec_session(rest: list[str], *, warn_no_voice: bool) -> int:
    """Exec claude wired to the voice channel; returns only on a refused flag or bad persona."""
    if _persona_refused():
        return 1
    for arg in rest:
        if arg in _REFUSED_SESSION_FLAGS or arg.startswith("--settings="):
            flag = arg.split("=", 1)[0]
            print(
                f"eridian: remove {flag}: eridian session sets the voice guard's "
                "settings itself",
                file=sys.stderr,
            )
            return 2
    token = secrets.token_hex(16)
    home = paths.home_dir()
    mcp_json = json.dumps(channel_mcp_config(sys.executable, home, token))
    settings_json = json.dumps(voice_guard_settings(sys.executable, home, token))
    if warn_no_voice and not _voice_socket_present(paths.voice_socket_path()):
        print(
            "eridian: no voice process is listening -- start `eridian attach` "
            "in another terminal to talk",
            file=sys.stderr,
        )
    claude_cmd = os.environ.get("ERIDIAN_CLAUDE_CMD") or "claude"
    argv = build_session_argv(mcp_json, settings_json, rest, claude_cmd)
    # Replies reach the user through the channel's `reply` tool.
    # Scrub the env vars that --bare / --safe-mode set, since they may disable hooks.
    env = {k: v for k, v in os.environ.items() if k not in _HOOK_DISABLING_ENV}
    os.execvpe(claude_cmd, argv, env)
    return 1  # unreachable: execvpe replaces the process


# Subcommands of `claude`, read from `claude --help` on Claude Code 2.1.311.
_CLAUDE_SUBCOMMANDS = frozenset(
    {
        "agents", "attach", "auth", "auto-mode", "doctor", "gateway", "import",
        "install", "logs", "mcp", "plugin", "plugins", "purge", "respawn", "rm",
        "setup-token", "stop", "kill", "ultrareview", "update", "upgrade",
    }
)  # fmt: skip
_PASSTHROUGH_FLAGS = frozenset(
    {"-p", "--print", "-h", "--help", "-v", "--version", *_REFUSED_SESSION_FLAGS}
)


def launch_mode(
    argv: Sequence[str],
    env: Mapping[str, str],
    stdin_tty: bool,
    stdout_tty: bool,
) -> str:
    """Return "voice" to start claude with the voice channel, else "passthrough" (plain claude).

    A claude subcommand anywhere among the non-option args passes through too: an
    option's value can precede it (`--model x mcp list`), and a stray prompt that
    happens to equal a subcommand name failing safe to plain claude is harmless.
    """
    if env.get("ERIDIAN_AUTOLAUNCH") == "0" or not (stdin_tty and stdout_tty):
        return "passthrough"
    for arg in argv:
        if arg in _PASSTHROUGH_FLAGS or arg.startswith("--settings="):
            return "passthrough"
        if not arg.startswith("-") and arg in _CLAUDE_SUBCOMMANDS:
            return "passthrough"
    return "voice"


def _ps_command(pid: int) -> str:
    """The command line of pid per `ps`, or "" if it cannot be read."""
    try:
        return subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _pid_is_attach(pid: int) -> bool:
    """True if pid is alive and its command line is an eridian attach."""
    out = _ps_command(pid)
    # Match tokens, not substrings: `tail -f ~/.eridian/attach.log` must not pass.
    return bool(
        re.search(r"(?:^|/)eridian attach(?:\s|$)|-m eridian attach(?:\s|$)", out)
    )


def _ensure_attach() -> None:
    """Spawn a background `eridian attach` unless one answers or is still starting.

    flock serializes terminals starting together. The child's pid is written to the
    pidfile at spawn, so a second terminal during the model load sees a live attach
    instead of spawning another. Attach is started from the terminal's process tree
    (not launchd) so it can use the terminal app's mic permission; that still needs
    a live check. Failures are warned about, never raised: claude still starts.
    """
    from . import voice

    try:
        _spawn_attach_locked(voice)
    except OSError as exc:
        print(f"eridian: could not start voice in the background: {exc}", file=sys.stderr)


def _spawn_attach_locked(voice) -> None:
    home = paths.home_dir()
    lock_fd = os.open(
        paths.attach_lock_path(), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if voice._server_answers(paths.voice_socket_path()):
            return
        pid = paths.read_attach_pid()
        if pid is not None and _pid_is_attach(pid):
            return
        log_path = paths.attach_log_path()
        try:
            oversized = log_path.lstat().st_size >= _ATTACH_LOG_ROTATE_BYTES
        except OSError:
            oversized = False  # absent or unstattable: just open it
        if oversized:
            try:
                os.replace(log_path, log_path.with_name(log_path.name + ".1"))
            except OSError as exc:
                print(f"eridian: could not rotate {log_path}: {exc}", file=sys.stderr)
        log_fd = os.open(
            log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
        try:
            os.fchmod(log_fd, 0o600)  # tighten a pre-existing log
            child = subprocess.Popen(
                [sys.executable, "-m", "eridian", "attach"],
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,
                cwd=home,
            )
        finally:
            os.close(log_fd)
        paths.write_attach_pid(child.pid)
        print(
            f"eridian: starting voice in the background (log: {log_path})",
            file=sys.stderr,
        )
    finally:
        os.close(lock_fd)  # closing releases the flock


def _cmd_launch(args: argparse.Namespace) -> int:
    rest = list(args.claude_args)
    if rest[:1] == ["--"]:
        rest = rest[1:]
    mode = launch_mode(rest, os.environ, sys.stdin.isatty(), sys.stdout.isatty())
    if mode == "voice":
        if _persona_refused():  # before spawning attach; passthrough ignores the persona
            return 1
        _ensure_attach()
        return _exec_session(rest, warn_no_voice=False)
    claude_cmd = os.environ.get("ERIDIAN_CLAUDE_CMD") or "claude"
    os.execvpe(claude_cmd, [claude_cmd, *rest], os.environ)
    return 1  # unreachable: execvpe replaces the process


def _cmd_stop(args: argparse.Namespace) -> int:
    del args
    pid = paths.read_attach_pid()
    if pid is None:
        print("eridian: no attach pidfile; nothing to stop")
        return 0
    if not _pid_is_attach(pid):
        print(
            f"eridian: pid {pid} is not an eridian attach (stale pidfile?); "
            "not signalling it",
            file=sys.stderr,
        )
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"eridian: could not stop attach (pid {pid}): {exc}", file=sys.stderr)
        return 1
    print(f"eridian: sent SIGTERM to attach (pid {pid})")
    return 0


_SESSION_ID_RE = re.compile(r"^(?:[0-9a-f]{12}|[0-9]{1,10}|pid-[0-9]{1,10})$")
_NOT_SAVED_WARNING = (
    "Voice settings aren't saved; an off session or mute may turn back on if attach restarts."
)
_STATE_ERROR_NOTE = (
    "Saved voice settings were unreadable, so every session is off and the mic is "
    "muted until you turn one on."
)
_UNMUTE_POLLS = 5
_UNMUTE_POLL_S = 0.2
_sleep = time.sleep  # seam: tests don't wait


_PS_BUDGET_S = 3.0  # total, for the whole ancestry walk


def _ppid_of(pid: int, deadline: float) -> int | None:
    """Parent pid of `pid` via ps, or None when it can't be read in time."""
    left = deadline - time.monotonic()
    if left <= 0:
        return None
    try:
        out = subprocess.run(
            ["/bin/ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True, text=True, timeout=left, check=False,
        ).stdout.strip()
        return int(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _own_ancestors(deadline: float) -> list[int]:
    """This process's ancestors, nearest first."""
    chain: list[int] = []
    pid = os.getppid()
    while pid > 1 and pid not in chain and len(chain) < 64:
        chain.append(pid)
        nxt = _ppid_of(pid, deadline)
        if nxt is None:
            break
        pid = nxt
    return chain


def _this_session(sessions: list) -> list[dict]:
    """Sessions whose channel server is a child of our nearest matching ancestor."""
    deadline = time.monotonic() + _PS_BUDGET_S
    by_parent: dict[int, list[dict]] = {}
    for sess in sessions:
        if not isinstance(sess, dict):
            continue
        pid = sess.get("pid")
        if not isinstance(pid, int):
            continue
        ppid = _ppid_of(pid, deadline)
        if ppid is None:  # budget spent or ps failed: can't rule this session in or out
            return []
        by_parent.setdefault(ppid, []).append(sess)
    for anc in _own_ancestors(deadline):
        if anc in by_parent:
            return by_parent[anc]
    return []


def _ctl_fail(message: str, rc: int = 1) -> int:
    print(f"eridian: {message[:1].upper()}{message[1:]}", file=sys.stderr)
    return rc


def _ctl_status() -> dict:
    reply = control.request({"op": "status"})
    if reply.get("ok") is not True or not isinstance(reply.get("sessions"), list):
        raise control.ControlError("Attach sent an unexpected status reply")
    return reply


def _warn_if_unsaved() -> None:
    try:
        reply = _ctl_status()
    except control.ControlError:
        return
    if reply.get("saved") is not True:
        print(f"eridian: warning: {_NOT_SAVED_WARNING}", file=sys.stderr)


def _cmd_voice_status(args: argparse.Namespace) -> int:
    del args
    try:
        reply = _ctl_status()
    except control.ControlError as exc:
        return _ctl_fail(str(exc))
    print(f"muted: {'no' if reply.get('muted') is False else 'yes'}")
    sessions = [s for s in reply["sessions"] if isinstance(s, dict)]
    if not sessions:
        print("No Claude sessions are attached.")
    for sess in sessions:
        print(
            f"{'*' if sess.get('target') is True else ' '} {'on ' if sess.get('on') is True else 'off'} "
            f"{control.clean(sess.get('id', '?'), 16)} pid {control.clean(sess.get('pid', '?'), 12)} "
            f"{control.clean(sess.get('cwd', ''))}"
        )
    if reply.get("saved") is not True:
        print(_NOT_SAVED_WARNING)
    if reply.get("state_error") is not False:
        print(_STATE_ERROR_NOTE)
    return 0


def _cmd_voice_set(args: argparse.Namespace, on: bool) -> int:
    wanted = args.session
    if wanted is not None and not _SESSION_ID_RE.fullmatch(wanted):
        return _ctl_fail("Bad session id; use an id or pid from `eridian voice status`", 2)
    try:
        if wanted is None:
            found = _this_session(_ctl_status()["sessions"])
            if len(found) > 1:
                print(
                    "eridian: Several sessions match; pass --session "
                    "(see eridian voice status)",
                    file=sys.stderr,
                )
                return 2
            sess = found[0] if found else None
            if sess is None or not isinstance(sess.get("id"), str):
                print(
                    "eridian: Couldn't tell which session this is; pass --session "
                    "(see eridian voice status)",
                    file=sys.stderr,
                )
                return 2
            wanted = sess["id"]
            label = f"{control.clean(wanted, 16)} (pid {control.clean(sess.get('pid'), 12)})"
        else:
            label = control.clean(wanted, 16)
        reply = control.request({"op": "voice_on" if on else "voice_off", "session": wanted})
    except control.ControlError as exc:
        return _ctl_fail(str(exc))
    if reply.get("ok") is not True:
        err = control.clean(reply.get("error", "request failed"))
        return _ctl_fail(err, 2 if reply.get("error") == "no such session" else 1)
    print(f"Voice {'on' if on else 'off'} for session {label}.")
    if not on:
        _warn_if_unsaved()
    return 0


def _cmd_voice_on(args: argparse.Namespace) -> int:
    return _cmd_voice_set(args, True)


def _cmd_voice_off(args: argparse.Namespace) -> int:
    return _cmd_voice_set(args, False)


def _cmd_mute(args: argparse.Namespace) -> int:
    del args
    try:
        reply = control.request({"op": "mute"})
    except control.ControlError as exc:
        return _ctl_fail(str(exc))
    if reply.get("ok") is not True:
        return _ctl_fail(control.clean(reply.get("error", "request failed")))
    print("Muted.")
    _warn_if_unsaved()
    return 0


def _cmd_unmute(args: argparse.Namespace) -> int:
    del args
    try:
        reply = control.request({"op": "unmute"})
        if reply.get("ok") is not True:
            return _ctl_fail(control.clean(reply.get("error", "request failed")))
        # ok only means accepted: the mic reopens on the next frame and may fail and
        # re-mute, so watch status for the whole window, not just until it first reads unmuted.
        muted = False
        for _ in range(_UNMUTE_POLLS):
            _sleep(_UNMUTE_POLL_S)
            muted = _ctl_status().get("muted") is not False  # fail safe: only a real False is unmuted
            if muted:
                break
    except control.ControlError as exc:
        return _ctl_fail(str(exc))
    if muted:
        return _ctl_fail(
            "Muted again: the mic may have failed to reopen, or something muted it"
        )
    print("Unmuted.")
    return 0


def _cmd_shell_init(args: argparse.Namespace) -> int:
    del args
    exe = Path(sys.argv[0]).absolute()
    if exe.name != "eridian":  # e.g. `python -m eridian`: use the venv script
        exe = Path(sys.executable).absolute().parent / "eridian"
    fn = f"claude() {{ {shlex.quote(str(exe))} launch -- \"$@\"; }}"
    print(
        "# `command claude` bypasses this function and runs plain claude.\n"
        f"{fn}\n"
        "# cmux redefines claude() at the first prompt (its _cmux_fix_path precmd hook); "
        "re-assert ours once, after it.\n"
        "_eridian_claude_precmd() {\n"
        f"  {fn}\n"
        "  add-zsh-hook -d precmd _eridian_claude_precmd\n"
        "}\n"
        "autoload -Uz add-zsh-hook\n"
        "add-zsh-hook precmd _eridian_claude_precmd"
    )
    return 0


def _voice_socket_present(sock_path: Path) -> bool:
    """True if a socket file exists at sock_path.

    Deliberately not a connect(): a probe connection would look like a session
    attaching to the voice process.
    """
    try:
        return stat.S_ISSOCK(sock_path.stat().st_mode)
    except OSError:
        return False


def _symlink_in(path: Path) -> Path | None:
    """The first component of path (itself included) that is a symlink, else None.

    Walks every component, anchored or relative. A missing component ends the
    walk (nothing below it exists); any other lstat error propagates as OSError.
    """
    probe = Path(path.anchor)
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        probe = probe / part
        try:
            if stat.S_ISLNK(os.lstat(probe).st_mode):
                return probe
        except FileNotFoundError:
            return None
    return None


def _pid_is_legacy_attach(pid: int) -> bool:
    """True if pid is alive and runs a pre-rename `undertone attach`."""
    return bool(
        re.search(
            r"(?:^|/)undertone attach(?:\s|$)|-m undertone attach(?:\s|$)",
            _ps_command(pid),
        )
    )


def _attach_live_in(folder: Path) -> str | None:
    """Why folder looks like it has a running attach, else None (no connect)."""
    try:
        pid = int((folder / "attach.pid").read_text().strip())
    except (OSError, ValueError):
        pid = None
    if pid is not None and (_pid_is_attach(pid) or _pid_is_legacy_attach(pid)):
        return f"an attach is running (pid {pid}; run `eridian stop` first)"
    sock = folder / "voice.sock"
    if _voice_socket_present(sock):
        return (
            f"{sock} exists, so an attach may be running; if no attach is "
            f"running, remove {sock} and retry"
        )
    return None


def _migrate(source: Path, target: Path, *, dry_run: bool) -> int:
    """Fail-closed, atomic move of the pre-rename state folder to target.

    Refuses (exit 1, nothing changed) on symlinks, wrong owner, a non-empty
    target, a running attach, or a cross-filesystem move. The only thing ever
    removed is an empty target directory.
    """

    def refuse(msg: str) -> int:
        print(f"eridian: migrate refused: {msg}", file=sys.stderr)
        return 1

    if not target.is_absolute():
        return refuse("ERIDIAN_HOME must be an absolute path")
    try:
        src_st = os.lstat(source)
    except FileNotFoundError:
        print(f"eridian: {source} does not exist; nothing to migrate")
        return 0
    except OSError as exc:
        return refuse(f"cannot inspect {source}: {exc}")
    try:
        for path in (source, target):
            link = _symlink_in(path)
            if link is not None:
                return refuse(f"{link} is a symlink")
    except OSError as exc:
        return refuse(f"cannot inspect {path}: {exc}")
    if not stat.S_ISDIR(src_st.st_mode):
        return refuse(f"{source} is not a directory")
    uid = os.getuid()
    if src_st.st_uid != uid:
        return refuse(f"{source} is not owned by the current user")
    try:
        parent_st = os.lstat(target.parent)
    except OSError as exc:
        return refuse(f"cannot inspect {target.parent}: {exc}")
    if parent_st.st_uid != uid:
        return refuse(f"{target.parent} is not owned by the current user")
    if parent_st.st_mode & 0o022:
        return refuse(f"{target.parent} is group- or world-writable")
    target_exists = True
    try:
        tgt_st = os.lstat(target)
    except FileNotFoundError:
        target_exists = False
        tgt_st = parent_st
    except OSError as exc:
        return refuse(f"cannot inspect {target}: {exc}")
    if target_exists:
        if not stat.S_ISDIR(tgt_st.st_mode):
            return refuse(f"{target} exists and is not a directory")
        if tgt_st.st_uid != uid:
            return refuse(f"{target} is not owned by the current user")
    for folder in (source, target) if target_exists else (source,):
        reason = _attach_live_in(folder)
        if reason is not None:
            return refuse(reason)
    try:
        if target_exists and any(target.iterdir()):
            return refuse(f"{target} is not empty")
    except OSError as exc:
        return refuse(f"cannot read {target}: {exc}")
    if src_st.st_dev != tgt_st.st_dev:
        return refuse(f"{source} and {target} are on different filesystems")
    if dry_run:
        print(f"eridian: would move {source} -> {target} (dry run; nothing changed)")
        return 0
    try:
        if target_exists:
            target.rmdir()
        os.rename(source, target)
    except OSError as exc:
        return refuse(f"could not move {source} -> {target}: {exc}")
    try:
        fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    except OSError as exc:
        print(
            f"eridian: migrated to {target} but could not set mode 0700: {exc}; "
            f"fix with chmod 700 {target}",
            file=sys.stderr,
        )
        return 1
    print(f"moved {source} -> {target}")
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    # The one place that names the pre-rename ~/.undertone folder.
    return _migrate(
        Path.home() / ".undertone", paths.home_path(), dry_run=args.dry_run
    )


def _cmd_attach(args: argparse.Namespace) -> int:
    from . import voice

    return voice.run_attach(
        no_beep=args.no_beep,
        follow_up_s=args.follow_up,
        no_aec=args.no_aec,
        tts=args.tts,
        announce=args.announce,
    )


def _cmd_models_fetch(args: argparse.Namespace) -> int:
    del args
    from . import kokoro, stt

    print(f"eridian: fetching {stt.MODEL_ID} (contacts the Hugging Face Hub)...")
    stt.fetch_model()
    print(
        "eridian: fetching onnx-community/Kokoro-82M-v1.0-ONNX (Kokoro voice, "
        "about 330 MB; contacts the Hugging Face Hub)..."
    )
    kokoro.fetch_model()
    print("eridian: models cached")
    if shutil.which("espeak-ng") is None:
        print("eridian: the Kokoro voice also needs espeak-ng: brew install espeak-ng")
    return 0


def _cmd_safe_phrase(args: argparse.Namespace) -> int:
    from . import voice_guard

    home = paths.home_dir()
    try:
        if args.status:
            if voice_guard.safe_phrase_configured(home):
                print("A safe phrase is set.")
            else:
                print(
                    "No safe phrase is set (voice confirms use a fresh two-word phrase)."
                )
            return 0
        if args.clear:
            if voice_guard.clear_safe_phrase(home):
                print(
                    "Safe phrase removed; voice confirms use a fresh two-word phrase "
                    "again. Restart attach and session."
                )
            else:
                print("No safe phrase was set.")
            return 0
        if not sys.stdin.isatty():
            # getpass falls back to an echoed prompt without a TTY, which would print the phrase.
            print(
                "eridian safe-phrase needs an interactive terminal so your phrase stays hidden.",
                file=sys.stderr,
            )
            return 2
        try:
            first = voice_guard.validate_safe_phrase(
                getpass.getpass("Safe phrase (1-3 words; hidden): ")
            )
            again = getpass.getpass("Say it again (hidden): ")
        except (EOFError, KeyboardInterrupt):
            return 1
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        try:
            matches = voice_guard.validate_safe_phrase(again) == first
        except ValueError:
            matches = False
        if not matches:
            print("The two entries didn't match; nothing changed.", file=sys.stderr)
            return 1
        voice_guard.set_safe_phrase(home, first)
    except voice_guard.GuardError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        "Safe phrase set. Restart `eridian attach` and `eridian session` to use it. "
        "Voice confirms are now: the wake word, then your safe phrase.\n"
        "Keep it private: anyone who hears it can use it to confirm."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eridian")
    sub = parser.add_subparsers(dest="command", required=True)

    listen_parser = sub.add_parser(
        "listen", help="mic -> wake word -> VAD -> STT; prints one JSON event per line"
    )
    listen_parser.add_argument(
        "--no-beep", action="store_true", help="skip the audible wake-word beep"
    )
    listen_parser.set_defaults(func=_cmd_listen)

    chat_parser = sub.add_parser(
        "chat", help="hands-free conversation with Claude: wake word -> reply spoken aloud"
    )
    chat_parser.add_argument(
        "--no-beep", action="store_true", help="skip the audible wake/follow-up cues"
    )
    chat_parser.add_argument(
        "--resume", action="store_true", help="resume the last chat session"
    )
    chat_parser.add_argument(
        "--follow-up",
        type=float,
        default=6.0,
        metavar="SECONDS",
        help="seconds to keep listening after a reply without the wake word "
        "(default 6; 0 disables)",
    )
    chat_parser.add_argument(
        "--no-aec",
        action="store_true",
        help="play speech with `say` directly; no echo cancellation, so only the "
        "wake word interrupts a reply (use if AEC misbehaves)",
    )
    chat_parser.add_argument(
        "--tts",
        choices=["kokoro", "say"],
        default="kokoro",
        help="voice engine: kokoro (default; needs `eridian models fetch` and "
        "espeak-ng) or the macOS say voice",
    )
    chat_parser.set_defaults(func=_cmd_chat)

    sub.add_parser(
        "channel", help="internal: MCP channel server spawned by Claude Code"
    ).set_defaults(func=_cmd_channel)

    session_parser = sub.add_parser(
        "session", help="start an interactive claude session that accepts voice turns"
    )
    session_parser.add_argument("claude_args", nargs=argparse.REMAINDER)
    session_parser.set_defaults(func=_cmd_session)

    launch_parser = sub.add_parser(
        "launch",
        help="run claude with voice when interactive (starts attach if needed), "
        "else plain claude",
    )
    launch_parser.add_argument("claude_args", nargs=argparse.REMAINDER)
    launch_parser.set_defaults(func=_cmd_launch)

    sub.add_parser("stop", help="stop the background `eridian attach`").set_defaults(
        func=_cmd_stop
    )

    voice_parser = sub.add_parser(
        "voice", help="show or change per-session voice (talks to a running `eridian attach`)"
    )
    voice_sub = voice_parser.add_subparsers(dest="voice_command", required=True)
    voice_sub.add_parser(
        "status", help="show whether the mic is muted and which sessions have voice on"
    ).set_defaults(func=_cmd_voice_status)
    for name, func, what in (("on", _cmd_voice_on, "on"), ("off", _cmd_voice_off, "off")):
        p = voice_sub.add_parser(name, help=f"turn voice {what} for a session")
        p.add_argument(
            "--session",
            metavar="ID",
            help="session id or pid from `eridian voice status` (default: the session "
            "this command is run from)",
        )
        p.set_defaults(func=func)
    sub.add_parser("mute", help="mute the mic (replies still play)").set_defaults(
        func=_cmd_mute
    )
    sub.add_parser("unmute", help="reopen the mic").set_defaults(func=_cmd_unmute)

    migrate_parser = sub.add_parser(
        "migrate",
        help="move the pre-rename ~/.undertone state folder to the eridian home",
    )
    migrate_parser.add_argument(
        "--dry-run", action="store_true", help="run every check, change nothing"
    )
    migrate_parser.set_defaults(func=_cmd_migrate)

    shell_parser = sub.add_parser(
        "shell-init", help="print shell code that makes `claude` autolaunch voice"
    )
    shell_parser.add_argument("shell", choices=["zsh"])
    shell_parser.set_defaults(func=_cmd_shell_init)

    attach_parser = sub.add_parser(
        "attach",
        help="listen for the wake word ('hey jarvis' by default) and send speech to the latest `eridian session`",
    )
    attach_parser.add_argument(
        "--no-beep", action="store_true", help="skip the audible wake/follow-up cues"
    )
    attach_parser.add_argument(
        "--follow-up",
        type=float,
        default=6.0,
        metavar="SECONDS",
        help="seconds to keep listening after a reply without the wake word "
        "(default 6; 0 disables)",
    )
    attach_parser.add_argument(
        "--no-aec",
        action="store_true",
        help="play speech with `say` directly; no echo cancellation, so only the "
        "wake word interrupts a reply (use if AEC misbehaves)",
    )
    attach_parser.add_argument(
        "--tts",
        choices=["kokoro", "say"],
        default="kokoro",
        help="voice engine: kokoro (default; needs `eridian models fetch` and "
        "espeak-ng) or the macOS say voice",
    )
    attach_parser.add_argument(
        "--announce",
        choices=["all", "permission", "off"],
        default="all",
        help="speak when Claude finishes or needs you (default all; "
        "'permission' skips 'finished')",
    )
    attach_parser.set_defaults(func=_cmd_attach)

    safe_parser = sub.add_parser(
        "safe-phrase", help="set, clear or check the safe phrase that confirms voice approvals"
    )
    safe_group = safe_parser.add_mutually_exclusive_group()
    safe_group.add_argument(
        "--clear", action="store_true", help="remove the safe phrase"
    )
    safe_group.add_argument(
        "--status", action="store_true", help="say whether a safe phrase is set"
    )
    safe_parser.set_defaults(func=_cmd_safe_phrase)

    models_parser = sub.add_parser("models", help="manage local speech models")
    models_sub = models_parser.add_subparsers(dest="models_command", required=True)
    models_sub.add_parser(
        "fetch", help="download the speech models once (needs network)"
    ).set_defaults(func=_cmd_models_fetch)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] in (["session"], ["launch"]):
        # argparse's REMAINDER drops a leading option (`session --model x`), so
        # hand everything after the subcommand to claude untouched.
        args = parser.parse_args(raw[:1])
        args.claude_args = raw[1:]
    else:
        args = parser.parse_args(raw)
    if getattr(args, "command", None) in _WAKE_COMMANDS:
        try:
            wake.resolve()
        except wake.WakeWordError as exc:
            print(f"eridian: {exc}", file=sys.stderr)
            return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

"""Command-line entry point: `undertone <command>`."""

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
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import paths, wake


_WAKE_CUE = "/System/Library/Sounds/Tink.aiff"
_FOLLOW_UP_CUE = "/System/Library/Sounds/Pop.aiff"

# Commands that use the wake word; a bad UNDERTONE_WAKE_MODEL stops them at startup.
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


def chat_workdir(cwd: Path, home: Path, undertone_home: Path) -> tuple[Path, bool]:
    """Pick the (resolved) directory Claude's file tools are confined to.

    Home, its ancestors, `/`, dot-directories and Library under home, and the
    top-level Documents/Desktop/Downloads are too broad or sensitive to expose
    to voice-driven file reads, so those fall back to a private workspace under
    ~/.undertone. Returns (workdir, substituted); raises WorkdirError if the
    workspace path is a symlink.
    """
    resolved = cwd.resolve()
    if not _too_broad_or_sensitive(resolved, home.resolve()):
        return resolved, False
    workspace = undertone_home / "workspace"
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

    def frames(self):
        if self.engine is not None:
            return self.engine.frames()
        from . import audio

        return audio.frame_source()

    def start(self) -> None:
        if self.engine is not None:
            self.engine.start()

    def close(self) -> None:
        if self.engine is not None:
            self.engine.close()


def _say_voices() -> tuple[str, str]:
    """(reply voice, undertone's own voice) for `say`, with the same defaults build_say_argv uses."""
    return (
        os.environ.get("UNDERTONE_VOICE") or "Evan (Enhanced)",
        os.environ.get("UNDERTONE_OWN_VOICE") or "Samantha",
    )


def _say_voice_label() -> str:
    reply, own = _say_voices()
    return f"say ({reply}; undertone's own voice: {own})"


class OwnVoiceError(Exception):
    """undertone's own voice is part of Claude's voice: read-backs would be indistinguishable."""


def _same_say_voice_message(name: str) -> str:
    return (
        f"undertone's own voice is the same as the reply voice ({name}); "
        "set UNDERTONE_VOICE or UNDERTONE_OWN_VOICE so they differ"
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
            espeak = os.environ.get("UNDERTONE_ESPEAK_CMD", "espeak-ng")
            if shutil.which(espeak) is None:
                raise KokoroUnavailable("espeak-ng not found; install it with `brew install espeak-ng`")
            voice = (kokoro_factory or _load_kokoro_voice)()
        except KokoroUnavailable as exc:
            print(f"undertone: Kokoro voice unavailable ({exc}); using say", file=err, flush=True)

    if voice is not None:
        if voice.own_voice in voice.voice_names:
            # Fail closed: the confirm read-back is only trustworthy if it sounds unlike Claude.
            raise OwnVoiceError(
                f"undertone's own voice ({voice.own_voice}) is part of the reply voice "
                f"({voice.voice}); set UNDERTONE_KOKORO_OWN_VOICE or UNDERTONE_KOKORO_VOICE "
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
            f"undertone: the wake-word screen failed ({exc}); speech stays muted until restart",
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
            f"Kokoro ({voice.voice}; undertone's own voice: {voice.own_voice})"
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
    "undertone: cannot open the audio devices for echo cancellation: {exc}. Try --no-aec."
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
        print(f"undertone: {exc}", file=sys.stderr)
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
        print(f"undertone: {exc}", file=sys.stderr)
        return 1

    from pysilero_vad import SileroVoiceActivityDetector

    vad = SileroVoiceActivityDetector()

    try:
        workdir, substituted = chat_workdir(Path.cwd(), Path.home(), paths.home_dir())
    except WorkdirError as exc:
        print(f"undertone: {exc}", file=sys.stderr)
        return 1

    def brain_factory(resume: str | None) -> brain.ClaudeBrain:
        b = brain.ClaudeBrain(workdir, resume_session_id=resume)
        b.start()
        return b

    cues = {"wake": _WAKE_CUE, "follow_up": _FOLLOW_UP_CUE}

    try:
        io = make_audio_io(args.no_aec, tts=args.tts)
    except OwnVoiceError as exc:
        print(f"undertone: {exc}", file=sys.stderr)
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

    print(f"undertone chat: working in {workdir}")
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
    """MCP config that launches the channel server with the same UNDERTONE_HOME.

    The channel gets the resolved wake PHRASE (never the model path): it only needs
    the words for its instructions, and this keeps it from loading the manifest.
    """
    env = {"UNDERTONE_HOME": str(home), "UNDERTONE_SESSION": token}
    phrase = wake.current()
    if not phrase.is_default:
        env[wake.PHRASE_ENV] = phrase.phrase
    return {
        "mcpServers": {
            "undertone": {
                "command": executable,
                # -P: keep the session cwd off sys.path so a hostile checkout can't shadow us.
                "args": ["-P", "-m", "undertone.cli", "channel"],
                "env": env,
            }
        }
    }


def voice_guard_settings(executable: str, home: Path, token: str) -> dict:
    """Inline `--settings` JSON that installs the voice guard's hooks and the announce hooks for one session."""
    from . import voice_guard

    def _hook_cmd(event: str) -> str:
        parts = [
            executable, "-P", "-m", "undertone.voice_guard", event,
            "--home", str(home), "--session", token,
        ]
        return " ".join(shlex.quote(x) for x in parts)

    def _announce(event: str) -> dict:
        parts = [
            executable, "-P", "-m", "undertone.announce", event,
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
    """argv for an interactive claude session with the undertone channel loaded.

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
        "server:undertone",
        "--allowedTools",
        "mcp__undertone__reply",
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


def _exec_session(rest: list[str], *, warn_no_voice: bool) -> int:
    """Exec claude wired to the voice channel; returns only on a refused flag."""
    for arg in rest:
        if arg in _REFUSED_SESSION_FLAGS or arg.startswith("--settings="):
            flag = arg.split("=", 1)[0]
            print(
                f"undertone: remove {flag}: undertone session sets the voice guard's "
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
            "undertone: no voice process is listening -- start `undertone attach` "
            "in another terminal to talk",
            file=sys.stderr,
        )
    claude_cmd = os.environ.get("UNDERTONE_CLAUDE_CMD") or "claude"
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
    if env.get("UNDERTONE_AUTOLAUNCH") == "0" or not (stdin_tty and stdout_tty):
        return "passthrough"
    for arg in argv:
        if arg in _PASSTHROUGH_FLAGS or arg.startswith("--settings="):
            return "passthrough"
        if not arg.startswith("-") and arg in _CLAUDE_SUBCOMMANDS:
            return "passthrough"
    return "voice"


def _pid_is_attach(pid: int) -> bool:
    """True if pid is alive and its command line is an undertone attach."""
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    # Match tokens, not substrings: `tail -f ~/.undertone/attach.log` must not pass.
    return bool(
        re.search(r"(?:^|/)undertone attach(?:\s|$)|-m undertone attach(?:\s|$)", out)
    )


def _ensure_attach() -> None:
    """Spawn a background `undertone attach` unless one answers or is still starting.

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
        print(f"undertone: could not start voice in the background: {exc}", file=sys.stderr)


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
        log_fd = os.open(
            log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
        try:
            os.fchmod(log_fd, 0o600)  # tighten a pre-existing log
            child = subprocess.Popen(
                [sys.executable, "-m", "undertone", "attach"],
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
            f"undertone: starting voice in the background (log: {log_path})",
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
        _ensure_attach()
        return _exec_session(rest, warn_no_voice=False)
    claude_cmd = os.environ.get("UNDERTONE_CLAUDE_CMD") or "claude"
    os.execvpe(claude_cmd, [claude_cmd, *rest], os.environ)
    return 1  # unreachable: execvpe replaces the process


def _cmd_stop(args: argparse.Namespace) -> int:
    del args
    pid = paths.read_attach_pid()
    if pid is None:
        print("undertone: no attach pidfile; nothing to stop")
        return 0
    if not _pid_is_attach(pid):
        print(
            f"undertone: pid {pid} is not an undertone attach (stale pidfile?); "
            "not signalling it",
            file=sys.stderr,
        )
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"undertone: could not stop attach (pid {pid}): {exc}", file=sys.stderr)
        return 1
    print(f"undertone: sent SIGTERM to attach (pid {pid})")
    return 0


def _cmd_shell_init(args: argparse.Namespace) -> int:
    del args
    exe = Path(sys.argv[0]).absolute()
    if exe.name != "undertone":  # e.g. `python -m undertone`: use the venv script
        exe = Path(sys.executable).absolute().parent / "undertone"
    fn = f"claude() {{ {shlex.quote(str(exe))} launch -- \"$@\"; }}"
    print(
        "# `command claude` bypasses this function and runs plain claude.\n"
        f"{fn}\n"
        "# cmux redefines claude() at the first prompt (its _cmux_fix_path precmd hook); "
        "re-assert ours once, after it.\n"
        "_undertone_claude_precmd() {\n"
        f"  {fn}\n"
        "  add-zsh-hook -d precmd _undertone_claude_precmd\n"
        "}\n"
        "autoload -Uz add-zsh-hook\n"
        "add-zsh-hook precmd _undertone_claude_precmd"
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

    print(f"undertone: fetching {stt.MODEL_ID} (contacts the Hugging Face Hub)...")
    stt.fetch_model()
    print(
        "undertone: fetching onnx-community/Kokoro-82M-v1.0-ONNX (Kokoro voice, "
        "about 330 MB; contacts the Hugging Face Hub)..."
    )
    kokoro.fetch_model()
    print("undertone: models cached")
    if shutil.which("espeak-ng") is None:
        print("undertone: the Kokoro voice also needs espeak-ng: brew install espeak-ng")
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
                "undertone safe-phrase needs an interactive terminal so your phrase stays hidden.",
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
        "Safe phrase set. Restart `undertone attach` and `undertone session` to use it. "
        "Voice confirms are now: the wake word, then your safe phrase.\n"
        "Keep it private: anyone who hears it can use it to confirm."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="undertone")
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
        help="voice engine: kokoro (default; needs `undertone models fetch` and "
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

    sub.add_parser("stop", help="stop the background `undertone attach`").set_defaults(
        func=_cmd_stop
    )

    shell_parser = sub.add_parser(
        "shell-init", help="print shell code that makes `claude` autolaunch voice"
    )
    shell_parser.add_argument("shell", choices=["zsh"])
    shell_parser.set_defaults(func=_cmd_shell_init)

    attach_parser = sub.add_parser(
        "attach",
        help="listen for the wake word ('hey jarvis' by default) and send speech to the latest `undertone session`",
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
        help="voice engine: kokoro (default; needs `undertone models fetch` and "
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
            print(f"undertone: {exc}", file=sys.stderr)
            return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

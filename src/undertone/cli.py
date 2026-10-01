"""Command-line entry point: `undertone <command>`."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import paths


_WAKE_CUE = "/System/Library/Sounds/Tink.aiff"
_FOLLOW_UP_CUE = "/System/Library/Sounds/Pop.aiff"


def _make_wake_factory() -> Callable[[], Callable[[bytes], bool]]:
    # Lazy import: pymicro-wakeword is heavy and only the voice commands need it.
    from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures, Model

    def wake_factory():
        mww = MicroWakeWord.from_builtin(Model.HEY_JARVIS)
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

    from . import audio, brain
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
    print("Say 'hey jarvis'. Ctrl-C to quit.", flush=True)
    try:
        session.run(audio.frame_source())
    except KeyboardInterrupt:
        pass
    finally:
        session.close()
    return 0


def channel_mcp_config(executable: str, home: Path) -> dict:
    """MCP config that launches the channel server with the same UNDERTONE_HOME."""
    return {
        "mcpServers": {
            "undertone": {
                "command": executable,
                # -P: keep the session cwd off sys.path so a hostile checkout can't shadow us.
                "args": ["-P", "-m", "undertone.cli", "channel"],
                "env": {"UNDERTONE_HOME": str(home)},
            }
        }
    }


def build_session_argv(
    cfg_path: Path, rest: Sequence[str], claude_cmd: str = "claude"
) -> list[str]:
    """argv for an interactive claude session with the undertone channel loaded.

    Only the reply tool is pre-approved, so a voice-driven turn can speak
    without a permission prompt but nothing else is auto-allowed.
    """
    return [
        claude_cmd,
        "--mcp-config",
        str(cfg_path),
        "--dangerously-load-development-channels",
        "server:undertone",
        "--allowedTools",
        "mcp__undertone__reply",
        *rest,
    ]


def _write_private_json(path: Path, payload: Mapping) -> None:
    """Atomically write JSON at 0600: exclusive mkstemp in the same dir, then os.replace."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _cmd_channel(args: argparse.Namespace) -> int:
    del args
    from . import mcp_channel

    return mcp_channel.main()


def _cmd_session(args: argparse.Namespace) -> int:
    cfg = paths.channel_config_path()
    _write_private_json(cfg, channel_mcp_config(sys.executable, paths.home_dir()))
    if not _voice_socket_present(paths.voice_socket_path()):
        print(
            "undertone: no voice process is listening -- start `undertone attach` "
            "in another terminal to talk",
            file=sys.stderr,
        )
    rest = list(args.claude_args)
    if rest[:1] == ["--"]:
        rest = rest[1:]
    claude_cmd = os.environ.get("UNDERTONE_CLAUDE_CMD") or "claude"
    argv = build_session_argv(cfg, rest, claude_cmd)
    # Replies reach the user through the channel's `reply` tool; claude inherits
    # our environment unchanged.
    os.execvpe(claude_cmd, argv, os.environ)
    return 1  # unreachable: execvpe replaces the process


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

    return voice.run_attach(no_beep=args.no_beep, follow_up_s=args.follow_up)


def _cmd_models_fetch(args: argparse.Namespace) -> int:
    del args
    from .stt import MODEL_ID, fetch_model

    print(f"undertone: fetching {MODEL_ID} (contacts the Hugging Face Hub)...")
    fetch_model()
    print("undertone: model cached")
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
    chat_parser.set_defaults(func=_cmd_chat)

    sub.add_parser(
        "channel", help="internal: MCP channel server spawned by Claude Code"
    ).set_defaults(func=_cmd_channel)

    session_parser = sub.add_parser(
        "session", help="start an interactive claude session that accepts voice turns"
    )
    session_parser.add_argument("claude_args", nargs=argparse.REMAINDER)
    session_parser.set_defaults(func=_cmd_session)

    attach_parser = sub.add_parser(
        "attach", help="listen for 'hey jarvis' and send speech to the latest `undertone session`"
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
    attach_parser.set_defaults(func=_cmd_attach)

    models_parser = sub.add_parser("models", help="manage local speech models")
    models_sub = models_parser.add_subparsers(dest="models_command", required=True)
    models_sub.add_parser(
        "fetch", help="download the Parakeet STT model once (needs network)"
    ).set_defaults(func=_cmd_models_fetch)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["session"]:
        # argparse's REMAINDER drops a leading option (`session --model x`), so
        # hand everything after `session` to claude untouched.
        args = parser.parse_args(["session"])
        args.claude_args = raw[1:]
    else:
        args = parser.parse_args(raw)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

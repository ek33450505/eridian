"""Command-line entry point: `undertone <command>`."""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

from . import hooks, paths, speaker


def _cmd_serve(args: argparse.Namespace) -> int:
    del args
    return speaker.serve()


def _cmd_say(args: argparse.Namespace) -> int:
    text = " ".join(args.text)
    if not speaker.send({"op": "say", "text": text}):
        print("undertone: no speaker daemon running (try `undertone serve`)", file=sys.stderr)
        return 1
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    del args
    speaker.send({"op": "stop"})
    return 0


def _cmd_on(args: argparse.Namespace) -> int:
    del args
    paths.enabled_flag_path().touch()
    print("undertone: speaking replies is ON")
    return 0


def _cmd_off(args: argparse.Namespace) -> int:
    del args
    flag = paths.enabled_flag_path()
    if flag.exists():
        flag.unlink()
    print("undertone: speaking replies is OFF")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    del args
    daemon_up = speaker.send({"op": "ping"}, timeout=0.3)
    enabled = paths.enabled_flag_path().exists()
    print(f"daemon: {'up' if daemon_up else 'down'}")
    print(f"enabled: {'yes' if enabled else 'no'}")
    return 0


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


def _cmd_models_fetch(args: argparse.Namespace) -> int:
    del args
    from .stt import MODEL_ID, fetch_model

    print(f"undertone: fetching {MODEL_ID} (contacts the Hugging Face Hub)...")
    fetch_model()
    print("undertone: model cached")
    return 0


def _cmd_hook(args: argparse.Namespace) -> int:
    stdin_text = sys.stdin.read()
    if args.hook_name == "stop":
        return hooks.stop_hook(stdin_text)
    return hooks.prompt_hook(stdin_text)


def _cmd_hooks_snippet(args: argparse.Namespace) -> int:
    del args
    snippet = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "undertone hook stop",
                            "async": True,
                            "timeout": 5,
                        }
                    ]
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "undertone hook prompt",
                        }
                    ]
                }
            ],
        }
    }
    print(json.dumps(snippet, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="undertone")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the speaker daemon in the foreground").set_defaults(
        func=_cmd_serve
    )

    say_parser = sub.add_parser("say", help="speak text through the daemon")
    say_parser.add_argument("text", nargs="+")
    say_parser.set_defaults(func=_cmd_say)

    sub.add_parser("stop", help="stop current speech").set_defaults(func=_cmd_stop)
    sub.add_parser("on", help="opt this machine in to speaking replies").set_defaults(
        func=_cmd_on
    )
    sub.add_parser("off", help="opt out of speaking replies").set_defaults(func=_cmd_off)
    sub.add_parser("status", help="show daemon and opt-in status").set_defaults(
        func=_cmd_status
    )

    hook_parser = sub.add_parser("hook", help="internal: invoked by Claude Code hooks")
    hook_parser.add_argument("hook_name", choices=["stop", "prompt"])
    hook_parser.set_defaults(func=_cmd_hook)

    sub.add_parser(
        "hooks-snippet", help="print the settings.json hooks block to register undertone"
    ).set_defaults(func=_cmd_hooks_snippet)

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

    models_parser = sub.add_parser("models", help="manage local speech models")
    models_sub = models_parser.add_subparsers(dest="models_command", required=True)
    models_sub.add_parser(
        "fetch", help="download the Parakeet STT model once (needs network)"
    ).set_defaults(func=_cmd_models_fetch)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

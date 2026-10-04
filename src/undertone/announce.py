"""Claude Code hook command that tells `undertone attach` to speak a short announcement.

Usage: python -P -m undertone.announce {stop,stopfailure,permissionrequest} --home HOME --session TOKEN
(stdin = the hook payload JSON).

It sends one line to `<HOME>/voice.sock`:
    {"type": "announce", "session": "<token>", "event": "<event>"}\n
Nothing from the payload is ever forwarded; only the fixed event key is sent.

Mapping: stop -> finished (nothing while a background subagent still runs),
stopfailure -> error, permissionrequest -> question (AskUserQuestion) /
plan (ExitPlanMode) / permission (anything else).

Always exits 0 and never writes to stdout or stderr: PermissionRequest stdout is
a decision channel, and stray output could allow or deny a tool.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import sys
from pathlib import Path

_HOOKS = ("stop", "stopfailure", "permissionrequest")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def event_for(hook: str, payload: object) -> str | None:
    """Map a hook name and its payload to an announce event key, or None to send nothing."""
    if hook == "stop":
        tasks = payload.get("background_tasks") if isinstance(payload, dict) else None
        if isinstance(tasks, list) and any(
            isinstance(t, dict) and t.get("type") == "subagent" and t.get("status") == "running"
            for t in tasks
        ):
            return None
        return "finished"
    if hook == "stopfailure":
        return "error"
    if hook == "permissionrequest":
        tool = payload.get("tool_name") if isinstance(payload, dict) else None
        if tool == "AskUserQuestion":
            return "question"
        if tool == "ExitPlanMode":
            return "plan"
        return "permission"
    return None


def _socket_ok(path: Path) -> bool:
    """True only if `path` is a socket owned by us."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid()


def _send(path: Path, line: bytes) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(0.5)
        s.connect(str(path))
        s.sendall(line)
        return True
    except OSError:
        return False
    finally:
        s.close()


def _parse_payload(raw: str) -> object:
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def _run(argv, raw: str) -> None:
    # Imported here so a broken install still exits 0 silently (inside main's try).
    from .voice_guard import TOKEN_RE, _log

    parser = _Parser(add_help=False)
    parser.add_argument("hook", choices=_HOOKS)
    parser.add_argument("--home", required=True)
    parser.add_argument("--session", required=True)
    args = parser.parse_args(argv)

    home = Path(args.home)
    if not home.is_absolute():
        return  # a relative --home once leaked logs into the repo
    hook = args.hook
    if not TOKEN_RE.fullmatch(args.session):
        _log(home, "announce", "skipped", hook, "bad-token")
        return
    event = event_for(hook, _parse_payload(raw))
    if event is None:
        _log(home, "announce", "skipped", hook, "bg-agents")
        return
    sock = home / "voice.sock"
    if not _socket_ok(sock):
        _log(home, "announce", "skipped", hook, "no-socket")
        return
    line = (json.dumps({"type": "announce", "session": args.session, "event": event}) + "\n").encode("utf-8")
    if _send(sock, line):
        _log(home, "announce", "sent", hook, event)
    else:
        _log(home, "announce", "error", hook, "send-failed")


def main(argv=None, stdin=sys.stdin) -> int:
    try:
        # Drain the payload first so Claude Code never hits EPIPE on an early return.
        try:
            raw = stdin.read(1_000_000)
        except BaseException:
            raw = ""
        _run(sys.argv[1:] if argv is None else argv, raw)
    except BaseException:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Claude Code status line segment: a short Rocky mood derived from git state.

`python -P -m eridian.statusline` reads the status line JSON from stdin (at most 64 KiB), runs two
bounded git commands (a `git config` filter pre-check, then `git status`) in the session's
directory and prints a single `Rocky: ...` line. It always exits 0 and prints nothing on any
problem (bad input, no repo, no git, timeout).

Guarded: fsmonitor is disabled, and if any `filter.*.clean` / `filter.*.process` driver is
configured (any scope, includes too) no segment is shown, since `git status` would run it.
A timed-out git is killed with its whole process group. Remaining: it reads repo config like
any git command does.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from typing import TextIO

MAX_BYTES = 64 * 1024
GIT_TIMEOUT = 0.25

WORRY = "Rocky: worry worry worry"
WORK = "Rocky: work work"
PUSH = "Rocky: push question"
PULL = "Rocky: pull question"
GOOD = "Rocky: good good good"


def mood_from_porcelain(text: str) -> str | None:
    """Mood line for `git status --porcelain=v2 --branch` output.

    Returns None when the text has no `# branch.head` header (not git status output); a header
    with no entries and no `branch.ab` line is clean (GOOD).
    """
    head = None
    ahead = behind = 0
    dirty = conflict = False
    for line in text.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):].strip()
        elif line.startswith("# branch.ab "):
            try:
                a, b = line.split()[2:4]
                ahead, behind = int(a.lstrip("+")), int(b.lstrip("-"))
            except ValueError:
                pass
        elif line.startswith("u "):
            conflict = True
        elif line.startswith(("1 ", "2 ")):
            dirty = True
    if head is None:
        return None
    if conflict or head == "(detached)" or (ahead > 0 and behind > 0):
        return WORRY
    if dirty:
        return WORK
    if ahead > 0:
        return PUSH
    if behind > 0:
        return PULL
    return GOOD


def reap(proc: subprocess.Popen) -> None:
    """Bounded cleanup after a kill: a setsid grandchild may still hold the stdout pipe."""
    try:
        proc.stdout.close()
    except (OSError, AttributeError):
        pass
    try:
        proc.wait(timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_git(git: str, args: list[str], env: dict[str, str]) -> tuple[int, bytes] | None:
    """Run git in its own process group; on timeout kill the whole group (filter children)."""
    try:
        proc = subprocess.Popen([git, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        reap(proc)
        return None
    return proc.returncode, out


def git_status(cwd: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    # `git status` runs filter drivers on stat-dirty files: refuse if any is configured
    # (exit 1 = no match; a match, an error or a timeout all mean no segment).
    found = run_git(git, ["-C", cwd, "config", "--get-regexp", r"^filter\..*\.(clean|process)$"],
                    env)
    if found is None or found[0] != 1:
        return None
    ran = run_git(git, ["-C", cwd, "-c", "core.fsmonitor=false", "--no-optional-locks", "status",
                        "--porcelain=v2", "--branch", "-uno", "--ignore-submodules=all"], env)
    if ran is None or ran[0] != 0:
        return None
    # Output is buffered whole (bounded by the timeout) before this slice; a truncated status
    # could drop a `u ` line (accepted).
    return ran[1][:MAX_BYTES].decode("utf-8", "replace")


def session_cwd(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    ws = payload.get("workspace")
    cwd = ws.get("current_dir") if isinstance(ws, dict) else None
    if not isinstance(cwd, str):
        cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        return None
    return cwd


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    out = stdout if stdout is not None else sys.stdout
    try:
        if stdin is None:
            raw = sys.stdin.buffer.read(MAX_BYTES).decode("utf-8", "replace")
        else:
            raw = stdin.read(MAX_BYTES)
        cwd = session_cwd(json.loads(raw))
        text = git_status(cwd) if cwd else None
        mood = mood_from_porcelain(text) if text else None
        if mood:
            out.write(mood + "\n")
            out.flush()
    except Exception:  # never error the status line
        if out is sys.stdout:
            # Python docs recipe: keep the exit-time flush from failing on a dead pipe.
            try:
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            except (OSError, ValueError, AttributeError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

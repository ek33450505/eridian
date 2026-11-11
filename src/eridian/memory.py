"""Rocky's cross-session memory: a small, personal-only store of short lines.

Run as ``python -P -m eridian.memory <command>`` with ``add``, ``list`` or ``forget``.
Entries live one JSON object per line in ``<dir>/memory.jsonl`` where ``<dir>`` is
``$ERIDIAN_HOME`` (else ``~/.eridian``). Nothing here reads stdin, and every command
refuses to run when the working directory is inside the work (deny) directory.
"""

from __future__ import annotations

import argparse
import datetime
import errno
import fcntl
import json
import math
import os
import re
import stat
import sys
import time
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from eridian.persona import _cwd_under

KINDS = ("win", "fail", "joke", "fact", "mood")
MAX_TEXT = 200
MAX_ENTRIES = 60
READ_CAP = 256 * 1024
DEFAULT_DENY = "~/Projects/work"
STORE_NAME = "memory.jsonl"
LOCK_NAME = "memory.lock"
LOCK_TIMEOUT = 5.0
DROPPED = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


class StoreError(Exception):
    """The store failed a safety check; the command exits 1."""


def store_dir() -> Path:
    home = os.environ.get("ERIDIAN_HOME")
    return Path(home) if home else Path.home() / ".eridian"


def normalise(words: Sequence[str]) -> str:
    """Whitespace to single spaces; control/format/surrogate/private/unassigned dropped."""
    spaced = "".join(" " if c.isspace() else c for c in " ".join(words))
    kept = "".join(c for c in spaced if unicodedata.category(c) not in DROPPED)
    return " ".join(kept.split())


@contextmanager
def _open_dir(home: Path, create: bool) -> Iterator[int | None]:
    """Yield an fd for the store dir (None if absent and not creating); all file
    operations go through it so the path cannot be swapped underneath us."""
    if create:
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        dfd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        yield None
        return
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise StoreError(f"refusing symlinked or non-directory memory dir: {home}") from exc
        raise
    try:
        st = os.fstat(dfd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise StoreError(f"memory dir is not a directory owned by you: {home}")
        if create:
            os.fchmod(dfd, 0o700)
        yield dfd
    finally:
        os.close(dfd)


def _open_file(dfd: int, name: str, flags: int) -> int:
    """Open a regular, single-link, self-owned file relative to dfd, never following links."""
    try:
        for attempt in range(5):
            try:
                fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=dfd)
                break
            except FileNotFoundError:
                # Observed on macOS (about half of racing opens in a probe), not a documented
                # guarantee: ENOENT from racing O_CREAT opens via dir_fd; retry.
                if not flags & os.O_CREAT or attempt == 4:
                    raise
                time.sleep(0.01)
    except OSError as exc:
        # ELOOP on Linux; EMLINK is the BSD/macOS errno for O_NOFOLLOW on a symlink.
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise StoreError(f"refusing symlinked file: {name}") from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
            raise StoreError(f"refusing unsafe file (not a plain private file): {name}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _lock_timeout() -> float:
    try:
        value = float(os.environ.get("ERIDIAN_TEST_LOCK_TIMEOUT", LOCK_TIMEOUT))
    except ValueError:
        return LOCK_TIMEOUT
    return value if math.isfinite(value) and value >= 0 else LOCK_TIMEOUT


@contextmanager
def _locked(dfd: int) -> Iterator[None]:
    fd = _open_file(dfd, LOCK_NAME, os.O_RDWR | os.O_CREAT)
    try:
        deadline = time.monotonic() + _lock_timeout()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StoreError("memory store is busy") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # closing releases the flock


@contextmanager
def _transaction(home: Path) -> Iterator[int]:
    with _open_dir(home, create=True) as dfd:
        if dfd is None:
            raise StoreError("memory dir vanished while opening it")
        with _locked(dfd):
            yield dfd


def _valid(obj: object) -> bool:
    if not isinstance(obj, dict) or set(obj) != {"ts", "kind", "text"}:
        return False
    ts, kind, text = obj["ts"], obj["kind"], obj["text"]
    if not isinstance(ts, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ts):
        return False
    try:
        datetime.date.fromisoformat(ts)
    except ValueError:
        return False
    return (
        isinstance(kind, str)
        and kind in KINDS
        and isinstance(text, str)
        and 0 < len(text) <= MAX_TEXT
        and normalise([text]) == text
    )


def _read_entries(dfd: int | None) -> list[dict]:
    if dfd is None:
        return []
    try:
        fd = _open_file(dfd, STORE_NAME, os.O_RDONLY)
    except FileNotFoundError:
        return []
    with os.fdopen(fd, "rb") as fh:
        if os.fstat(fh.fileno()).st_size > READ_CAP:
            raise StoreError(f"memory store is larger than {READ_CAP} bytes; not touching it")
        raw = fh.read(READ_CAP)
    entries: list[dict] = []
    for line in raw.split(b"\n"):
        try:
            obj = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError):  # includes UnicodeDecodeError
            continue
        if _valid(obj):
            entries.append(obj)
    return entries[-MAX_ENTRIES:]


def load_entries(home: Path | None = None) -> list[dict]:
    """Newest valid entries (at most MAX_ENTRIES), oldest first.

    A missing dir or file gives []; invalid lines are skipped. Raises StoreError or
    OSError when the store is unsafe or unreadable, so callers should catch them.
    """
    with _open_dir(store_dir() if home is None else home, create=False) as dfd:
        return _read_entries(dfd)


def _write_entries(dfd: int, entries: list[dict]) -> None:
    tmp = f".memory.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    fd = _open_file(dfd, tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, STORE_NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
        os.fsync(dfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eridian.memory")
    parser.add_argument(
        "--deny-under",
        default=DEFAULT_DENY,
        help="refuse every command when the cwd is inside this dir (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="save a short memory line")
    add.add_argument("--kind", required=True, choices=KINDS)
    add.add_argument("text", nargs="+", metavar="TEXT")
    sub.add_parser("list", help="show saved memories")
    forget = sub.add_parser("forget", help="remove one memory by number, or all")
    group = forget.add_mutually_exclusive_group(required=True)
    group.add_argument("number", nargs="?", type=int, metavar="N")
    group.add_argument("--all", action="store_true", dest="all_entries")
    return parser


def _run(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    home = store_dir()
    if args.command == "list":
        for i, e in enumerate(load_entries(home), 1):
            out.write(f"{i}. {e['ts']} {e['kind']}: {e['text']}\n")
        return 0
    if args.command == "add":
        text = normalise(args.text)
        if not text:
            err.write("memory text is empty after cleaning\n")
            return 2
        if len(text) > MAX_TEXT:
            err.write(f"memory text is {len(text)} chars; the limit is {MAX_TEXT}\n")
            return 2
        with _transaction(home) as dfd:
            entries = _read_entries(dfd)
            today = datetime.date.today().isoformat()
            entries.append({"ts": today, "kind": args.kind, "text": text})
            entries = entries[-MAX_ENTRIES:]
            _write_entries(dfd, entries)
        out.write(f"saved ({len(entries)} entries)\n")
        return 0
    with _transaction(home) as dfd:
        entries = _read_entries(dfd)
        if args.all_entries:
            entries = []
        else:
            n = args.number
            if not 1 <= n <= len(entries):
                err.write(f"no memory numbered {n}; have {len(entries)}\n")
                return 2
            del entries[n - 1]
        _write_entries(dfd, entries)
    out.write(f"forgot ({len(entries)} entries left)\n")
    return 0


def main(
    argv: Sequence[str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    args = _build_parser().parse_args(argv)
    try:
        deny = os.path.abspath(os.path.expanduser(args.deny_under))
        if _cwd_under(os.getcwd(), deny):
            err.write("memory is personal-only; refusing to run in the work directory\n")
            return 2
        return _run(args, out, err)
    except StoreError as exc:
        err.write(f"memory: {exc}\n")
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        err.write(f"memory: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())

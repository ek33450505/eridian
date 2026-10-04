"""Voice turn guard: a PreToolUse hook that gates tool calls made on a spoken turn.

`undertone session` launches an interactive `claude` with a voice channel, so a
transcribed utterance arrives as a channel event with full tool power. Claude's
instructions ask it to get a spoken "confirm <word1> <word2>" before anything
destructive, but that is a soft control. This module is the hard one:

  - the channel records each utterance and each confirm request in a small
    per-session state file (record_voice / record_confirm_request);
  - Claude Code hooks (SessionStart, UserPromptSubmit, PreToolUse) run
    `python -m undertone.voice_guard <event> --home PATH --session TOKEN`;
  - a channel event also fires UserPromptSubmit, with a prompt wrapped in
    `<channel ...>`; typed prompts never carry that wrapper. Injected non-user
    messages (`<task-notification>`, `<system-reminder>`, ...) fire it too, so
    only a plain non-envelope prompt counts as typed;
  - a Stop hook marks the end of a turn: a voice turn stays gated until its own
    Stop, even if the user types mid-turn, and a grant ends there;
  - subagents are tainted unless proven typed-origin: background subagents
    outlive their turn, so an agent started during a voice turn (or while a
    voice-born agent is alive, as its child) keeps the voice gate and any grant
    it inherited. Only SubagentStart records agents; an agent_id PreToolUse
    cannot find (missed start, eviction, early stop, invalid id) is tainted;
  - while the latest turn was spoken, any tool call outside a read-only
    allowlist is denied until the user speaks the exact confirm phrase.

Fail-closed rules (Claude Code lets the tool run on any exit code but 0 or 2):
  - pretooluse turns every error into exit 2 (block, stderr goes to Claude);
  - userpromptsubmit exits 2 on failure only when the input carries a channel
    tag (dropping a voice turn is the safe side); a typed prompt always gets
    exit 0, because exit 2 would erase what the user typed;
  - sessionstart, stop, subagentstart and subagentstop ALWAYS exit 0 (exit 2 on Stop forces Claude to go on);
  - an allow is an empty exit 0, never an explicit "allow", so the user's
    normal permission prompts still apply.

State lives in <home>/sessions/<token>.json (0600, dir 0700), updated under an
flock. The log, <home>/logs/guard.log, never holds command or prompt text. The state
file also keeps a short spoken summary of up to 3 blocked calls (program, file and
host names) for the confirm read-back.

Imports stay light on purpose: the hook starts a fresh interpreter per tool
call, so this module must not pull in undertone.paths or anything heavy.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import shlex
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, TextIO

TOKEN_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)
GRANT_TTL_S = 300.0
CHALLENGE_TTL_S = 300.0
LOCK_TIMEOUT_S = 2.0
MAX_USED = 200
MAX_STATE_BYTES = 64 * 1024
MAX_AGENTS = 100
MAX_AGENT_ID = 128
MAX_BLOCKED = 3
MAX_WHAT_CHARS = 120
NOTHING_BLOCKED = "undertone has not blocked anything yet."
_NO_AGENT: Any = object()  # payload had no agent_id key: the main thread

STOPWORDS = frozenset({
    "the", "and", "yes", "okay", "then", "say", "now", "please", "go", "to",
    "this", "that", "it", "you", "wake", "word", "phrase", "hey", "jarvis",
})

DENY_REASON = (
    "undertone voice guard: this {tool} call is part of an unconfirmed voice turn. "
    "Ask the user with the reply tool (kind=\"confirm\") to say the wake word, then "
    "\"confirm <word1> <word2>\", and retry only after that exact phrase arrives. "
    "Do not try a different tool or command to get around this. "
    "If the user is at the keyboard, they can type the request once the current voice turn has finished."
)

UNGATED_TOOLS = frozenset({
    "Read", "Grep", "Glob", "LS", "WebSearch", "TodoWrite", "Task", "Agent",
    "AskUserQuestion", "ToolSearch", "Skill", "EnterPlanMode", "ExitPlanMode",
    "BashOutput", "TaskOutput", "mcp__undertone__reply",
})

# Exact MCP tool names that run ungated on a voice turn. They live in code, not a
# config file, because a typed turn could edit a config file to widen the voice
# gate. Each one is vetted read-only: cast-record opens cast.db with mode=ro and
# exposes no SQL tool (claude-agent-team scripts/cast-mcp-server.py, 2026-10-03).
READ_ONLY_MCP_TOOLS = frozenset({
    "mcp__cast-record__cast_decisions", "mcp__cast-record__cast_incidents",
    "mcp__cast-record__cast_cost", "mcp__cast-record__cast_sessions",
    "mcp__cast-record__cast_ask",
})

PLAIN_READ = frozenset({
    "ls", "pwd", "echo", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep",
    "which", "type", "whoami", "uname", "basename", "dirname", "realpath", "stat",
    "du", "df", "true", "false",
})
GIT_LEADING = frozenset({"--no-pager", "-P", "--no-optional-locks"})
GIT_READ = frozenset({
    "status", "log", "diff", "show", "blame", "rev-parse", "ls-files", "describe",
    "shortlog",
})
GIT_LISTING = frozenset({"branch", "remote", "tag"})
GIT_LISTING_ARGS = frozenset({"-a", "-r", "-v", "-vv", "-l", "--list", "--show-current"})
# git accepts abbreviated long options, so match on prefixes of these.
GIT_UNSAFE_LONG = (("--output", 4), ("--ext-diff", 5), ("--textconv", 7))
# An allowlist, not a deny list: bfs (which may stand in for find) keeps adding
# actions (`-rm`), so any dash argument outside this set is gated.
FIND_SAFE = frozenset({
    "-name", "-iname", "-path", "-ipath", "-type", "-maxdepth", "-mindepth", "-print",
    "-print0", "-newer", "-size", "-mtime", "-mmin", "-empty", "-not", "-and", "-or",
    "-o", "-a", "-prune",
})
SAFE_ENV = frozenset({"CI", "NO_COLOR", "FORCE_COLOR", "TERM", "LANG", "LC_ALL", "TZ",
                      "PYTHONDONTWRITEBYTECODE"})


class _Runner:
    """A test runner: allowed command prefixes plus the only arguments it may take."""

    def __init__(self, prefixes: tuple[tuple[str, ...], ...], safe: str, values: str, plain: bool):
        self.prefixes = prefixes
        self.safe = frozenset(safe.split())
        self.values = frozenset(values.split())
        self.plain = plain


_PYTHONS = (("python",), ("python3",), ("uv", "run", "python"), ("uv", "run", "python3"))
_PYTEST_PREFIXES = tuple(pre + ("-m", "pytest") for pre in _PYTHONS) + (("pytest",), ("uv", "run", "pytest"))
_UNITTEST_PREFIXES = tuple(pre + ("-m", "unittest") for pre in _PYTHONS)
RUNNERS = (
    _Runner((("npm", "test"), ("npm", "t"), ("pnpm", "test"), ("yarn", "test"), ("bun", "test"),
             ("npm", "run", "test")),
            "-- --run --watch=false --no-watch --silent --ci --coverage", "", True),
    _Runner(_PYTEST_PREFIXES,
            "-q -qq -v -vv -x -s -l --no-header --lf --ff --sw -ra -rA --tb=short --tb=line "
            "--tb=no --tb=long --tb=auto --co --collect-only --exitfirst --verbose --quiet",
            "-k -m", True),
    _Runner(_UNITTEST_PREFIXES,
            "-v -q -f -b -c --failfast --buffer --catch --locals --verbose --quiet",
            "-s -t -p -k", True),
    _Runner((("bats",),), "-t --tap -r --recursive -p --pretty -T --timing -x --trace -c --count",
            "-f --filter", True),
    _Runner((("make", "test"), ("make", "check"), ("just", "test"), ("just", "check")), "", "", False),
    _Runner((("cargo", "test"),),
            "-q -v --release --lib --doc --workspace --all --no-fail-fast --quiet --verbose", "", True),
    _Runner((("go", "test"),), "-v -race -short -cover -failfast -json -count=1",
            "-run -count -timeout", True),
)
# Values of these flags are paths, so they get the same check as plain arguments.
PATH_VALUE_FLAGS = frozenset({"-s", "-t"})

_PLAIN_ARG_RE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./:@\[\]+-]*", re.ASCII)
_DOTDOT_RE = re.compile(r"(^|/)\.\.(/|$)", re.ASCII)
_CHANNEL_RE = re.compile(r"<channel\b", re.ASCII)
# An fd number glued to a redirect (`2>&1`) is not an argument; `2 >&1` is.
_FD_REDIRECT_RE = re.compile(r"(?:(?<=\s)|^)[0-9]+(?=>)", re.ASCII)
_FD_RE = re.compile(r"[0-9]+", re.ASCII)
_PRINTABLE_ASCII_RE = re.compile(r"[\x20-\x7e\t\n]*", re.ASCII)
_ENV_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=", re.ASCII)
_SEPARATOR_CHARS = frozenset(";&|")
_OPERATOR_CHARS = frozenset("();<>|&")
_CONFIRM_RE = re.compile(r"\bconfirm ([a-z]+) ([a-z]+)\b", re.ASCII)


class GuardError(Exception):
    """State is unreadable, unsafe or contended; callers fail closed."""


class ConfirmPhraseError(GuardError):
    """A confirm reply did not carry a usable phrase; the message is user-facing."""


# -- state ----------------------------------------------------------------


def _default_state() -> dict[str, Any]:
    return {
        "armed_at": None,
        "voice_at": None,
        "typed_at": None,
        "stopped_at": None,
        "challenge": None,
        "grant": None,
        "used_phrases": [],
        "agents": {},
        "blocked": [],
    }


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_agent_id(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_AGENT_ID


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _validate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise GuardError("state is not a JSON object")
    state = _default_state()
    for key in ("armed_at", "voice_at", "typed_at", "stopped_at"):
        value = raw.get(key)
        if value is not None and not _is_num(value):
            raise GuardError(f"state field {key} has the wrong type")
        state[key] = value
    used = raw.get("used_phrases", [])
    if not _is_str_list(used):
        raise GuardError("state field used_phrases has the wrong type")
    state["used_phrases"] = used
    challenge = raw.get("challenge")
    if challenge is not None:
        if not (isinstance(challenge, dict) and isinstance(challenge.get("phrase"), str)
                and _is_num(challenge.get("at"))):
            raise GuardError("state field challenge has the wrong type")
    state["challenge"] = challenge
    grant = raw.get("grant")
    if grant is not None:
        if not (isinstance(grant, dict) and _is_num(grant.get("at"))):
            raise GuardError("state field grant has the wrong type")
    state["grant"] = grant
    agents = raw.get("agents", {})
    if not isinstance(agents, dict) or len(agents) > MAX_AGENTS:
        raise GuardError("state field agents has the wrong type")
    clean: dict[str, dict[str, Any]] = {}
    for agent_id, rec in agents.items():
        if not (_valid_agent_id(agent_id) and isinstance(rec, dict)
                and isinstance(rec.get("voice"), bool)
                and (rec.get("granted_at") is None or _is_num(rec.get("granted_at")))
                and _is_num(rec.get("started_at"))):
            raise GuardError("state field agents has the wrong type")
        clean[agent_id] = {"voice": rec["voice"], "granted_at": rec.get("granted_at"),
                           "started_at": rec["started_at"]}
    state["agents"] = clean
    blocked = raw.get("blocked", [])
    if not isinstance(blocked, list) or len(blocked) > MAX_BLOCKED:
        raise GuardError("state field blocked has the wrong type")
    for entry in blocked:
        if not (isinstance(entry, dict) and _is_num(entry.get("at"))
                and isinstance(entry.get("what"), str) and 0 < len(entry["what"]) <= MAX_WHAT_CHARS):
            raise GuardError("state field blocked has the wrong type")
    state["blocked"] = [{"at": e["at"], "what": e["what"]} for e in blocked]
    return state


def _check_token(token: Any) -> str:
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise GuardError("bad session token")
    return token


def _sessions_dir(home: Any, create: bool) -> Path | None:
    path = Path(home) / "sessions"
    if path.is_symlink():
        raise GuardError("sessions dir is a symlink")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    elif not path.is_dir():
        return None
    return path


def _read_state_file(path: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GuardError(f"cannot open state file: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not (st.st_mode & 0o170000) == 0o100000:
            raise GuardError("state file is not a regular file")
        if st.st_size > MAX_STATE_BYTES:
            raise GuardError("state file is too large")
        data = fh.read(MAX_STATE_BYTES + 1)
    if len(data) > MAX_STATE_BYTES:
        raise GuardError("state file is too large")
    try:
        raw = json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise GuardError("state file is not valid JSON") from exc
    return _validate(raw)


def load_state(home: Any, token: str) -> dict[str, Any] | None:
    """Return the session state, None if there is none, or raise GuardError."""
    _check_token(token)
    sessions = _sessions_dir(home, create=False)
    if sessions is None:
        return None
    return _read_state_file(sessions / f"{token}.json")


def _lock(sessions: Path, token: str) -> int:
    fd = os.open(sessions / f"{token}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise GuardError("timed out waiting for the state lock") from None
            time.sleep(0.01)
        except OSError:
            os.close(fd)
            raise


def _update(home: Any, token: str, fn: Callable[[dict[str, Any]], Any]) -> Any:
    """Load the state under an exclusive lock, apply fn, write it back atomically."""
    _check_token(token)
    try:
        sessions = _sessions_dir(home, create=True)
        assert sessions is not None
        lock_fd = _lock(sessions, token)
    except OSError as exc:
        raise GuardError(f"cannot prepare state dir: {exc.strerror}") from exc
    try:
        path = sessions / f"{token}.json"
        state = _read_state_file(path) or _default_state()
        result = fn(state)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=sessions, prefix=f".{token}.", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, separators=(",", ":"))
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return result
    except OSError as exc:
        raise GuardError(f"cannot write state: {exc.strerror}") from exc
    finally:
        os.close(lock_fd)


# -- transitions ----------------------------------------------------------


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]", " ", text.lower()).split())


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def record_armed(home: Any, token: str, *, now: float | None = None) -> None:
    stamp = _now(now)

    def fn(state: dict[str, Any]) -> None:
        state["armed_at"] = stamp

    _update(home, token, fn)


def record_voice(
    home: Any, token: str, text: str, *, can_grant: bool = False, now: float | None = None
) -> bool:
    """Note a spoken turn; True when it carried the pending confirm phrase.

    Every voice turn consumes the pending challenge (one attempt), but only a
    turn with `can_grant` (opened by the wake word) may satisfy it -- a
    talk-over or follow-up turn could be our own speech or ambient audio.
    Defaults to False so a caller that forgets fails closed.
    """
    stamp = _now(now)
    norm = normalize(text)

    def fn(state: dict[str, Any]) -> bool:
        state["voice_at"] = stamp
        challenge = state["challenge"]
        granted = bool(
            can_grant
            and challenge
            and stamp - challenge["at"] <= CHALLENGE_TTL_S
            and f" confirm {challenge['phrase']} " in f" {norm} "
        )
        state["grant"] = {"at": stamp} if granted else None
        state["challenge"] = None  # one attempt per challenge
        state["blocked"] = []  # a new utterance starts fresh
        return granted

    return _update(home, token, fn)


def record_confirm_request(home: Any, token: str, reply_text: str, *, now: float | None = None) -> str:
    """Arm a challenge from Claude's confirm reply and return its phrase."""
    stamp = _now(now)
    candidates = _CONFIRM_RE.findall(normalize(reply_text))

    def fn(state: dict[str, Any]) -> str:
        used = state["used_phrases"]
        saw_used = False
        for w1, w2 in reversed(candidates):
            if len(w1) < 3 or len(w2) < 3 or w1 in STOPWORDS or w2 in STOPWORDS or w1 == w2:
                continue
            phrase = f"{w1} {w2}"
            if phrase in used:
                saw_used = True
                continue
            state["challenge"] = {"phrase": phrase, "at": stamp}
            state["used_phrases"] = (used + [phrase])[-MAX_USED:]
            return phrase
        if saw_used:
            raise ConfirmPhraseError(
                "Not spoken: that confirm phrase was already used in this session; pick two new words."
            )
        raise ConfirmPhraseError(
            "Not spoken: a confirm reply must ask the user to say the wake word, then "
            "'confirm <word1> <word2>' using two uncommon words of three or more letters."
        )

    return _update(home, token, fn)


def record_typed(home: Any, token: str, *, now: float | None = None) -> None:
    stamp = _now(now)

    def fn(state: dict[str, Any]) -> None:
        state["typed_at"] = stamp
        state["grant"] = None
        state["challenge"] = None
        state["blocked"] = []
        for rec in state["agents"].values():
            rec["granted_at"] = None  # a typed "stop" revokes inherited grants

    _update(home, token, fn)


def record_stopped(home: Any, token: str, *, now: float | None = None) -> None:
    """Mark a turn boundary: the grant ends here; a pending challenge survives."""
    stamp = _now(now)

    def fn(state: dict[str, Any]) -> None:
        state["stopped_at"] = stamp
        state["grant"] = None

    _update(home, token, fn)


def _record_agent(state: dict[str, Any], agent_id: str, now: float) -> None:
    """Record an agent's origin at SubagentStart; an existing entry is never overwritten."""
    agents = state["agents"]
    if agent_id in agents:
        return
    # A child of a live voice-born agent is voice-born, even after the user typed.
    # No age limit: a voice-born agent can live arbitrarily long. A crashed one that
    # never sends SubagentStop keeps tainting until the session restarts.
    live_taint = any(r["voice"] for r in agents.values())
    voice = voice_active(state) or live_taint
    agents[agent_id] = {
        "voice": voice,
        "granted_at": now if voice and grant_valid(state, now) else None,
        "started_at": now,
    }
    while len(agents) > MAX_AGENTS:
        # Evict typed-origin entries first: an evicted agent becomes unknown, which
        # is tainted, so dropping a voice entry would be the less safe loss.
        victim = next((k for k, r in agents.items() if not r["voice"]), next(iter(agents)))
        del agents[victim]


def record_subagent_start(home: Any, token: str, agent_id: str, *, now: float | None = None) -> None:
    stamp = _now(now)
    _update(home, token, lambda state: _record_agent(state, agent_id, stamp))


def record_subagent_stop(home: Any, token: str, agent_id: str, *, now: float | None = None) -> None:
    _update(home, token, lambda state: state["agents"].pop(agent_id, None))


# -- decision -------------------------------------------------------------


def voice_active(state: dict[str, Any]) -> bool:
    """True while the latest turn is spoken, or a spoken turn has not yet stopped."""
    voice_at = state.get("voice_at")
    typed_at = state.get("typed_at")
    stopped_at = state.get("stopped_at")
    if not _is_num(voice_at):
        return False
    in_flight = stopped_at is None or stopped_at < voice_at
    return typed_at is None or voice_at > typed_at or in_flight


def grant_valid(state: dict[str, Any], now: float) -> bool:
    grant = state.get("grant")
    if not grant:
        return False
    return 0 <= now - grant["at"] <= GRANT_TTL_S


def _git_reason(args: list[str]) -> str | None:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] not in GIT_LEADING:
            return "git option"
        i += 1
    if i >= len(args):
        return "git"
    sub, rest = args[i], args[i + 1:]
    if sub in GIT_READ:
        for arg in rest:
            if not arg.startswith("--"):
                continue
            name = arg.split("=", 1)[0]
            for full, min_len in GIT_UNSAFE_LONG:
                if len(name) >= min_len and full.startswith(name):
                    return f"git {full}"
        return None
    if sub in GIT_LISTING:
        return None if all(a in GIT_LISTING_ARGS for a in rest) else f"git {sub}"
    return f"git {sub}"


def _plain_arg(arg: str) -> bool:
    return bool(_PLAIN_ARG_RE.fullmatch(arg)) and not arg.startswith("/") and not _DOTDOT_RE.search(arg)


def _runner_args_ok(runner: _Runner, args: list[str]) -> bool:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in runner.safe:
            i += 1
        elif arg in runner.values:
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                return False
            if arg in PATH_VALUE_FLAGS and not _plain_arg(args[i + 1]):
                return False
            i += 2
        elif runner.plain and _plain_arg(arg):
            i += 1
        else:
            return False
    return True


def _runner_segment_ok(seg: list[str]) -> bool:
    for runner in RUNNERS:
        for prefix in runner.prefixes:
            if tuple(seg[:len(prefix)]) != prefix:
                continue
            rest = seg[len(prefix):]
            if prefix == ("npm", "run", "test"):
                return _runner_args_ok(runner, rest)
            if _runner_args_ok(runner, rest):
                return True
    if seg[:2] == ["npm", "run"] and len(seg) > 2 and seg[2].startswith("test:"):
        return _runner_args_ok(RUNNERS[0], seg[3:])
    return False


def _segment_reason(seg: list[str]) -> str | None:
    cmd, args = seg[0], seg[1:]
    if cmd in PLAIN_READ:
        return None
    if cmd == "cd":
        return None if len(args) == 1 and _plain_arg(args[0]) else "cd"
    if cmd == "find":
        return "find" if any(a.startswith("-") and a not in FIND_SAFE for a in args) else None
    if cmd == "git":
        return _git_reason(args)
    return None if _runner_segment_ok(seg) else cmd


def bash_gate_reason(command: str) -> str | None:
    """Fail-closed allowlist: None only for read-only or test-run command lines."""
    # The shell expands after this check but shlex sees one word: `#` is a
    # comment to shlex but a literal mid-word to bash/zsh; `$` opens parameter,
    # command and arithmetic expansion; braces and globs turn one word into
    # several (`--out{put=f,}`, or a file named `--output=x`). Voice-turn Bash
    # needs none of them, so over-gating is the safe direction.
    if not _PRINTABLE_ASCII_RE.fullmatch(command):
        return "non-ASCII or control character"
    # shlex drops empty quotes ('' and "") but zsh keeps them as empty words.
    if "''" in command or '""' in command:
        return "empty quotes"
    if "`" in command or "~[" in command or any(c in command for c in "$#{}*?[]\\^"):
        return "shell expansion"
    cmd = command.replace("\n", " ; ").replace("\r", " ; ")
    cmd = _FD_REDIRECT_RE.sub("", cmd)
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        return "unparseable command"

    segments: list[list[str]] = [[]]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        # With punctuation_chars, an unquoted operator is a token made only of
        # operator characters; a quoted "a > b" stays an ordinary argument.
        if not set(tok) <= _OPERATOR_CHARS:
            segments[-1].append(tok)
        elif set(tok) <= _SEPARATOR_CHARS:
            segments.append([])
        elif tok == ">&" and nxt is not None and _FD_RE.fullmatch(nxt):
            i += 1
        elif tok in (">", ">>", "&>", "&>>") and nxt == "/dev/null":
            i += 1
        elif tok == "<" and nxt is not None:
            i += 1
        elif "<" in tok:
            return "input redirect"
        elif ">" in tok:
            return "output redirect"
        else:
            return "subshell"
        i += 1

    live = []
    for seg in segments:
        while seg and (m := _ENV_ASSIGN_RE.match(seg[0])):
            if m.group(1) not in SAFE_ENV:
                return "env assignment"
            seg = seg[1:]
        if seg:
            live.append(seg)
    if not live:
        return "empty command"
    for seg in live:
        reason = _segment_reason(seg)
        if reason is not None:
            return reason
    return None


def gate_reason(tool_name: str, tool_input: Any) -> str | None:
    """None when the tool call is ungated, else a short log reason (never command text)."""
    if tool_name in UNGATED_TOOLS or tool_name in READ_ONLY_MCP_TOOLS:
        return None
    if tool_name == "Bash":
        if not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str):
            return "Bash"
        reason = bash_gate_reason(tool_input["command"])
        return None if reason is None else f"Bash:{reason}"
    return tool_name


# -- spoken summary of a blocked call ---------------------------------------


def _cut(text: str, limit: int) -> str:
    """Cut at a word boundary to at most `limit` chars."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    if text[limit] != " " and " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.strip(" ,")


def _speakable(text: str, limit: int = MAX_WHAT_CHARS) -> str:
    words = (w.strip(".") for w in re.sub(r"[^A-Za-z0-9.]+", " ", text).split())
    return _cut(" ".join(w for w in words if w), limit)


def _path_words(path: str) -> str:
    special = {".": "this folder", "..": "parent folder", "*": "everything"}
    if path in special:
        return special[path]
    prefix = ""
    if path.startswith("~"):
        prefix, path = "home ", path[1:]
    elif path.startswith("/"):
        prefix, path = "root ", path[1:]
    return (prefix + _speakable(path)).strip()


def _arg_words(arg: str) -> str:
    if arg.startswith("-"):
        return _speakable(arg)
    if "/" in arg or arg.startswith(("~", ".")) or arg == "*":
        return _path_words(arg)
    return _speakable(arg)


_UNREADABLE_BASH = "a shell command undertone could not read"
_COMPLEX_MARKERS = ("`", "$(", "<(", ">(", "<<")
_SUBCOMMAND_PROGRAMS = frozenset({
    "git", "npm", "pnpm", "yarn", "uv", "cargo", "go", "make", "just", "brew", "pip",
    "pip3", "docker", "kubectl", "gh",
})
_SINGLE_PHRASE_CHARS = 100


def _bash_segments(command: str) -> list[list[str]]:
    """Split into [program, *args] segments, env assignments dropped. Raises ValueError."""
    lex = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""  # bash treats `a#` as a word, not a comment
    segments: list[list[str]] = [[]]
    for tok in lex:
        if set(tok) <= _SEPARATOR_CHARS:
            segments.append([])
        elif not set(tok) <= _OPERATOR_CHARS:
            segments[-1].append(tok)
    out = []
    for seg in segments:
        while seg and _ENV_ASSIGN_RE.match(seg[0]):
            seg = seg[1:]
        if seg:
            out.append(seg)
    return out


def _program_words(tok: str, limit: int) -> str:
    return _speakable(os.path.basename(tok), limit) or "unknown"


def _bash_words(command: str) -> str:
    """Vague is fine; never omit a program that runs or pass a partial list off as whole."""
    try:
        segments = _bash_segments(command)
    except ValueError:
        return _UNREADABLE_BASH
    if not segments:
        return _UNREADABLE_BASH
    if any(marker in command for marker in _COMPLEX_MARKERS):
        first = _speakable(os.path.basename(segments[0][0]), 40)
        return f"a complex shell command starting with {first}" if first else _UNREADABLE_BASH
    if len(segments) == 1:
        seg = segments[0]
        phrase = f"the shell command {_program_words(seg[0], 60)}"
        for i, arg in enumerate(seg[1:]):
            words = _arg_words(arg)
            if not words:
                if arg.strip():
                    return phrase + " and more"
                continue
            if len(phrase) + 1 + len(words) > _SINGLE_PHRASE_CHARS:
                return phrase + " and more"
            phrase += " " + words
        return phrase
    total = len(segments)
    shown = segments[:3]
    more = f", and {total - 3} more commands" if total > 3 else ""
    for with_sub in (True, False):
        names = []
        for seg in shown:
            name = _program_words(seg[0], 20)
            if with_sub and name in _SUBCOMMAND_PROGRAMS:
                sub = next((a for a in seg[1:] if not a.startswith("-")), "")
                name = f"{name} {_speakable(sub, 16)}".strip()
            names.append(name)
        text = "the shell commands " + ", then ".join(names) + more
        if len(text) <= MAX_WHAT_CHARS:
            return text
    return f"the shell commands {_program_words(segments[0][0], 20)} and {total - 1} more commands"


def _file_words(path: Any) -> str:
    parts = [p for p in str(path).split("/") if p] if isinstance(path, str) else []
    if not parts:
        return ""
    name = _path_words(parts[-1])
    if len(parts) == 1:
        return name
    return f"{name} in {_path_words(parts[-2])}"


def _describe(tool_name: str, tool_input: Any) -> str:
    args = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash":
        command = args.get("command")
        return _bash_words(command) if isinstance(command, str) else "a shell command undertone could not read"
    for names, verb, noun, key in (
        (("Write",), "write", "file", "file_path"),
        (("Edit", "MultiEdit"), "edit", "file", "file_path"),
        (("NotebookEdit",), "edit", "notebook", "notebook_path"),
    ):
        if tool_name in names:
            where = _file_words(args.get(key))
            return f"{verb} the {noun} {where}" if where else f"{verb} a {noun}"
    if tool_name == "WebFetch":
        url = args.get("url")
        host = urllib.parse.urlsplit(url).hostname if isinstance(url, str) else None
        host_words = _speakable(host) if host else ""
        return f"fetch a web page from {host_words}" if host_words else "fetch a web page"
    if tool_name.startswith("mcp__") and tool_name.count("__") >= 2:
        server, tool = tool_name[5:].split("__", 1)
        text = f"the {_speakable(server)} tool {_speakable(tool)}"
        extras = []
        for key, value in args.items():
            if isinstance(value, (str, int, float, bool)):
                words = _speakable(str(value), 24)
                if words and _speakable(str(key)):
                    extras.append(f", {_speakable(str(key))} {words}")
            if len(extras) == 2:
                break
        return text + "".join(extras)
    return f"the {_speakable(tool_name)} tool"


def describe_call(tool_name: Any, tool_input: Any) -> str:
    """A short plain-words phrase for what a call would do; never raises.

    Output holds only [A-Za-z0-9. ,] and at most MAX_WHAT_CHARS characters.
    """
    try:
        text = _describe(tool_name, tool_input)
    except Exception:
        try:
            text = f"the {_speakable(tool_name)} tool"
        except Exception:
            text = "a tool call"
    text = _cut(re.sub(r"[^A-Za-z0-9. ,]", "", text), MAX_WHAT_CHARS)
    return text if text.strip() else "a tool call"


def record_blocked(home: Any, token: str, what: str, *, now: float | None = None) -> None:
    """Remember a denied call's spoken summary (latest MAX_BLOCKED, deduped)."""
    stamp = _now(now)

    def fn(state: dict[str, Any]) -> None:
        kept = [e for e in state["blocked"] if e["what"] != what]
        state["blocked"] = (kept + [{"at": stamp, "what": what}])[-MAX_BLOCKED:]

    _update(home, token, fn)


def read_back(home: Any, token: str, *, now: float | None = None) -> str:
    """Spoken summary of the recently blocked calls, for the confirm request."""
    stamp = _now(now)
    state = load_state(home, token)
    whats = [
        e["what"] for e in (state["blocked"] if state else [])
        if 0 <= stamp - e["at"] <= CHALLENGE_TTL_S
    ]
    if not whats:
        return NOTHING_BLOCKED
    return "This will run: " + "; and ".join(whats) + "."


def _agent_tainted(state: dict[str, Any], agent_id: Any) -> bool:
    """True for a subagent call that is voice-born or unknown; False for the main
    thread and for an agent recorded as typed-origin."""
    if agent_id is _NO_AGENT or state.get("voice_at") is None:
        return False
    rec = state["agents"].get(agent_id) if _valid_agent_id(agent_id) else None
    return rec is None or rec["voice"]


def decide(
    state: dict[str, Any] | None, tool_name: str, tool_input: Any, now: float,
    agent_id: Any = _NO_AGENT,
) -> str | None:
    """Return a deny reason, or None to allow.

    A subagent call is ungated only if the agent was recorded as typed-origin;
    an unknown or invalid agent_id is voice-tainted with no inherited grant.
    """
    if state is None:
        return None
    tainted = _agent_tainted(state, agent_id)
    if not tainted and not voice_active(state):
        return None
    if gate_reason(tool_name, tool_input) is None:
        return None
    if tainted and _valid_agent_id(agent_id):
        rec = state["agents"].get(agent_id)
        granted_at = rec["granted_at"] if rec else None
        if granted_at is not None and 0 <= now - granted_at <= GRANT_TTL_S:
            return None
    if grant_valid(state, now):
        return None
    return DENY_REASON.format(tool=tool_name)


# -- hook entrypoint ------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # argparse would print and exit 2
        raise GuardError(f"bad arguments: {message}")


def _log(home: Any, event: str, decision: str, what: str, reason: str = "-") -> None:
    """Append one line to guard.log; never holds command or prompt text."""
    try:
        logs = Path(home) / "logs"
        if logs.is_symlink():
            return
        logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(logs, 0o700)
        fd = os.open(logs / "guard.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {event} {decision} {what} {reason}\n"
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
    except (OSError, TypeError, ValueError):
        pass  # logging must never change the hook's outcome


def _parse_payload(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def _raw_has_channel(raw: str) -> bool:
    """Channel-tag test on raw hook stdin (also catches a prompt in unparseable JSON)."""
    if _CHANNEL_RE.search(raw):
        return True
    payload = _parse_payload(raw)
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    return isinstance(prompt, str) and bool(_CHANNEL_RE.search(prompt))


def _sessionstart(home: Any, token: str) -> None:
    record_armed(home, token)
    _log(home, "sessionstart", "armed", "-")


def _stop(home: Any, token: str) -> None:
    record_stopped(home, token)
    _log(home, "stop", "ok", "-")


def _userpromptsubmit(home: Any, token: str, raw: str) -> None:
    payload = _parse_payload(raw)
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if not isinstance(prompt, str):
        if _raw_has_channel(raw):
            raise GuardError("unreadable channel prompt")
        _log(home, "userpromptsubmit", "ok", "unchanged")
        return
    stamp = time.time()
    if _CHANNEL_RE.search(prompt):
        def fn(state: dict[str, Any]) -> None:
            # Leave grant and challenge alone: a confirm utterance's own prompt
            # event arrives after record_voice created its grant.
            state["voice_at"] = max(state["voice_at"] or 0, stamp)

        _update(home, token, fn)
        _log(home, "userpromptsubmit", "ok", "voice")
    elif prompt.lstrip().startswith("<") or not prompt.strip():
        # An injected envelope (task notification, teammate message, system
        # reminder) or nothing at all: not the user typing, so no state change.
        _log(home, "userpromptsubmit", "ok", "other")
    else:
        record_typed(home, token, now=stamp)
        _log(home, "userpromptsubmit", "ok", "typed")


def _agent_tag(agent_id: str | None) -> str:
    """Log suffix identifying a subagent; sanitized so an id cannot forge log lines."""
    return f" a:{re.sub(r'[^A-Za-z0-9_-]', '', agent_id)[:8]}" if agent_id else ""


def _agent_event(event: str, home: Any, token: str, raw: str) -> None:
    payload = _parse_payload(raw)
    agent_id = payload.get("agent_id") if isinstance(payload, dict) else None
    if not _valid_agent_id(agent_id):
        return
    if event == "subagentstart":
        record_subagent_start(home, token, agent_id)
    else:
        record_subagent_stop(home, token, agent_id)
    _log(home, event, "ok", "-", _agent_tag(agent_id).strip())


def _pretooluse(home: Any, token: str, raw: str, stdout: TextIO) -> None:
    payload = _parse_payload(raw)
    if not isinstance(payload, dict):
        raise GuardError("bad hook input")
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str):
        raise GuardError("bad hook input")
    tool_input = payload.get("tool_input")
    agent_id = payload["agent_id"] if "agent_id" in payload else _NO_AGENT
    tag = _agent_tag(agent_id if _valid_agent_id(agent_id) else None) if agent_id is not _NO_AGENT else ""
    if agent_id is not _NO_AGENT and not _valid_agent_id(agent_id):
        tag = " a:invalid"
    state = load_state(home, token)
    now = time.time()
    reason = decide(state, tool_name, tool_input, now, agent_id)
    if reason is None:
        gated = (
            state is not None
            and (voice_active(state) or _agent_tainted(state, agent_id))
            and gate_reason(tool_name, tool_input) is not None
        )
        _log(home, "pretooluse", "granted" if gated else "allow", tool_name, f"-{tag}")
        return
    _log(home, "pretooluse", "deny", tool_name, f"{gate_reason(tool_name, tool_input) or '-'}{tag}")
    stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    stdout.flush()
    try:
        record_blocked(home, token, describe_call(tool_name, tool_input))
    except Exception:
        _log(home, "pretooluse", "error", tool_name, "record_blocked")


_EVENTS = ("sessionstart", "userpromptsubmit", "stop", "subagentstart", "subagentstop", "pretooluse")


def _event_of(argv: list[str]) -> str:
    """Best-effort event name for the error path, when argument parsing may have failed.

    The event is normally the first argument, so `--home pretooluse` does not
    turn a prompt hook into a blocking one; with no recognizable event at all,
    block.
    """
    if argv and argv[0] in _EVENTS:
        return argv[0]
    if "pretooluse" in argv:
        return "pretooluse"
    for event in _EVENTS:
        if event in argv:
            return event
    return "pretooluse"


def main(
    argv: list[str] | None = None,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        raw = stdin.read()
    except (OSError, ValueError):
        raw = ""
    home: Any = None
    try:
        parser = _Parser(prog="undertone.voice_guard")
        parser.add_argument("event", choices=_EVENTS)
        parser.add_argument("--home", required=True)
        parser.add_argument("--session", required=True)
        args = parser.parse_args(args_list)
        home = args.home
        if args.event == "pretooluse":
            _pretooluse(home, args.session, raw, stdout)
        elif args.event == "userpromptsubmit":
            _userpromptsubmit(home, args.session, raw)
        elif args.event == "stop":
            _stop(home, args.session)
        elif args.event in ("subagentstart", "subagentstop"):
            _agent_event(args.event, home, args.session, raw)
        else:
            _sessionstart(home, args.session)
        return 0
    except (Exception, SystemExit) as exc:
        event = _event_of(args_list)
        _log(home, event, "error", "-", type(exc).__name__)
        if event == "pretooluse":
            reason = exc if isinstance(exc, GuardError) else type(exc).__name__
            stderr.write(f"undertone voice guard: failing closed: {reason}\n")
            return 2
        if event == "userpromptsubmit" and _raw_has_channel(raw):
            stderr.write("undertone voice guard: failing closed; voice turn dropped\n")
            return 2
        return 0  # exit 2 on a typed prompt would erase what the user typed


if __name__ == "__main__":
    sys.exit(main())

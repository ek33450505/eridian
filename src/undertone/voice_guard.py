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
    allowlist is denied until the user speaks the confirm phrase. The confirm is
    either the user's own fixed safe phrase (if configured; stored only as a
    salted SHA-256 in <home>/safe_phrase.json, 0600, and never shown to Claude)
    or, when none is configured, the per-action "confirm <word1> <word2>" Claude
    picks. A 1-3 word phrase is cheap to brute-force from its hash, so the
    protection is the 0600 permissions plus the fact that a voice turn cannot
    run the gated tools it would take to read the file.

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
file also keeps a short spoken summary of up to 3 blocked calls for the confirm
read-back: tool kinds, program and file names, location classes, and never an
argument value.

Imports stay light on purpose: the hook starts a fresh interpreter per tool
call, so this module must not pull in undertone.paths or anything heavy.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
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
MAX_WHAT_CHARS = 160
MAX_BLOCKED_MORE = 1_000_000
NOTHING_BLOCKED = "undertone has not blocked anything yet."
SAFE_PHRASE_FILE = "safe_phrase.json"
SAFE_PHRASE_MARKER = "<safe phrase>"  # challenge phrase in safe-phrase mode; normalize() can never produce it
REDACTED_PHRASE = "[safe phrase]"
MAX_SAFE_PHRASE_BYTES = 1024
SAFE_PHRASE_RESERVED = frozenset({"hey", "jarvis", "confirm"})
_HEX32_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)
_HEX64_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
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
        "blocked_more": 0,
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
    more = raw.get("blocked_more", 0)
    if not isinstance(more, int) or isinstance(more, bool) or not 0 <= more <= MAX_BLOCKED_MORE:
        raise GuardError("state field blocked_more has the wrong type")
    state["blocked_more"] = more
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


# -- safe phrase ----------------------------------------------------------


def validate_safe_phrase(phrase: str) -> str:
    """Return the normalized safe phrase, or raise ValueError with a user-facing message."""
    norm = normalize(phrase)
    words = norm.split()
    if not 1 <= len(words) <= 3:
        raise ValueError("The safe phrase must be one to three words.")
    if not all(w.isalpha() for w in words):
        raise ValueError("The safe phrase must be letters only, no digits.")
    if not 4 <= len(norm) <= 40:
        raise ValueError("The safe phrase must be 4 to 40 characters long.")
    if any(w in SAFE_PHRASE_RESERVED for w in words):
        raise ValueError("The safe phrase cannot use the words hey, jarvis or confirm.")
    if all(w in STOPWORDS for w in words):
        raise ValueError("The safe phrase is too common; use at least one uncommon word.")
    return norm


def _phrase_hash(salt: str, norm: str) -> str:
    return hashlib.sha256(bytes.fromhex(salt) + norm.encode()).hexdigest()


def _safe_phrase_path(home: Any) -> Path:
    return Path(home) / SAFE_PHRASE_FILE


def set_safe_phrase(home: Any, phrase: str) -> None:
    """Store the salted hash of the safe phrase atomically, 0600, in a 0700 home."""
    norm = validate_safe_phrase(phrase)
    salt = secrets.token_hex(16)
    rec = {"v": 1, "salt": salt, "words": len(norm.split()), "sha256": _phrase_hash(salt, norm)}
    home_path = Path(home)
    path = _safe_phrase_path(home)
    try:
        if home_path.is_symlink():
            raise GuardError("home dir is a symlink")
        home_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(home_path, 0o700)
        if path.is_symlink():
            raise GuardError("safe phrase file is a symlink")
        tmp_fd, tmp_name = tempfile.mkstemp(dir=home_path, prefix=".safe_phrase.", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, separators=(",", ":"))
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise GuardError(f"cannot write safe phrase: {exc.strerror}") from exc


def clear_safe_phrase(home: Any) -> bool:
    """Remove the safe phrase; True when there was one."""
    path = _safe_phrase_path(home)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise GuardError(f"cannot remove safe phrase: {exc.strerror}") from exc
    return True


def safe_phrase_configured(home: Any) -> bool:
    path = _safe_phrase_path(home)
    return path.is_file() and not path.is_symlink()


def _load_safe_phrase(home: Any) -> dict[str, Any] | None:
    """The validated safe-phrase record, or None for no/malformed config (fail closed)."""
    try:
        fd = os.open(_safe_phrase_path(home), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if (st.st_mode & 0o170000) != 0o100000 or st.st_size > MAX_SAFE_PHRASE_BYTES:
                return None
            raw = json.loads(fh.read(MAX_SAFE_PHRASE_BYTES + 1))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(raw, dict) or raw.get("v") != 1:
        return None
    salt, words, digest = raw.get("salt"), raw.get("words"), raw.get("sha256")
    if not (isinstance(salt, str) and _HEX32_RE.fullmatch(salt)
            and isinstance(digest, str) and _HEX64_RE.fullmatch(digest)
            and isinstance(words, int) and not isinstance(words, bool) and 1 <= words <= 3):
        return None
    return {"salt": salt, "words": words, "sha256": digest}


def _phrase_in(norm_words: list[str], rec: dict[str, Any]) -> bool:
    n = rec["words"]
    for i in range(len(norm_words) - n + 1):
        cand = _phrase_hash(rec["salt"], " ".join(norm_words[i:i + n]))
        if hmac.compare_digest(cand, rec["sha256"]):
            return True
    return False


def redact_safe_phrase(home: Any, text: str) -> str:
    """Replace each safe-phrase occurrence in text with [safe phrase]. Never raises; not a gate."""
    try:
        rec = _load_safe_phrase(home)
        if rec is None:
            return text
        n = rec["words"]
        toks = list(re.finditer(r"[A-Za-z0-9]+", text))
        out: list[str] = []
        pos = 0
        i = 0
        while i < len(toks):
            if i + n <= len(toks) and _phrase_in([t.group().lower() for t in toks[i:i + n]], rec):
                out.append(text[pos:toks[i].start()])
                out.append(REDACTED_PHRASE)
                pos = toks[i + n - 1].end()
                i += n
            else:
                i += 1
        out.append(text[pos:])
        return "".join(out)
    except Exception:  # noqa: BLE001 - redaction must never break the caller
        return text


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
        if challenge and challenge["phrase"] == SAFE_PHRASE_MARKER:
            rec = _load_safe_phrase(home) if can_grant and stamp - challenge["at"] <= CHALLENGE_TTL_S else None
            words = norm.split()
            granted = bool(rec and len(words) <= rec["words"] + 2 and _phrase_in(words, rec))
        else:
            granted = bool(
                can_grant
                and challenge
                and stamp - challenge["at"] <= CHALLENGE_TTL_S
                and f" confirm {challenge['phrase']} " in f" {norm} "
            )
        state["grant"] = {"at": stamp} if granted else None
        state["challenge"] = None  # one attempt per challenge
        if granted:  # a failed confirm keeps the blocked list so Claude can re-ask meaningfully
            state["blocked"] = []
            state["blocked_more"] = 0
        return granted

    return _update(home, token, fn)


def record_confirm_request(
    home: Any, token: str, reply_text: str, *, safe_phrase: bool = False, now: float | None = None
) -> str:
    """Arm a challenge from Claude's confirm reply and return its phrase.

    With `safe_phrase` the user's own configured phrase is the challenge: nothing
    is taken from the reply text and SAFE_PHRASE_MARKER is returned.
    """
    stamp = _now(now)
    if safe_phrase:
        def arm(state: dict[str, Any]) -> str:
            state["challenge"] = {"phrase": SAFE_PHRASE_MARKER, "at": stamp}
            return SAFE_PHRASE_MARKER

        return _update(home, token, arm)
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
        state["blocked_more"] = 0
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


_LONG_NAME = "a long name"
_UNREADABLE_BASH = "a shell command undertone could not read"
_COMPLEX_MARKERS = ("`", "$(", "<(", ">(", "<<")
_SCRIPT_COMPLEX_CHARS = ";&|\n`<>()${}"
_SUBCOMMAND_PROGRAMS = frozenset({
    "git", "npm", "pnpm", "yarn", "uv", "cargo", "go", "make", "just", "brew", "pip",
    "pip3", "docker", "kubectl", "gh",
})
_WRAPPER_VALUE_FLAGS = {
    "sudo": frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-R", "-r", "-t", "-T", "-U"}),
    "env": frozenset({"-u", "-C", "-S"}),
    "xargs": frozenset({"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a"}),
    "nohup": frozenset(),
    "time": frozenset({"-f", "-o"}),
    "nice": frozenset({"-n"}),
    "exec": frozenset({"-a"}),
    "command": frozenset(),
    "builtin": frozenset(),
    "timeout": frozenset({"-s", "-k"}),
    "watch": frozenset({"-n", "-d"}),
    "caffeinate": frozenset({"-t", "-w"}),
}
_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})
_SHELL_C_FLAG_RE = re.compile(r"-[A-Za-z]*c[A-Za-z]*", re.ASCII)
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_REDIRECT_OUT = frozenset({">", ">>", "&>", "&>>", ">|", "<>"})
_APPEND_REDIRECTS = frozenset({">>", "&>>"})
_FD_DUP_RE = re.compile(r"[0-9]+-?|-", re.ASCII)
_MAX_FLAG_WORDS = 3
_MAX_FLAG_CHARS = 10
_MAX_REDIRECT_PHRASES = 2
_MAX_WRAPPER_DEPTH = 3
_Segment = tuple[list[str], list[tuple[bool, str]]]  # (program and args, (append?, target) redirects)


def _name(raw: str) -> str:
    """A short name safe to speak: at most 2 words and 20 chars, else a fixed phrase."""
    text = _speakable(raw, 200)
    if text and len(text.split()) <= 2 and len(text) <= 20:
        return text
    return _LONG_NAME


def _under(path: str, base: str) -> bool:
    return (path.rstrip("/") + "/").startswith(base.rstrip("/") + "/")


def _loc(path: str, cwd: str | None) -> str:
    """Class a path as a location; string work only, no filesystem access."""
    if "$" in path or "`" in path:
        return "somewhere unknown"
    home = os.path.normpath(os.path.expanduser("~"))
    full = os.path.expanduser(path) if path.startswith("~") else path
    if full.startswith("~"):
        return "somewhere unknown"
    if not os.path.isabs(full):
        if not cwd:
            return "at a relative path"
        full = os.path.join(cwd, full)
    full = re.sub(r"^/+", "/", os.path.normpath(full))
    base = re.sub(r"^/+", "/", os.path.normpath(cwd)) if cwd else None
    # A project that is the home folder (or contains it) would hide the home class.
    if base is not None and _under(full, base) and not _under(home, base):
        first = os.path.relpath(full, base).split("/")[0]
        if first in (".git", ".claude", ".mcp.json"):
            return "in the project git or Claude settings"
        return "in this project"
    if _under(full, home):
        first = os.path.relpath(full, home).split("/")[0]
        if first.startswith(".") and first != ".":
            return "in your home settings files"
        return "in your home folder"
    return "outside your home folder"


def _file_phrase(path: Any, cwd: str | None) -> str:
    """`named <name> <class>` for a path, or "" when there is none."""
    if not isinstance(path, str) or not path.strip():
        return ""
    return f"named {_name(os.path.basename(path.rstrip('/')))} {_loc(path, cwd)}"


def _is_path_like(arg: str) -> bool:
    return arg == "*" or "/" in arg or arg.startswith(("~", "."))


_REMOTE_HOST_RE = re.compile(r"(?:[A-Za-z0-9._-]+@)?([A-Za-z0-9.-]+):", re.ASCII)
_BARE_USER_HOST_RE = re.compile(r"[A-Za-z0-9._-]+@([A-Za-z0-9.-]+)", re.ASCII)


def _url_class(value: str) -> str:
    """`to <host class>` for a URL, or `to a web address` when it has no readable host."""
    try:
        host = urllib.parse.urlsplit(value).hostname
    except ValueError:
        host = None
    return f"to {_host_class(host)}" if host else "to a web address"


def _remote_class(arg: str) -> str:
    """`to a remote computer <host class>` for `[user@]host:...` or `user@host`, else ""."""
    match = _REMOTE_HOST_RE.match(arg) or _BARE_USER_HOST_RE.fullmatch(arg)
    if match and ("." in match.group(1) or _is_ip(match.group(1))):
        return f"to a remote computer {_host_class(match.group(1))}"
    return ""


def _arg_clauses(args: list[str], cwd: str | None, detail: bool) -> str:
    """Flags (a few short ones), the location classes touched, and a flag for the rest.

    Never speaks a plain argument or a flag's value.
    """
    if not args:
        return ""
    if not detail:
        return " with arguments"
    flags: list[str] = []
    classes: list[str] = []
    other = False
    for arg in args:
        cls = ""
        if arg.startswith("-"):
            word = _speakable(arg)
            if len(flags) < _MAX_FLAG_WORDS and word and " " not in word and len(word) <= _MAX_FLAG_CHARS:
                flags.append(word)
            else:
                other = True
            value = arg.partition("=")[2]
            if "=" in arg and "://" in value:
                cls = _url_class(value)
        elif "://" in arg:
            cls = _url_class(arg)
        elif _remote_class(arg):
            cls = _remote_class(arg)
        elif _is_path_like(arg):
            cls = "on everything" if arg == "*" else f"on files {_loc(arg, cwd)}"
        else:
            other = True
        if cls and cls not in classes:
            classes.append(cls)
    text = "".join(f" {f}" for f in flags) + "".join(f" {c}" for c in classes)
    return text + (" with other arguments" if other else "")


def _wrapped_command(prog: str, args: list[str]) -> list[str]:
    """The words after a wrapper's own flags (and env assignments, timeout duration)."""
    value_flags = _WRAPPER_VALUE_FLAGS[prog]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-") and len(arg) > 1:
            i += 2 if arg in value_flags else 1
        elif prog == "env" and _ENV_ASSIGN_RE.match(arg):
            i += 1
        else:
            break
    rest = args[i:]
    if prog == "timeout" and rest:
        rest = rest[1:]  # the duration
    return rest


def _script_label(script: str) -> str:
    """The program a `sh -c` script runs, or `a script` when it is more than one plain command."""
    if any(ch in script for ch in _SCRIPT_COMPLEX_CHARS):
        return "a script"
    try:
        words = shlex.split(script)
    except ValueError:
        return "a script"
    while words and _ENV_ASSIGN_RE.match(words[0]):
        words = words[1:]
    if not words:
        return "a script"
    label, rest = _resolve(words, False, 1)
    return f"{label} with other arguments" if rest else label


def _resolve(words: list[str], with_sub: bool, depth: int = 0) -> tuple[str, list[str]]:
    """Name what a command runs (program, subcommand, wrapped program); also return the
    arguments left over to describe."""
    prog = os.path.basename(words[0])
    args = words[1:]
    if "$" in words[0]:
        return "a program chosen at run time", []
    name = _name(prog)
    if prog in _WRAPPER_VALUE_FLAGS:
        if depth >= _MAX_WRAPPER_DEPTH:
            return f"{name} wrapping other commands", []
        inner = _wrapped_command(prog, args)
        if not inner:
            return name, []
        label, rest = _resolve(inner, with_sub, depth + 1)
        return f"{name} {label}", rest
    if prog in _SHELLS:
        for j, arg in enumerate(args):
            if not arg.startswith("-"):
                break
            if _SHELL_C_FLAG_RE.fullmatch(arg):
                script = args[j + 1] if j + 1 < len(args) else ""
                return f"{name} running {_script_label(script)}", []
        return name, args
    if prog == "find":
        for j, arg in enumerate(args):
            if arg == "-delete":
                return f"{name} delete", args[:j] + args[j + 1:]
            if arg in _FIND_EXEC:
                tail = args[j + 1:]
                label = f"{name} exec" + (f" {_resolve(tail, False, depth + 1)[0]}" if tail else "")
                return label, args[:j] + (["tail"] if len(tail) > 1 else [])
    if with_sub and prog in _SUBCOMMAND_PROGRAMS:
        for j, arg in enumerate(args):
            if not arg.startswith("-") and not _is_path_like(arg):
                return f"{name} {_name(arg)}", args[:j] + args[j + 1:]
    return name, args


def _command_phrase(words: list[str], cwd: str | None, detail: bool, with_sub: bool) -> str:
    label, args = _resolve(words, with_sub)
    return label + _arg_clauses(args, cwd, detail)


def _redirect_words(redirects: list[tuple[bool, str]], cwd: str | None, with_target: bool) -> str:
    if not redirects:
        return ""
    if not with_target:
        return " with a redirect"
    text = ""
    for append, target in redirects[:_MAX_REDIRECT_PHRASES]:
        verb = "appending to" if append else "overwriting"
        text += f" {verb} a file {_file_phrase(target, cwd) or 'named a long name somewhere unknown'}"
    if len(redirects) > _MAX_REDIRECT_PHRASES:
        text += " and other redirects"
    return text


def _bash_segments(command: str) -> list[_Segment]:
    """Split into (program + args, output redirects) segments. Raises ValueError."""
    lex = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""  # bash treats `a#` as a word, not a comment
    tokens = list(lex)
    segments: list[_Segment] = [([], [])]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        nxt_is_word = nxt is not None and not set(nxt) <= _OPERATOR_CHARS
        if set(tok) <= _SEPARATOR_CHARS:
            segments.append(([], []))
        elif tok.isdigit() and nxt is not None and set(nxt) <= _OPERATOR_CHARS and set(nxt) & set("<>"):
            pass  # an fd number in front of a redirect, such as the 2 in 2>&1
        elif tok in _REDIRECT_OUT or (tok == ">&" and nxt_is_word and not _FD_DUP_RE.fullmatch(nxt)):
            if nxt_is_word:
                if nxt != "/dev/null":
                    segments[-1][1].append((tok in _APPEND_REDIRECTS, nxt))
                i += 1
        elif set(tok) <= _OPERATOR_CHARS:
            if tok in ("<", "<&", ">&") and nxt_is_word:
                i += 1  # an input file or fd dup: not shown
        else:
            segments[-1][0].append(tok)
        i += 1
    out = []
    for words, redirects in segments:
        while words and _ENV_ASSIGN_RE.match(words[0]):
            words = words[1:]
        if not words and redirects:
            words = ["redirect"]
        if words:
            out.append((words, redirects))
    return out


def _bash_words(command: str, cwd: str | None = None) -> str:
    """Fixed vocabulary plus capped names; never an argument value. Vague is fine, but
    never omit a program that runs, a redirect, or the fact that more was left out."""
    try:
        segments = _bash_segments(command)
    except ValueError:
        return _UNREADABLE_BASH
    if not segments:
        return _UNREADABLE_BASH
    if any(marker in command for marker in _COMPLEX_MARKERS):
        return f"a complex shell command starting with {_name(os.path.basename(segments[0][0][0]))}"
    total = len(segments)
    shown = segments[:3]
    n_more = total - 3
    more = f", and {n_more} more command{'s' if n_more > 1 else ''}" if n_more > 0 else ""
    prefix = "the shell command " if total == 1 else "the shell commands "
    for detail, with_sub, with_target in (
        (True, True, True), (False, True, True), (False, True, False), (False, False, False),
    ):
        parts = [
            _command_phrase(words, cwd, detail, with_sub) + _redirect_words(redirects, cwd, with_target)
            for words, redirects in shown
        ]
        text = prefix + ", then ".join(parts) + more
        if len(text) <= MAX_WHAT_CHARS:
            return text
    first = _resolve(segments[0][0], False)[0]
    n_rest = total - 1
    text = f"the shell commands {first} and {n_rest} more command{'s' if n_rest > 1 else ''}"
    return text + (" with a redirect" if any(r for _, r in segments) else "")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _host_class(host: str) -> str:
    labels = host.split(".")
    if not host.isascii() or any(label.startswith("xn--") for label in labels):
        return "an international address"
    if _is_ip(host):
        return "an IP address"
    last2 = ".".join(_name(label) for label in labels[-2:])
    return f"{'a site under ' if len(labels) > 2 else ''}{last2}"


def _fetch_words(url: Any) -> str:
    try:
        host = urllib.parse.urlsplit(url).hostname if isinstance(url, str) else None
    except ValueError:
        host = None
    if not host:
        return "fetch a web page"
    return f"fetch a web page from {_host_class(host)}"


def _describe(tool_name: str, tool_input: Any, cwd: str | None) -> str:
    args = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash":
        command = args.get("command")
        return _bash_words(command, cwd) if isinstance(command, str) else _UNREADABLE_BASH
    for names, verb, noun, key in (
        (("Write",), "write", "file", "file_path"),
        (("Edit", "MultiEdit"), "edit", "file", "file_path"),
        (("NotebookEdit",), "edit", "notebook", "notebook_path"),
    ):
        if tool_name in names:
            where = _file_phrase(args.get(key), cwd)
            return f"{verb} a {noun} {where}" if where else f"{verb} a {noun}"
    if tool_name == "WebFetch":
        return _fetch_words(args.get("url"))
    if tool_name.startswith("mcp__") and tool_name.count("__") >= 2:
        server, tool = tool_name[5:].split("__", 1)
        text = f"the {_name(server)} tool {_name(tool)}"
        if args:
            text += f" with {len(args)} argument{'s' if len(args) > 1 else ''}"
        return text
    return f"the {_name(tool_name)} tool"


def describe_call(tool_name: Any, tool_input: Any, cwd: str | None = None) -> str:
    """A short plain-words phrase for what a call would do; never raises.

    Speaks only fixed vocabulary plus short capped names (programs, files, hosts,
    tools) and location classes; never an argument value, free text or a message.
    Output holds only [A-Za-z0-9. ,] and at most MAX_WHAT_CHARS characters.
    """
    try:
        text = _describe(tool_name, tool_input, cwd)
    except Exception:
        try:
            text = f"the {_name(tool_name)} tool"
        except Exception:
            text = "a tool call"
    text = _cut(re.sub(r"[^A-Za-z0-9. ,]", "", text), MAX_WHAT_CHARS)
    return text if text.strip() else "a tool call"


def record_blocked(home: Any, token: str, what: str, *, now: float | None = None) -> None:
    """Remember a denied call's spoken summary (latest MAX_BLOCKED, deduped); count evictions."""
    stamp = _now(now)

    def fn(state: dict[str, Any]) -> None:
        kept = [e for e in state["blocked"] if e["what"] != what]
        evicted = max(0, len(kept) + 1 - MAX_BLOCKED)
        state["blocked"] = (kept + [{"at": stamp, "what": what}])[-MAX_BLOCKED:]
        state["blocked_more"] = min(state["blocked_more"] + evicted, MAX_BLOCKED_MORE)

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
    text = "undertone blocked: " + "; and ".join(whats)
    more = state["blocked_more"]
    if more:
        text += f", and {more} more blocked call{'s' if more > 1 else ''}"
    return text + "."


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
        cwd = payload.get("cwd")
        record_blocked(home, token, describe_call(
            tool_name, tool_input, cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None))
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

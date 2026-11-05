"""The one wake-word record every surface reads.

Default is the builtin "hey jarvis". `ERIDIAN_WAKE_MODEL` points at a
microWakeWord manifest json (loaded with `MicroWakeWord.from_config`) and
replaces it everywhere: detector, acoustic self-screen, text scrub, transcript
stripping, hints and the channel's instructions. A set-but-invalid manifest is
an error at startup, never a silent fall back to the default.

The channel process runs inside Claude's process tree and needs only the phrase,
not the tflite model. `eridian session` resolves the manifest once and hands
the phrase to the channel as `ERIDIAN_WAKE_PHRASE` in the MCP config's env,
so the channel never reads the manifest (or imports pymicro-wakeword) itself.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import paths

MODEL_ENV = "ERIDIAN_WAKE_MODEL"
PHRASE_ENV = "ERIDIAN_WAKE_PHRASE"

DEFAULT_PHRASE = "hey jarvis"

_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_MODEL_BYTES = 16 * 1024 * 1024
_PHRASE_RE = re.compile(r"[a-z][a-z' ]{0,38}[a-z]")
_LEAD_INS = ("hey", "hi", "okay", "ok")


class WakeWordError(Exception):
    """The configured wake word can't be used; the message is user-facing."""


@dataclass(frozen=True)
class WakeWord:
    phrase: str  # lowercase, e.g. "hey rocky"
    model_path: Path | None = None  # private copy of the manifest json; None = builtin HEY_JARVIS

    @property
    def words(self) -> tuple[str, ...]:
        return tuple(self.phrase.split())

    @property
    def name(self) -> str:
        """The assistant's name: the last word of the phrase."""
        return self.words[-1]

    @property
    def is_default(self) -> bool:
        return self.model_path is None and self.phrase == DEFAULT_PHRASE

    def load_model(self):
        """Load a fresh MicroWakeWord for this wake word (lazy: the import is heavy)."""
        from pymicro_wakeword import MicroWakeWord, Model

        if self.model_path is None:
            return MicroWakeWord.from_builtin(Model.HEY_JARVIS)
        try:
            return MicroWakeWord.from_config(self.model_path)
        except Exception as exc:  # noqa: BLE001 - any load failure must fail closed
            raise WakeWordError(
                f"{MODEL_ENV}={self.model_path}: cannot load the wake model: {exc}"
            ) from exc


DEFAULT = WakeWord(DEFAULT_PHRASE)


def _check_owned_private(st: os.stat_result, label: str) -> None:
    """The file/dir must belong to this user and not be writable by group or other."""
    if st.st_uid != os.geteuid():
        raise WakeWordError(f"{label}: not owned by you; refusing to load a wake model someone else controls")
    if st.st_mode & 0o022:
        raise WakeWordError(f"{label}: writable by group or other; run chmod go-w on it")


def _read_regular(path: Path, limit: int, what: str) -> bytes:
    """Read a regular, user-owned, not group/other-writable file; never follow a final symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, ValueError) as exc:
        raise WakeWordError(f"{what} {path}: cannot open: {getattr(exc, 'strerror', None) or exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise WakeWordError(f"{what} {path}: not a regular file")
        _check_owned_private(st, f"{what} {path}")
        if st.st_size > limit:
            raise WakeWordError(f"{what} {path}: too large")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read(limit + 1)
    finally:
        os.close(fd)


_HASH_DIR_RE = re.compile(r"[0-9a-f]{64}")


def _stage(root: Path, target: Path, files: dict[str, bytes]) -> None:
    staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=root))
    try:
        for name, data in files.items():
            fd = os.open(staging / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        os.chmod(staging, 0o700)
        try:
            os.rename(staging, target)
        except OSError:
            pass  # a concurrent process published the same content first; verification decides
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _copy_problem(target: Path, files: dict[str, bytes]) -> str | None:
    """Why the copy at `target` can't be loaded, or None if it is exactly `files`."""
    st = os.lstat(target)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        return "not a private directory"
    for name, data in files.items():
        try:
            if _read_regular(target / name, len(data), "wake model copy") != data:
                return f"{name} does not match its source"
        except WakeWordError as exc:
            return str(exc)
    if {e.name for e in os.scandir(target)} != set(files):
        return "unexpected files"
    return None


def _remove_copy(root: Path, target: Path) -> None:
    """Delete one stale hash dir. Narrow on purpose: only root/<64 hex>, a real dir we own."""
    if target.parent != root or not _HASH_DIR_RE.fullmatch(target.name):
        raise WakeWordError(f"{target}: refusing to remove a path that is not a wake model copy")
    st = os.lstat(target)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise WakeWordError(f"{target}: wake model copy is a link or not yours; remove it by hand")
    shutil.rmtree(target)


def _private_copy(manifest: bytes, model_name: str, model: bytes) -> Path:
    """Copy the validated bytes into a 0700 content-addressed dir under ERIDIAN_HOME.

    Every later load (detector, per-chunk self-screen detectors) reads this copy,
    so the source can't be swapped or re-pointed after validation. A stale or
    damaged dir for the same content is removed and re-staged; a link is refused.
    """
    digest = hashlib.sha256(manifest + b"\0" + model_name.encode() + b"\0" + model).hexdigest()
    root = paths.home_dir() / "wake-model"
    if os.path.lexists(root):
        rst = os.lstat(root)  # lstat: a symlinked root must not be followed (mkdir/chmod would)
        if not stat.S_ISDIR(rst.st_mode) or rst.st_uid != os.geteuid():
            raise WakeWordError(f"{root}: must be a real directory you own (not a link); remove it by hand")
    else:
        root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    target = root / digest
    files = {"manifest.json": manifest, model_name: model}
    if not os.path.lexists(target):
        _stage(root, target, files)
    problem = _copy_problem(target, files)
    if problem is not None:
        if stat.S_ISLNK(os.lstat(target).st_mode):
            raise WakeWordError(f"{target}: wake model copy is a symlink; remove it by hand")
        _remove_copy(root, target)
        _stage(root, target, files)
        problem = _copy_problem(target, files)
        if problem is not None:
            raise WakeWordError(f"{target}: wake model copy is unusable: {problem}")
    return target / "manifest.json"


def normalize_phrase(raw: object, source: str) -> str:
    phrase = " ".join(raw.lower().split()) if isinstance(raw, str) else ""
    if not _PHRASE_RE.fullmatch(phrase):
        raise WakeWordError(
            f"{source}: the wake phrase must be 2-40 letters (spaces allowed), got {raw!r}"
        )
    if phrase != DEFAULT_PHRASE:
        from .voice_guard import SAFE_PHRASE_RESERVED  # lazy: voice_guard imports this module

        # "hey" is part of nearly every phrase; any other reserved word (confirm,
        # jarvis) would collide with the voice-confirm vocabulary.
        clash = (set(phrase.split()) & SAFE_PHRASE_RESERVED) - {"hey"}
        if clash:
            raise WakeWordError(f"{source}: the wake phrase cannot contain {sorted(clash)[0]!r}")
    return phrase


def from_manifest(path: str | os.PathLike[str]) -> WakeWord:
    """Validate a manifest + model, copy them privately, return a WakeWord on the copy.

    Raises WakeWordError for anything unusable. Does not load the tflite model.
    """
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        raise WakeWordError(f"{MODEL_ENV}={path}: must be an absolute path (a leading ~ is fine)")
    try:
        parent = p.parent.resolve(strict=True)
    except OSError as exc:
        raise WakeWordError(f"{MODEL_ENV}={path}: cannot open the manifest: {exc.strerror or exc}") from exc
    pst = os.stat(parent)
    if not stat.S_ISDIR(pst.st_mode):
        raise WakeWordError(f"{parent}: not a directory")
    _check_owned_private(pst, str(parent))
    p = parent / p.name
    raw = _read_regular(p, _MAX_MANIFEST_BYTES, "wake manifest")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WakeWordError(f"{p}: manifest is not valid json: {exc}") from exc
    if not isinstance(data, dict):
        raise WakeWordError(f"{p}: manifest must be a json object")
    phrase = normalize_phrase(data.get("wake_word"), f"{p}: wake_word")
    model = data.get("model")
    # from_config joins this onto the manifest's directory: keep it a plain sibling file.
    if (
        not isinstance(model, str)
        or not model
        or model != Path(model).name
        or model in (".", "..")
        or model.lower() == "manifest.json"  # case-insensitive filesystems: same file
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in model)
    ):
        raise WakeWordError(f"{p}: `model` must be a file name next to the manifest, got {model!r}")
    micro = data.get("micro")
    if (
        not isinstance(micro, dict)
        or not isinstance(micro.get("probability_cutoff"), (int, float))
        or isinstance(micro.get("probability_cutoff"), bool)
        or not isinstance(micro.get("sliding_window_size"), int)
        or isinstance(micro.get("sliding_window_size"), bool)
    ):
        raise WakeWordError(f"{p}: `micro` needs numeric probability_cutoff and integer sliding_window_size")
    model_bytes = _read_regular(parent / model, _MAX_MODEL_BYTES, "wake model")
    try:
        copy = _private_copy(raw, model, model_bytes)
    except (OSError, ValueError) as exc:
        raise WakeWordError(f"{p}: cannot make the private wake model copy: {exc}") from exc
    return WakeWord(phrase, copy)


def resolve(environ: Mapping[str, str] | None = None) -> WakeWord:
    """The configured wake word. Raises WakeWordError if configured but unusable."""
    env = os.environ if environ is None else environ
    raw_model = env.get(MODEL_ENV)
    if raw_model:  # set-but-empty counts as unset, like the other ERIDIAN_* knobs
        return from_manifest(raw_model)
    raw_phrase = env.get(PHRASE_ENV)
    if raw_phrase:  # handed down by `eridian session`; no model needed (channel, hooks)
        return WakeWord(normalize_phrase(raw_phrase, PHRASE_ENV))
    return DEFAULT


def resolve_phrase_only(environ: Mapping[str, str] | None = None) -> WakeWord:
    """The wake word from ERIDIAN_WAKE_PHRASE alone; never reads a manifest.

    For the channel, which runs inside Claude's process tree: an exported
    ERIDIAN_WAKE_MODEL in the user's shell must not make it open a model file.
    """
    env = os.environ if environ is None else environ
    raw_phrase = env.get(PHRASE_ENV)
    return WakeWord(normalize_phrase(raw_phrase, PHRASE_ENV)) if raw_phrase else DEFAULT


@functools.lru_cache(maxsize=8)
def _resolve_cached(model: str, phrase: str) -> WakeWord:
    return resolve({MODEL_ENV: model, PHRASE_ENV: phrase})


def current_phrase_only() -> WakeWord:
    return resolve_phrase_only()


def current() -> WakeWord:
    """resolve() on the live environment, cached per env value (the manifest is read once)."""
    return _resolve_cached(os.environ.get(MODEL_ENV, ""), os.environ.get(PHRASE_ENV, ""))


# ---- derived patterns ------------------------------------------------------

_SEP = r"[\W_]*"
# Look-arounds rather than \b so "rockyfoo" is left alone while separators
# between the letters are free. A false positive only costs a substitution.
_EDGE_L = r"(?<![^\W_])"
_EDGE_R = r"(?![^\W_])"
_LETTER_CLASS = {"i": "[iy]", "y": "[iy]", "e": "[ea]", "a": "[ae]"}


def _spelled(word: str) -> str:
    return _SEP.join(_LETTER_CLASS.get(c, re.escape(c)) for c in word)


@functools.lru_cache(maxsize=8)
def scrub_pattern(wake: WakeWord) -> re.Pattern[str]:
    """Matches spoken forms of the wake PHRASE anywhere in text.

    The default keeps its historical shape (the bare name matches too, with an
    optional lead-in). A custom phrase matches only the whole phrase, so the
    assistant's bare name stays speakable; the acoustic self-screen decides it.
    """
    if wake.is_default:
        return re.compile(
            _EDGE_L
            + r"(?:(?:hey|hi|okay|ok)[\W_]+)?"
            + _SEP.join(["j", "[ae]", "r", "v", "[iy]", "s"])
            + _EDGE_R,
            re.IGNORECASE,
        )
    words = list(wake.words)
    parts = []
    for i, w in enumerate(words):
        if i == 0 and w == "hey":
            parts.append(r"(?:hay|hi|" + _spelled(w) + ")")
        else:
            parts.append(_spelled(w))
    return re.compile(_EDGE_L + r"[\W_]+".join(parts) + _EDGE_R, re.IGNORECASE)


@functools.lru_cache(maxsize=8)
def strip_pattern(wake: WakeWord) -> re.Pattern[str]:
    """Matches a leading wake phrase (and trailing punctuation) in a transcript.

    Everything before the name is optional: a lead-in ("hey"), then any other
    words of the phrase ("hey big rocky" -> "big"), so a transcript that kept
    only part of the phrase is still stripped.
    """
    leads = list(dict.fromkeys(re.escape(w) for w in (*_LEAD_INS, *wake.words[:-1][:1])))
    middle = "".join(
        rf"(?:{re.escape(w)}[\s,]+)?" for w in wake.words[:-1] if w not in _LEAD_INS
    )
    # STT sometimes hears "hey" as "a"; that one needs a separator so "ajarvis"
    # and "Alpha Jarvis" are left alone.
    return re.compile(
        r"^\s*(" + "|".join(leads) + r"|a(?=[\s,]))?[\s,]*" + middle + re.escape(wake.name) + r"[\s,.!?:]*",
        re.IGNORECASE,
    )

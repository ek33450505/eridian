"""Path helpers shared by the persona hook and the memory store."""

from __future__ import annotations

import os
from pathlib import Path


def cwd_under(cwd: object, under: str) -> bool:
    """True when cwd is under (or equal to) under, after resolving symlinks.

    May raise OSError/ValueError/RuntimeError from path resolution; main() catches them all.
    """
    if not isinstance(cwd, str) or not cwd:
        return False
    cwd_path, under_path = Path(cwd), Path(under)
    if not cwd_path.is_absolute() or not under_path.is_absolute():
        return False
    return cwd_path.resolve().is_relative_to(under_path.resolve())


def inside_dir(cwd: object, target: str) -> bool:
    """True when cwd is target or inside it, compared by file identity (not by spelling).

    Unlike cwd_under this also matches a differently-cased path on a case-insensitive
    filesystem. A missing target never matches. May raise from path resolution.
    """
    if not isinstance(cwd, str) or not cwd:
        return False
    cwd_path = Path(cwd)
    if not cwd_path.is_absolute() or not Path(target).is_absolute():
        return False
    resolved = cwd_path.resolve()
    for candidate in (resolved, *resolved.parents):
        try:
            if os.path.samefile(candidate, target):
                return True
        except OSError:
            continue
    return False


def within(cwd: object, target: str) -> bool:
    """True when cwd is in target by spelling or by file identity (fails closed on either)."""
    return cwd_under(cwd, target) or inside_dir(cwd, target)

"""Conflict-aware scheduling primitives for bounded concurrency (V1.5).

Two edit tasks may run at the same time only when their ``allowed_paths``
scopes are disjoint. Path comparison is component-wise: ``src`` is an
ancestor of ``src/core`` but has nothing to do with ``srcx``. An empty
scope (read-only) never conflicts with anything because it cannot write.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath


def _is_prefix(candidate: PurePosixPath, base: PurePosixPath) -> bool:
    return candidate.parts[: len(base.parts)] == base.parts


def paths_conflict(
    left: tuple[str, ...], right: tuple[str, ...]
) -> bool:
    """True when any scope entry in ``left`` overlaps one in ``right``."""
    if not left or not right:
        return False
    right_paths = [PurePosixPath(p) for p in right]
    for l_raw in left:
        lp = PurePosixPath(l_raw)
        for rp in right_paths:
            if _is_prefix(lp, rp) or _is_prefix(rp, lp):
                return True
    return False


# --------------------------------------------------------------------------
# Task 26: per-repository integration serialization

import contextlib
import fcntl
import hashlib


class RepositoryBusy(RuntimeError):
    """Raised when another integrator already holds the repository lock."""


@contextlib.contextmanager
def repository_integration_lock(lock_root: Path, repository: Path):
    """Serialize ``integrate`` per repository via an advisory file lock.

    The lock file lives under ``lock_root`` keyed by the repository's
    realpath so aliases converge on one key. Acquisition is non-blocking:
    a concurrent integrator raises :class:`RepositoryBusy` immediately,
    because integration must never silently queue behind a review that
    may take arbitrarily long.
    """
    lock_root = Path(lock_root)
    lock_root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(
        str(Path(os.path.realpath(repository))).encode("utf-8")
    ).hexdigest()[:24]
    lock_path = lock_root / f"{key}.lock"

    handle = open(lock_path, "w")  # noqa: SIM115 - closed in finally below
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RepositoryBusy(
                f"another integration for {repository} is in progress"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()

"""Conflict-aware scheduling primitives for bounded concurrency (V1.5).

Two edit tasks may run at the same time only when their ``allowed_paths``
scopes are disjoint. Path comparison is component-wise: ``src`` is an
ancestor of ``src/core`` but has nothing to do with ``srcx``. An empty
scope (read-only) never conflicts with anything because it cannot write.
"""

from __future__ import annotations

from pathlib import PurePosixPath


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

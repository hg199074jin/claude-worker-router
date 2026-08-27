"""Pre-flight safety scans executed before any worker invocation.

V1.2 covers tracked symlinks. Git stores symlinks as regular index entries
with mode ``120000``; because they are just file contents to Git, an
unsuspecting worker can happily create or follow one and reach far beyond
the worktree boundary.
"""

from __future__ import annotations

import errno
import os
import subprocess
from pathlib import Path, PurePosixPath

from .models import RunMode


class ExternalSymlinkError(RuntimeError):
    """Raised when a tracked symlink fails the containment scan."""


def validate_symlinks(
    repository: Path,
    allowed_paths: tuple[str, ...],
    mode: RunMode,
) -> None:
    """Fail closed on unsafe tracked symlinks; raise :class:`ExternalSymlinkError`.

    All modes reject links that resolve outside the repository or cannot be
    resolved at all (broken link, cycle). Edit mode additionally requires a
    link *located inside* ``allowed_paths`` to resolve inside
    ``allowed_paths`` too.
    """
    root = Path(os.path.realpath(Path(repository)))

    for rel, link_path in _tracked_symlinks(root):
        try:
            target_text = os.readlink(link_path)
        except OSError as exc:
            raise ExternalSymlinkError(
                f"tracked symlink {rel} could not be read: {exc}"
            ) from exc

        logical = (
            Path(target_text)
            if os.path.isabs(target_text)
            else link_path.parent / target_text
        )
        try:
            resolved = logical.resolve(strict=True)
        except RuntimeError as exc:
            # Python 3.12+ pathlib reports symlink loops as RuntimeError.
            raise ExternalSymlinkError(
                f"symlink cycle detected at {rel}"
            ) from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ExternalSymlinkError(
                    f"symlink cycle detected at {rel}"
                ) from exc
            raise ExternalSymlinkError(
                f"broken or unresolvable symlink {rel}: {exc}"
            ) from exc

        if not resolved.is_relative_to(root):
            raise ExternalSymlinkError(
                f"tracked symlink {rel} resolves outside the repository: "
                f"{resolved}"
            )

        if mode == RunMode.EDIT and _within_any(rel, allowed_paths):
            resolved_rel = resolved.relative_to(root).as_posix()
            if not _within_any(resolved_rel, allowed_paths):
                raise ExternalSymlinkError(
                    f"tracked symlink {rel} sits inside allowed_paths but "
                    f"resolves to {resolved_rel}, which is outside them"
                )


def _tracked_symlinks(repo_root: Path) -> list[tuple[str, Path]]:
    """Return ``(relative path, absolute path)`` for index entries of mode 120000."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-s", "-z"],
            shell=False,
            check=True,
            text=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []

    links: list[tuple[str, Path]] = []
    for entry in proc.stdout.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        if len(fields) >= 3 and fields[0] == "120000" and path:
            links.append((path, repo_root / path))
    return links


def _within_any(rel_path: str, allowed_paths: tuple[str, ...]) -> bool:
    changed_parts = PurePosixPath(rel_path).parts
    return any(
        changed_parts[: len(PurePosixPath(allowed).parts)]
        == PurePosixPath(allowed).parts
        for allowed in allowed_paths
    )

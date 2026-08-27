"""Shared helpers for worker-router tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _PATH_EXTENSIONS() -> str:
    """A PATH that lets executor subprocesses locate ``uv``."""

    existing = os.environ.get("PATH", "")
    return os.pathsep.join(
        ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", existing]
    )


def _git(repository: Path, *args: str) -> None:
    """Run a git command with ``-C <repository>`` and raise on failure."""

    subprocess.run(
        ["git", "-C", str(repository), *args],
        shell=False,
        check=True,
        text=True,
        capture_output=True,
    )


def git_head(repository: Path) -> str:
    """Return the current HEAD commit SHA of a repository."""

    result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        shell=False,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def init_repository(repository: Path) -> Path:
    """Initialize a temporary repository on ``main`` with an ``example.txt`` commit.

    Returns the repository path after creating it so the caller can chain
    directory operations directly.
    """

    repository = Path(repository)
    repository.mkdir(parents=True, exist_ok=True)
    _git(repository, "init", "--initial-branch=main", "--quiet")
    _git(repository, "config", "user.email", "router-test@example.invalid")
    _git(repository, "config", "user.name", "Router Test")
    _git(repository, "config", "commit.gpgsign", "false")
    (repository / "example.txt").write_text("main\n", encoding="utf-8")
    _git(repository, "add", "example.txt")
    _git(repository, "commit", "--quiet", "-m", "seed example.txt")
    return repository

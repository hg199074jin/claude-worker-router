"""Git worktree isolation and change-budget helpers for worker runs."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class DirtyCheckoutError(RuntimeError):
    """Raised when the repository has uncommitted changes before worker isolation."""


class ScopeExceededError(RuntimeError):
    """Raised when worker edits exceed the configured change budget."""


class PathScopeExceededError(ScopeExceededError):
    """Raised when worker edits escape the request's allowed path prefixes."""


@dataclass(frozen=True)
class ChangeMeasure:
    files: tuple[str, ...]
    diff_lines: int


@dataclass
class GitWorkspace:
    repository: Path
    path: Path
    branch: str
    run_id: str

    @classmethod
    def create(cls, repository: Path, run_id: str) -> "GitWorkspace":
        """Create a clean worker branch and sibling worktree from HEAD."""
        resolved = _resolve_repo_root(repository)
        _ensure_clean(resolved)

        branch = f"codex-worker/{run_id}"
        worktree_root = resolved.parent / ".codex-worktrees" / resolved.name / run_id

        _run_git(resolved, "worktree", "add", "-b", branch, str(worktree_root), "HEAD")

        return cls(repository=resolved, path=worktree_root, branch=branch, run_id=run_id)

    def measure_changes(
        self,
        max_files: int,
        max_diff_lines: int,
        allowed_paths: tuple[str, ...],
    ) -> ChangeMeasure:
        """Count tracked and untracked files plus numstat lines, then enforce budgets."""
        files = _list_changed_files(self.path)
        diff_lines = _count_diff_lines(self.path)

        outside_scope = tuple(
            path for path in files if not _is_allowed_path(path, allowed_paths)
        )
        if outside_scope:
            raise PathScopeExceededError(
                "changed paths outside allowed scope: " + ", ".join(outside_scope)
            )

        if len(files) > max_files or diff_lines > max_diff_lines:
            raise ScopeExceededError(
                f"changes exceed budget: {len(files)} files (limit {max_files}); "
                f"{diff_lines} diff lines (limit {max_diff_lines})"
            )

        return ChangeMeasure(files=files, diff_lines=diff_lines)

    def commit_worker_change(self, message: str) -> str:
        """Create one local commit on the temporary branch and return its SHA."""
        _run_git(self.path, "add", "--all")
        _run_git(self.path, "commit", "--quiet", "-m", message)
        result = _run_git(self.path, "rev-parse", "HEAD")
        return result.stdout.strip()

    def remove_after_review(self) -> None:
        """Remove the worktree and branch only after an explicit reviewed cleanup call."""
        _run_git(self.repository, "worktree", "remove", "--force", str(self.path))
        _run_git(self.repository, "branch", "-D", self.branch)

    def render_patch(self) -> str:
        """Render a best-effort textual patch of tracked and untracked edits.

        Tracked changes come from ``git diff HEAD``. Untracked files get a
        synthetic ``new file`` diff section so evidence survives worktree
        cleanup. Binary untracked files are noted instead of inlined.
        """
        parts: list[str] = [_run_git(self.path, "diff", "HEAD").stdout]

        untracked = _run_git(self.path, "ls-files", "--others", "--exclude-standard")
        for rel in untracked.stdout.splitlines():
            if not rel:
                continue
            header = (
                f"diff --git a/{rel} b/{rel}\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                f"+++ b/{rel}\n"
            )
            try:
                text = (self.path / rel).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                parts.append(f"{header}Binary file {rel} added\n")
                continue
            body = "".join(f"+{line}\n" for line in text.splitlines())
            parts.append(header + body)
        return "".join(parts)


def _run_git(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke git with an explicit argv list and capture text output."""
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        shell=False,
        check=True,
        text=True,
        capture_output=True,
    )


def _resolve_repo_root(repository: Path) -> Path:
    """Resolve the canonical repository root via ``git rev-parse --show-toplevel``."""
    result = _run_git(Path(repository), "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve()


def _ensure_clean(repository: Path) -> None:
    """Raise ``DirtyCheckoutError`` when the working tree is not empty."""
    result = _run_git(repository, "status", "--porcelain")
    if result.stdout.strip():
        raise DirtyCheckoutError(f"repository has uncommitted changes: {repository}")


def _list_changed_files(workspace: Path) -> tuple[str, ...]:
    """Return sorted relative paths of modified tracked or untracked files."""
    tracked = _run_git(workspace, "diff", "--name-only", "HEAD")
    untracked = _run_git(workspace, "ls-files", "--others", "--exclude-standard")

    files: set[str] = set()
    for line in tracked.stdout.splitlines():
        if line:
            files.add(line)
    for line in untracked.stdout.splitlines():
        if line:
            files.add(line)
    return tuple(sorted(files))


def _count_diff_lines(workspace: Path) -> int:
    """Sum added+removed numstat lines across tracked and untracked changes."""
    total = 0

    numstat = _run_git(workspace, "diff", "--numstat", "HEAD")
    for line in numstat.stdout.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        total += _parse_numstat_count(parts[0]) + _parse_numstat_count(parts[1])

    untracked = _run_git(workspace, "ls-files", "--others", "--exclude-standard")
    for file_path in untracked.stdout.splitlines():
        if not file_path:
            continue
        # ``git diff --no-index`` exits with code 1 when files differ; this is the
        # normal path for untracked files. Treat any other exit as an error.
        no_index = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "diff",
                "--no-index",
                "--numstat",
                "/dev/null",
                file_path,
            ],
            shell=False,
            check=False,
            text=True,
            capture_output=True,
        )
        if no_index.returncode not in (0, 1):
            raise RuntimeError(
                f"git diff --no-index failed for {file_path}: "
                f"{no_index.stderr.strip() or 'unknown error'}"
            )
        for line in no_index.stdout.splitlines():
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            total += _parse_numstat_count(parts[0]) + _parse_numstat_count(parts[1])
    return total


def _parse_numstat_count(value: str) -> int:
    """Parse a numstat count cell; ``-`` (binary) or empty string return 0."""
    if value in ("", "-"):
        return 0
    return int(value)


def _is_allowed_path(changed_path: str, allowed_paths: tuple[str, ...]) -> bool:
    changed_parts = PurePosixPath(changed_path).parts
    return any(
        changed_parts[: len(PurePosixPath(allowed).parts)]
        == PurePosixPath(allowed).parts
        for allowed in allowed_paths
    )

"""Symlink safety tests (V1.2 Task 6).

Tracked symlinks are an escape hatch from worktree isolation. Before any
worker invocation the executor scans ``git ls-files`` for mode-120000
entries and refuses to run when a symlink resolves outside the repository,
is broken, or forms a cycle. Edit mode additionally requires that a
symlink located inside ``allowed_paths`` also *resolves* inside
``allowed_paths``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claude_worker_router.models import RunMode
from claude_worker_router.safety import ExternalSymlinkError, validate_symlinks
from tests.helpers import init_repository, run_bounded_fixture


def _commit_symlink(repository: Path, name: str, target: str) -> None:
    """Create a tracked symlink at ``name`` pointing at ``target``."""

    link = repository / name
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(target)
    from tests.helpers import _git

    _git(repository, "add", str(name))
    _git(
        repository,
        "-c",
        "user.email=router-test@example.invalid",
        "-c",
        "user.name=Router Test",
        "commit",
        "--quiet",
        "-m",
        f"track symlink {name}",
    )


class ValidateSymlinksUnitTests(unittest.TestCase):
    def test_internal_symlink_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = init_repository(Path(raw) / "repo")
            _commit_symlink(repository, "alias.txt", "example.txt")

            validate_symlinks(repository, ("example.txt",), RunMode.READ_ONLY)
            validate_symlinks(repository, ("example.txt",), RunMode.EDIT)

    def test_external_absolute_target_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = init_repository(Path(raw) / "repo")
            _commit_symlink(repository, "escape.txt", "/etc/hosts")

            with self.assertRaisesRegex(ExternalSymlinkError, "escape.txt"):
                validate_symlinks(repository, ("example.txt",), RunMode.READ_ONLY)

    def test_broken_symlink_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = init_repository(Path(raw) / "repo")
            _commit_symlink(repository, "dangling.txt", "no-such-target.txt")

            with self.assertRaisesRegex(ExternalSymlinkError, "dangling.txt"):
                validate_symlinks(repository, ("example.txt",), RunMode.EDIT)

    def test_cycle_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = init_repository(Path(raw) / "repo")
            _commit_symlink(repository, "a-link", "b-link")
            _commit_symlink(repository, "b-link", "a-link")

            with self.assertRaisesRegex(ExternalSymlinkError, "cycle"):
                validate_symlinks(repository, ("example.txt",), RunMode.EDIT)

    def test_edit_mode_allowed_path_link_must_resolve_inside_scope(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = init_repository(Path(raw) / "repo")
            # Inside the repo but resolving to a file outside allowed_paths.
            _commit_symlink(repository, "scope/neighbor.txt", "../example.txt")

            # The extra rule only binds links *located inside* allowed_paths:
            # this link lives under scope/, so its target must resolve there.
            with self.assertRaisesRegex(ExternalSymlinkError, "neighbor.txt"):
                validate_symlinks(repository, ("scope",), RunMode.EDIT)

            # A link outside the declared scope carries no extra constraint;
            # read-only mode never applies the scoping rule either.
            validate_symlinks(repository, ("elsewhere",), RunMode.READ_ONLY)

    def test_edit_mode_link_target_inside_same_scope_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = init_repository(Path(raw) / "repo")
            _commit_symlink(repository, "scope/direct.txt", "./payload.txt")
            (repository / "scope" / "payload.txt").write_text("x\n", encoding="utf-8")
            from tests.helpers import _git

            _git(repository, "add", "scope/payload.txt")
            _git(
                repository,
                "-c",
                "user.email=router-test@example.invalid",
                "-c",
                "user.name=Router Test",
                "commit",
                "--quiet",
                "-m",
                "track payload",
            )

            validate_symlinks(repository, ("scope",), RunMode.EDIT)


class SymlinkExecutorIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="safety-symlink-"))

    def _repo_with(self, name: str) -> Path:
        from tests.helpers import seed_smoke_test

        repository = init_repository(self.tmp / name)
        seed_smoke_test(repository)
        return repository

    def test_external_symlink_escalates_without_invoking_worker(self) -> None:
        import shutil

        repo = self._repo_with("escape-repo")
        _commit_symlink(repo, "escape.txt", "/etc/hosts")

        outcome = run_bounded_fixture(self.tmp, repository=repo)

        shutil.rmtree(self.tmp, True)
        self.assertEqual(outcome.result.status, "escalated")
        self.assertEqual(outcome.result.escalation_reason, "external-symlink-denied")
        self.assertEqual(outcome.invocation_count, 0)

    def test_read_only_mode_also_rejects_external_escape(self) -> None:
        import shutil

        repo = self._repo_with("readonly-escape")
        _commit_symlink(repo, "escape.txt", "/etc/hosts")

        outcome = run_bounded_fixture(
            self.tmp, repository=repo, mode="read-only"
        )

        shutil.rmtree(self.tmp, True)
        self.assertEqual(outcome.result.status, "escalated")
        self.assertEqual(outcome.result.escalation_reason, "external-symlink-denied")
        self.assertEqual(outcome.invocation_count, 0)

    def test_internal_symlink_run_still_succeeds(self) -> None:
        import shutil

        repo = self._repo_with("internal-ok")
        _commit_symlink(repo, "alias.txt", "example.txt")

        outcome = run_bounded_fixture(self.tmp, repository=repo)

        shutil.rmtree(self.tmp, True)
        self.assertEqual(outcome.result.status, "ready-for-review")


if __name__ == "__main__":
    unittest.main()

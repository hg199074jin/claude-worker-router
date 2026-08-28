"""Explicit verified integration tests (V1.2 Task 8).

``integrate_run`` fast-forwards the main checkout to a reviewed worker
commit only after a fixed preflight: evidence loads, mode is edit, status
is ``ready-for-review``, the worker commit exists and belongs to the worker
branch, the repository identity matches, the checkout is clean, HEAD still
equals ``base_sha``, the evidence manifest verifies, and tests passed.
Anything else is refused with a structured reason and leaves main intact.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_worker_router.integration import IntegrationError, integrate_run
from tests.helpers import init_repository, run_bounded_fixture, seed_smoke_test


def _git_argv(repository: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repository), *args],
        shell=False,
        check=True,
        text=True,
        capture_output=True,
    )
    return proc.stdout.strip()


class IntegrationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="integration-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, True)

    # ------------------------------------------------------------ helpers

    def _ready_run(self) -> tuple[object, Path]:
        repository = init_repository(self._tmp / "repo")
        seed_smoke_test(repository)
        outcome = run_bounded_fixture(
            self._tmp, behavior="fix", repository=repository
        )
        return outcome, repository

    def _integrate_expect_error(self, run_id: str, config) -> IntegrationError:
        with self.assertRaises(IntegrationError) as ctx:
            integrate_run(run_id, config)
        return ctx.exception

    # ------------------------------------------------------------- tests

    def test_valid_fast_forward_integrates_worker_commit(self) -> None:
        outcome, repository = self._ready_run()
        result = outcome.result
        self.assertEqual(result.status, "ready-for-review")

        merged_sha = integrate_run(result.run_id, outcome.config)

        self.assertEqual(merged_sha, result.commit)
        self.assertEqual(
            (repository / "example.txt").read_text(encoding="utf-8"),
            "worker\n",
        )
        self.assertEqual(_git_argv(repository, "rev-parse", "HEAD"), merged_sha)

        # Metadata records the integration; manifest stays verifiable.
        run_dir = next(p for p in outcome.runs_root.iterdir() if p.is_dir())
        metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["integrated_sha"], merged_sha)
        self.assertIn("integrated_at", metadata)
        manifest = json.loads(
            (run_dir / "evidence_manifest.json").read_text(encoding="utf-8")
        )
        for name, digest in manifest.items():
            actual = subprocess.run(
                ["shasum", "-a", "256", str(run_dir / name)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()[0]
            self.assertEqual(digest, actual)

        events = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        names = [json.loads(line)["event"] for line in events]
        self.assertIn("integration-started", names)
        self.assertIn("integration-completed", names)

    def test_dirty_main_checkout_rejected_without_merge(self) -> None:
        outcome, repository = self._ready_run()
        result = outcome.result
        (repository / "example.txt").write_text("local-draft\n", encoding="utf-8")

        error = self._integrate_expect_error(result.run_id, outcome.config)
        self.assertEqual(error.reason, "integration-dirty-checkout")
        self.assertEqual(
            (repository / "example.txt").read_text(encoding="utf-8"),
            "local-draft\n",
        )
        self.assertNotEqual(
            _git_argv(repository, "rev-parse", "HEAD"), result.commit
        )

    def test_moved_head_base_divergence_blocks_integration(self) -> None:
        outcome, repository = self._ready_run()
        result = outcome.result
        (repository / "extra.txt").write_text("downstream work\n", encoding="utf-8")
        _git_argv(repository, "add", "extra.txt")
        _git_argv(
            repository,
            "-c",
            "user.email=router-test@example.invalid",
            "-c",
            "user.name=Router Test",
            "commit",
            "--quiet",
            "-m",
            "advance main",
        )

        error = self._integrate_expect_error(result.run_id, outcome.config)
        self.assertEqual(error.reason, "integration-base-diverged")
        self.assertEqual(
            (repository / "example.txt").read_text(encoding="utf-8"), "main\n"
        )
        self.assertFalse((repository / "extra.txt").read_text(encoding="utf-8") is None)

    def test_escalated_run_is_not_integratable(self) -> None:
        repository = init_repository(self._tmp / "repo2")
        seed_smoke_test(repository)
        outcome = run_bounded_fixture(
            self._tmp, behavior="provider-error", repository=repository
        )
        result = outcome.result
        self.assertEqual(result.status, "escalated")

        error = self._integrate_expect_error(result.run_id, outcome.config)
        self.assertEqual(error.reason, "integration-status-invalid")

    def test_failed_tests_are_rejected_by_preflight(self) -> None:
        outcome, repository = self._ready_run()
        result = outcome.result
        run_dir = next(p for p in outcome.runs_root.iterdir() if p.is_dir())

        # Rewrite tests as failing, then refresh the manifest through the
        # legitimate EvidenceWriter API so integrity alone cannot explain
        # the upcoming rejection.
        from claude_worker_router.evidence import EvidenceWriter

        tests_path = run_dir / "tests.json"
        tests = json.loads(tests_path.read_text(encoding="utf-8"))
        tests[0]["exit_code"] = 5
        tests_path.write_text(json.dumps(tests), encoding="utf-8")
        result_path = run_dir / "result.json"
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payload["tests"] = tests
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        EvidenceWriter(outcome.runs_root, result.run_id).finalize_manifest()

        error = self._integrate_expect_error(result.run_id, outcome.config)
        self.assertEqual(error.reason, "integration-tests-failed")

    def test_evidence_manifest_mismatch_rejects_integration(self) -> None:
        outcome, repository = self._ready_run()
        result = outcome.result
        run_dir = next(p for p in outcome.runs_root.iterdir() if p.is_dir())

        # Tamper WITHOUT refreshing the manifest.
        result_path = run_dir / "result.json"
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payload["summary"] = "tampered summary"
        result_path.write_text(json.dumps(payload), encoding="utf-8")

        error = self._integrate_expect_error(result.run_id, outcome.config)
        self.assertEqual(error.reason, "integration-evidence-mismatch")

    def test_missing_worker_branch_rejects_integration(self) -> None:
        outcome, repository = self._ready_run()
        result = outcome.result
        # Remove the worktree first; the branch is checked out there.
        _git_argv(
            repository,
            "worktree",
            "remove",
            "--force",
            outcome.config.run_records.parent
            / ".codex-worktrees"
            / repository.name
            / result.run_id,
        )
        _git_argv(repository, "branch", "-D", result.branch)

        error = self._integrate_expect_error(result.run_id, outcome.config)
        self.assertEqual(error.reason, "integration-branch-missing")
        self.assertEqual(
            (repository / "example.txt").read_text(encoding="utf-8"), "main\n"
        )

    def test_missing_worker_commit_rejects_integratable_but_empty_runs(self) -> None:
        repository = init_repository(self._tmp / "repo3")
        seed_smoke_test(repository)
        # Pre-satisfy acceptance so the no-change run still reaches
        # ready-for-review without producing any commit.
        (repository / "example.txt").write_text("worker\n", encoding="utf-8")
        _git_argv(repository, "add", "example.txt")
        _git_argv(
            repository,
            "-c",
            "user.email=router-test@example.invalid",
            "-c",
            "user.name=Router Test",
            "commit",
            "--quiet",
            "-m",
            "pre-accept example",
        )

        outcome = run_bounded_fixture(
            self._tmp, behavior="no-change", repository=repository
        )
        result = outcome.result
        self.assertEqual(result.status, "ready-for-review")
        self.assertIsNone(result.commit)

        error = self._integrate_expect_error(result.run_id, outcome.config)
        self.assertEqual(error.reason, "integration-worker-commit-missing")


if __name__ == "__main__":
    unittest.main()


class IntegrationLockReleaseTests(unittest.TestCase):
    """Regression (review C6): the repo lock frees on EVERY exit path."""

    def test_failure_inside_critical_section_releases_lock(self) -> None:
        import tempfile
        import shutil
        from unittest.mock import patch

        from claude_worker_router.integration import IntegrationError, integrate_run
        from claude_worker_router.scheduler import (
            RepositoryBusy,
            repository_integration_lock,
        )

        tmp = Path(tempfile.mkdtemp(prefix="lock-release-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        repository = init_repository(tmp / "repo")
        seed_smoke_test(repository)
        outcome = run_bounded_fixture(tmp, behavior="fix", repository=repository)
        run_id = outcome.result.run_id
        self.assertEqual(outcome.result.status, "ready-for-review")

        lock_root = Path(outcome.config.run_records).parent / "locks"

        class _BoomWriter:
            def __init__(self, records_root, run_id):
                self.run_dir = Path(records_root) / run_id

            def append_event(self, *a, **k):
                raise OSError("simulated evidence failure in critical section")

        # The failure happens right AFTER the lock is taken, BEFORE merge.
        with patch(
            "claude_worker_router.integration.EvidenceWriter", _BoomWriter
        ):
            with self.assertRaises(OSError):
                integrate_run(run_id, outcome.config)

        # The lock MUST be free now: a second holder can acquire in-process.
        with repository_integration_lock(lock_root, repository):
            pass

        # And a real integration still succeeds afterwards.
        merged = integrate_run(run_id, outcome.config)
        self.assertEqual(merged, outcome.result.commit)


class CancelledRunIntegrityTests(unittest.TestCase):
    """Re-review #2/#3: discard intent is binding and manifest stays valid."""

    def _ready_run(self, tmp: Path):
        repository = init_repository(tmp / "repo")
        seed_smoke_test(repository)
        outcome = run_bounded_fixture(tmp, behavior="fix", repository=repository)
        self.assertEqual(outcome.result.status, "ready-for-review")
        return outcome, repository

    def test_cancelled_run_refuses_integration_even_with_repaired_manifest(self) -> None:
        import tempfile
        import shutil

        from claude_worker_router.cleanup import _manifest_verifies  # noqa: F401
        from claude_worker_router.evidence import EvidenceWriter
        from claude_worker_router.integration import IntegrationError, integrate_run
        from claude_worker_router.task_queue import cancel_run
        from tests.run_store_probe import manifest_ok  # local helper below

        tmp = Path(tempfile.mkdtemp(prefix="cancel-integrity-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        outcome, _repository = self._ready_run(tmp)
        run_id = outcome.result.run_id
        # Legacy stdin runs have no lifecycle row; create the
        # ready-for-review row a queued run would already carry.
        from claude_worker_router.state_store import StateStore, default_state_db_path

        StateStore(default_state_db_path(outcome.config)).ensure_row(
            run_id=run_id,
            repository=str(outcome.repository),
            mode="edit",
            final_status="ready-for-review",
            evidence_path=str(outcome.runs_root / run_id),
        )

        # Explicit discard of the reviewed result.
        cancelled = cancel_run(run_id, outcome.config)
        self.assertEqual(cancelled["action"], "discard-intent-recorded")

        # Repair the manifest the way an honest operator could; the discard
        # decision itself must still block integration.
        EvidenceWriter(outcome.runs_root, run_id).finalize_manifest()
        self.assertTrue(manifest_ok(outcome.runs_root, run_id))

        with self.assertRaises(IntegrationError) as ctx:
            integrate_run(run_id, outcome.config)
        self.assertIn(ctx.exception.reason, ("integration-status-invalid",))

    def test_cancel_event_appended_with_refreshed_manifest(self) -> None:
        import tempfile
        import shutil

        from claude_worker_router.task_queue import cancel_run
        from tests.run_store_probe import manifest_ok

        tmp = Path(tempfile.mkdtemp(prefix="cancel-manifest-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        outcome, _repository = self._ready_run(tmp)
        run_id = outcome.result.run_id
        from claude_worker_router.state_store import StateStore, default_state_db_path

        StateStore(default_state_db_path(outcome.config)).ensure_row(
            run_id=run_id,
            repository=str(outcome.repository),
            mode="edit",
            final_status="ready-for-review",
            evidence_path=str(outcome.runs_root / run_id),
        )
        cancel_run(run_id, outcome.config)

        # The cancel event landed AND the manifest still verifies.
        events_path = outcome.runs_root / run_id / "events.jsonl"
        self.assertIn('"cancelled"', events_path.read_text(encoding="utf-8"))
        self.assertTrue(
            manifest_ok(outcome.runs_root, run_id),
            "cancel invalidated the manifest without re-finalizing",
        )

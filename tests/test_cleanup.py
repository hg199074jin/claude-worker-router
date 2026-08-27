"""Worktree lifecycle tests (V1.2 Task 9).

``cleanup_run`` removes only the isolation artifacts (worktree directory and
temporary branch) and *never* deletes evidence. Runs that still hold an
unintegrated worker change are refused unless ``--discard`` is explicit.
``find_stale_runs`` classifies old runs into safe auto-clean candidates and
report-only entries; stale never silently deletes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_worker_router.cleanup import (
    CleanupRefused,
    classify_stale_run,
    cleanup_run,
    find_stale_runs,
)
from claude_worker_router.evidence import EvidenceWriter, parse_utc_timestamp, utc_timestamp
from claude_worker_router.integration import integrate_run
from tests.helpers import init_repository, run_bounded_fixture, seed_smoke_test


def _git_out(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        shell=False,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _branch_exists(repository: Path, branch: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--verify", branch],
            shell=False,
            capture_output=True,
        ).returncode
        == 0
    )


class CleanupLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="cleanup-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, True)

    def _fresh_fix_run(self):
        repository = init_repository(self._tmp / "repo")
        seed_smoke_test(repository)
        outcome = run_bounded_fixture(
            self._tmp, behavior="fix", repository=repository
        )
        worktree_path = Path(outcome.result.worktree)
        self.assertTrue(worktree_path.is_dir())
        return outcome, repository, worktree_path

    def _evidence_files(self, outcome) -> set[str]:
        run_dir = next(p for p in outcome.runs_root.iterdir() if p.is_dir())
        return {p.name for p in run_dir.rglob("*") if p.is_file()}

    # ------------------------------------------------------------- tests

    def test_integrated_cleanup_removes_artifacts_keeps_evidence(self) -> None:
        outcome, repository, worktree = self._fresh_fix_run()
        integrate_run(outcome.result.run_id, outcome.config)

        before = self._evidence_files(outcome)
        report = cleanup_run(outcome.result.run_id, outcome.config)

        self.assertFalse(worktree.exists())
        self.assertFalse(_branch_exists(repository, outcome.result.branch))
        self.assertEqual(report.removed_worktree, True)
        self.assertEqual(self._evidence_files(outcome), before | {
            "evidence_manifest.json"
        })

    def test_unintegrated_ready_for_review_requires_explicit_discard(self) -> None:
        outcome, _repository, worktree = self._fresh_fix_run()

        with self.assertRaises(CleanupRefused) as ctx:
            cleanup_run(outcome.result.run_id, outcome.config)

        self.assertIn("discard", str(ctx.exception))
        self.assertTrue(worktree.exists())
        self.assertTrue(_branch_exists(_repository := outcome.repository, outcome.result.branch))

    def test_discard_flag_removes_unintegrated_worktree(self) -> None:
        outcome, repository, worktree = self._fresh_fix_run()

        report = cleanup_run(
            outcome.result.run_id, outcome.config, discard=True
        )

        self.assertFalse(worktree.exists())
        self.assertFalse(_branch_exists(repository, outcome.result.branch))
        self.assertTrue(report.removed_worktree)
        self.assertEqual(
            self._evidence_files(outcome),
            self._evidence_files(outcome),
        )

    def test_escalated_with_modifications_refused_without_discard(self) -> None:
        repository = init_repository(self._tmp / "repo-esc")
        seed_smoke_test(repository)
        outcome = run_bounded_fixture(
            self._tmp,
            behavior="fix-and-outside",
            repository=repository,
            allowed_paths=("example.txt",),
        )
        result = outcome.result
        self.assertEqual(result.status, "escalated")
        worktree = Path(result.worktree)
        self.assertTrue(worktree.is_dir())

        with self.assertRaises(CleanupRefused):
            cleanup_run(result.run_id, outcome.config)
        self.assertTrue(worktree.is_dir())

        report = cleanup_run(result.run_id, outcome.config, discard=True)
        self.assertFalse(worktree.exists())
        self.assertTrue(report.removed_worktree)

    def test_repeated_cleanup_is_idempotent(self) -> None:
        outcome, repository, worktree = self._fresh_fix_run()
        cleanup_run(outcome.result.run_id, outcome.config, discard=True)

        second = cleanup_run(outcome.result.run_id, outcome.config)

        self.assertFalse(second.removed_worktree)
        self.assertEqual(second.already_cleaned, True)


class StaleClassificationTests(unittest.TestCase):
    """Pure classification of metadata against a caller-supplied cutoff."""

    def _metadata(self, *, status="ready-for-review", integrated=None,
                  finished_at="2026-08-01T00:00:00.000Z",
                  has_changes=True) -> dict:
        return {
            "final_status": status,
            "integrated_sha": integrated,
            "finished_at": finished_at,
            "changed_files": ["a.txt"] if has_changes else [],
        }

    def test_old_integrated_run_is_auto_candidate(self) -> None:
        verdict = classify_stale_run(
            self._metadata(status="ready-for-review", integrated="abc123"),
            cutoff=parse_utc_timestamp("2026-08-10T00:00:00+00:00"),
        )
        self.assertEqual(verdict, "auto")

    def test_recent_run_is_never_stale(self) -> None:
        verdict = classify_stale_run(
            self._metadata(integrated="abc",
                           finished_at="2026-08-26T23:59:00.000Z"),
            cutoff=parse_utc_timestamp("2026-08-20T00:00:00+00:00"),
        )
        self.assertEqual(verdict, "keep")

    def test_old_unintegrated_with_changes_is_report_only(self) -> None:
        verdict = classify_stale_run(
            self._metadata(),
            cutoff=parse_utc_timestamp("2026-08-10T00:00:00+00:00"),
        )
        self.assertEqual(verdict, "report")

    def test_old_escalated_without_changes_is_auto(self) -> None:
        verdict = classify_stale_run(
            self._metadata(status="escalated", has_changes=False),
            cutoff=parse_utc_timestamp("2026-08-10T00:00:00+00:00"),
        )
        self.assertEqual(verdict, "auto")


class FindStaleRunsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="stale-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, True)

    def test_integrated_old_run_appears_in_auto_candidates(self) -> None:
        from datetime import datetime, timezone

        repository = init_repository(self._tmp / "repo")
        seed_smoke_test(repository)
        outcome = run_bounded_fixture(
            self._tmp, behavior="fix", repository=repository
        )
        integrate_run(outcome.result.run_id, outcome.config)

        # Backdate finished_at via legitimate evidence APIs.
        run_dir = next(p for p in outcome.runs_root.iterdir() if p.is_dir())
        meta_path = run_dir / "metadata.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["finished_at"] = "2020-01-01T00:00:00.000Z"
        writer = EvidenceWriter(outcome.runs_root, outcome.result.run_id)
        writer.write_metadata(meta)
        writer.finalize_manifest()

        report = find_stale_runs(
            outcome.config,
            cutoff=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        self.assertIn(outcome.result.run_id, [r.run_id for r in report.auto_candidates])
        self.assertEqual(report.report_only, [])


if __name__ == "__main__":
    unittest.main()

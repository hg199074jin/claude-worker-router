"""Bounded-concurrency scheduling tests (V1.5 Tasks 22–26).

Pure conflict logic lives in :mod:`claude_worker_router.scheduler`; the
drain-side integration tests live further down this module once the
concurrent runner lands (Tasks 23–25).
"""

from __future__ import annotations

import unittest

from claude_worker_router.scheduler import paths_conflict


class PathConflictEngineTests(unittest.TestCase):
    def test_identical_paths_conflict(self) -> None:
        self.assertTrue(paths_conflict(("src/core",), ("src/core",)))

    def test_ancestor_conflicts_in_both_directions(self) -> None:
        self.assertTrue(paths_conflict(("src/core",), ("src/core/models",)))
        self.assertTrue(paths_conflict(("src/core/models",), ("src/core",)))

    def test_disjoint_paths_do_not_conflict(self) -> None:
        self.assertFalse(paths_conflict(("src/backend",), ("web/frontend",)))
        self.assertFalse(
            paths_conflict(
                ("src/core/a",), ("src/cored/b",)
            )  # prefix must respect component boundary
        )

    def test_multi_entry_sets_detect_any_overlap(self) -> None:
        self.assertTrue(paths_conflict(("a", "b"), ("b", "c")))
        self.assertFalse(paths_conflict(("a", "b"), ("c", "d")))

    def test_component_boundary_is_respected(self) -> None:
        # "src" is NOT a prefix of "srcx/file".
        self.assertFalse(paths_conflict(("src",), ("srcx/file.py",)))

    def test_empty_scope_means_read_only_never_conflicts(self) -> None:
        self.assertFalse(paths_conflict((), ("src",)))
        self.assertFalse(paths_conflict(("src",), ()))


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# Task 24: concurrent runner

from pathlib import Path
from unittest.mock import patch
import threading
import time

from claude_worker_router.models import RunResult
from tests.test_queue_cli import QueueCliHarness, _valid_task


class ConcurrencyTwoRunnerTests(QueueCliHarness):
    """A harness where the drainer is configured with max_concurrency = 2."""

    def _enable_two(self) -> None:
        text = self.config_path.read_text(encoding="utf-8")
        assert "max_concurrency" not in text
        self.config_path.write_text(text + "\nmax_concurrency = 2\n", encoding="utf-8")
        from claude_worker_router.config import load_config

        self._config = load_config(self.config_path)

    def _submit_pair(self, tags=("c1", "c2")) -> list[str]:
        ids = []
        for tag in tags:
            payload = _valid_task(repository=str(self.tmp / f"c-{tag}"), task=f"job-{tag}")
            Path(payload["repository"]).mkdir(exist_ok=True)
            _, submitted = self._submit(payload)
            ids.append(submitted["run_id"])
        return ids

    def test_two_disjoint_tasks_actually_overlap(self) -> None:
        self._enable_two()
        self._submit_pair()
        barrier = threading.Barrier(2, timeout=5)
        overlap_seconds: list[float] = []

        def fake_execute(request, config, on_child_start=None, run_id=None):
            entered = time.monotonic()
            barrier.wait()  # would raise on a serial runner
            time.sleep(0.05)
            overlap_seconds.append(time.monotonic() - entered)
            return RunResult(run_id=run_id or "", status="read-only")

        with (
            patch("claude_worker_router.task_queue.execute_task", fake_execute),
            patch(
                "claude_worker_router.task_queue.read_current_fingerprint",
                side_effect=lambda cfg: "fp-stable",
            ),
            patch("claude_worker_router.task_queue.os.getpid", return_value=991),
        ):
            code, out, err = self._main(["--config", str(self.config_path), "drain"])

        self.assertEqual(code, 0, err)
        rows = list(self._all_rows().values())
        self.assertEqual({r["lifecycle"] for r in rows}, {"ready-for-review"})
        # Both workers truly ran inside the same window.
        self.assertEqual(len(overlap_seconds), 2)

    def test_backlog_larger_than_limit_processes_in_batches(self) -> None:
        self._enable_two()
        ids = self._submit_pair(tags=("b1", "b2")) + [None]
        third = _valid_task(repository=str(self.tmp / "c-b3"), task="job-b3", priority=9)
        Path(third["repository"]).mkdir(exist_ok=True)
        _, submitted = self._submit(third)
        ids[2] = submitted["run_id"]

        executed: list[str] = []
        lock = threading.Lock()

        def fake_execute(request, config, on_child_start=None, run_id=None):
            with lock:
                executed.append(request.task)
            return RunResult(run_id=run_id or "", status="read-only")

        with (
            patch("claude_worker_router.task_queue.execute_task", fake_execute),
            patch(
                "claude_worker_router.task_queue.read_current_fingerprint",
                side_effect=lambda cfg: "fp-stable",
            ),
            patch("claude_worker_router.task_queue.os.getpid", return_value=992),
        ):
            code, out, err = self._main(["--config", str(self.config_path), "drain"])

        self.assertEqual(code, 0, err)
        self.assertEqual(len(executed), 3)
        rows = list(self._all_rows().values())
        self.assertEqual({r["lifecycle"] for r in rows}, {"ready-for-review"})

    def test_max_concurrency_one_keeps_sequential_default(self) -> None:
        ids = self._submit_pair()
        order: list[str] = []

        def fake_execute(request, config, on_child_start=None, run_id=None):
            order.append(request.task)
            return RunResult(run_id=run_id or "", status="read-only")

        with (
            patch("claude_worker_router.task_queue.execute_task", fake_execute),
            patch(
                "claude_worker_router.task_queue.read_current_fingerprint",
                side_effect=lambda cfg: "fp-stable",
            ),
            patch("claude_worker_router.task_queue.os.getpid", return_value=993),
        ):
            code, out, err = self._main(["--config", str(self.config_path), "drain"])
        self.assertEqual(code, 0, err)
        self.assertEqual(order, ["job-c1", "job-c2"])

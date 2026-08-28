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
import sqlite3
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


# --------------------------------------------------------------------------
# Task 25: exclusive tests (request-level adaptation pending V1.3 profiles)

class ExclusiveSchedulingTests(QueueCliHarness):
    def _enable_two(self):
        text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(text + "\nmax_concurrency = 2\n", encoding="utf-8")
        from claude_worker_router.config import load_config

        self._config = load_config(self.config_path)

    def _submit_exclusive_then_pair(self):
        ex = _valid_task(
            repository=str(self.tmp / "x-exc"),
            task="job-exclusive",
            priority=9,
            exclusive_tests=True,
        )
        Path(ex["repository"]).mkdir(exist_ok=True)
        _, ex_sub = self._submit(ex)

        normals = []
        for tag in ("n1", "n2"):
            payload = _valid_task(repository=str(self.tmp / f"x-{tag}"), task=f"job-{tag}")
            Path(payload["repository"]).mkdir(exist_ok=True)
            _, sub = self._submit(payload)
            normals.append(sub["run_id"])
        return ex_sub["run_id"], normals

    def test_model_contract_parses_and_persists_flag(self):
        import tempfile
        from claude_worker_router.models import RunMode, TaskRequest, TestCommand

        request = TaskRequest.from_dict(
            {
                "repository": "/tmp/x",
                "task": "exclusive fixture",
                "mode": "edit",
                "test_commands": [["uv"]],
                "allowed_paths": ["a"],
                "exclusive_tests": True,
            }
        )
        self.assertTrue(request.exclusive_tests)
        self.assertTrue(request.to_dict()["exclusive_tests"])

        for bad in ("yes", 1, None):
            data = {
                "repository": "/tmp/x",
                "task": "bad",
                "mode": "read-only",
                "allowed_paths": [],
                "test_commands": [],
                "exclusive_tests": bad,
            }
            with self.subTest(bad=bad):
                from claude_worker_router.models import RunMode as RM

                data["mode"] = str(RM.READ_ONLY.value)
                with self.assertRaisesRegex(ValueError, "exclusive_tests"):
                    TaskRequest.from_dict(data)

    def test_exclusive_batch_runs_alone_before_others(self):
        self._enable_two()
        ex_id, normal_ids = self._submit_exclusive_then_pair()

        intervals: dict[str, tuple[float, float]] = {}
        lock = threading.Lock()

        def fake_execute(request, config, on_child_start=None, run_id=None):
            start = time.monotonic()
            time.sleep(0.08)
            end = time.monotonic()
            with lock:
                intervals[run_id or ""] = (start, end)
            return RunResult(run_id=run_id or "", status="read-only")

        with (
            patch("claude_worker_router.task_queue.execute_task", fake_execute),
            patch(
                "claude_worker_router.task_queue.read_current_fingerprint",
                side_effect=lambda cfg: "fp-stable",
            ),
            patch("claude_worker_router.task_queue.os.getpid", return_value=995),
        ):
            code, out, err = self._main(["--config", str(self.config_path), "drain"])

        self.assertEqual(code, 0, err)
        self.assertEqual(len(intervals), 3)

        def overlaps(a, b):
            return max(a[0], b[0]) < min(a[1], b[1])

        exc_interval = intervals[ex_id]
        for nid in normal_ids:
            self.assertFalse(
                overlaps(exc_interval, intervals[nid]),
                f"exclusive overlapped {nid}",
            )
        # The two non-exclusive tasks still ran concurrently with each other.
        n1, n2 = (intervals[nid] for nid in normal_ids)
        self.assertTrue(overlaps(n1, n2))


class ClaimAbandonmentTests(QueueCliHarness):
    """Regression: a conflicting mid-priority row must never be stolen."""

    def test_conflicting_middle_row_reaches_terminal_state(self) -> None:
        text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(text + "\nmax_concurrency = 2\n", encoding="utf-8")
        from claude_worker_router.config import load_config

        self._config = load_config(self.config_path)

        specs = [
            ("A", 9, ("src/core",)),
            ("B", 5, ("src/core/models",)),  # conflicts with A
            ("C", 1, ("web",)),
        ]
        for tag, prio, scope in specs:
            payload = _valid_task(
                repository=str(self.tmp / "same-repo"),
                task=f"job-{tag}",
                priority=prio,
                allowed_paths=scope,
            )
            Path(payload["repository"]).mkdir(exist_ok=True)
            self._submit(payload)

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
            patch("claude_worker_router.task_queue.os.getpid", return_value=997),
        ):
            code, out, err = self._main(["--config", str(self.config_path), "drain"])

        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(executed), ["job-A", "job-B", "job-C"])
        rows = self._all_rows()
        self.assertEqual(len(rows), 3)
        for run_id, row in rows.items():
            self.assertNotEqual(
                row["lifecycle"],
                "running",
                f"{run_id} abandoned in running state",
            )
            self.assertEqual(row["lifecycle"], "ready-for-review")


# --------------------------------------------------------------------------
# Re-review #1: join budget & genuine wedge handling

from claude_worker_router.config import RouterConfig as _RC


def _budget_config(timeout_seconds=1200, correction_limit=1) -> RouterConfig:
    return _RC(
        command="claude",
        provider="cc-switch-current",
        max_turns=5,
        timeout_seconds=timeout_seconds,
        correction_limit=correction_limit,
        max_changed_files=5,
        max_diff_lines=500,
        allowed_test_binaries=("uv",),
        run_records=Path("/tmp/unused-runs"),
        test_output_limit_bytes=65536,
        claude_settings=Path("/tmp/unused-settings.json"),
        max_concurrency=2,
    )


class JoinBudgetTests(unittest.TestCase):
    def test_budget_covers_correction_loop_and_tests(self) -> None:
        from claude_worker_router.task_queue import _join_budget

        # correction_limit=1 → two worker attempts; one test phase each
        # plus the attempt itself; plus slack. The old fixed budget
        # (timeout+30) covered only ONE attempt.
        config = _budget_config(timeout_seconds=1200, correction_limit=1)
        budget = _join_budget(config, n_test_commands=1)
        self.assertGreaterEqual(budget, 2 * 1200 * 2)
        old_style = config.timeout_seconds + 30
        self.assertGreater(budget, old_style)

    def test_zero_correction_still_covers_attempt_plus_tests(self) -> None:
        from claude_worker_router.task_queue import _join_budget

        config = _budget_config(timeout_seconds=60, correction_limit=0)
        self.assertGreaterEqual(_join_budget(config, n_test_commands=2), 60 * 3)


class ExternallyFinalizedStateTests(unittest.TestCase):
    def test_missing_state_after_external_finalization_returns_safe_blocked_step(self) -> None:
        """A vanished state DB must not crash a worker thread after a wedge."""
        from claude_worker_router.state_store import StateTransitionError
        from claude_worker_router.task_queue import _execute_claim_row

        class StateStoreLostAfterExternalFinalization:
            def finish(self, *args, **kwargs):
                raise StateTransitionError("blocked -> ready-for-review")

            def get(self, run_id):
                raise sqlite3.OperationalError("no such table: runs")

        row = {"run_id": "lost-state-row"}
        result = RunResult(run_id="lost-state-row", status="read-only")
        with patch(
            "claude_worker_router.task_queue._execute_claimed", return_value=result
        ):
            step = _execute_claim_row(
                row, _budget_config(), StateStoreLostAfterExternalFinalization()
            )

        self.assertEqual(step["lifecycle"], "blocked")
        self.assertEqual(step["outcome"], "state-unavailable")
        self.assertTrue(step["externally_finalized"])


class WedgedThreadStopsDrainTests(QueueCliHarness):
    def test_genuine_wedge_blocks_row_and_stops_draining(self) -> None:
        import time as _time

        text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(text + "\nmax_concurrency = 2\n", encoding="utf-8")
        from claude_worker_router.config import load_config

        self._config = load_config(self.config_path)

        payload = _valid_task(repository=str(self.tmp / "wedge"))
        Path(payload["repository"]).mkdir(exist_ok=True)
        _, submitted = self._submit(payload)
        run_id = submitted["run_id"]

        release = threading.Event()
        worker_finished = threading.Event()

        def slow_fake(request, config, on_child_start=None, run_id=None):
            release.wait(timeout=10)  # simulate a wedged worker
            return RunResult(run_id=run_id or "", status="read-only")

        from claude_worker_router.task_queue import _execute_claim_row

        def tracked_execute_claim_row(*args, **kwargs):
            try:
                return _execute_claim_row(*args, **kwargs)
            finally:
                worker_finished.set()

        with (
            patch(
                "claude_worker_router.task_queue.execute_task", slow_fake
            ),
            patch(
                "claude_worker_router.task_queue.read_current_fingerprint",
                side_effect=lambda cfg: "fp",
            ),
            patch("claude_worker_router.task_queue.os.getpid", return_value=998),
            patch(
                "claude_worker_router.task_queue._join_budget",
                side_effect=lambda cfg, n_test_commands: 0.05,
            ),
            patch(
                "claude_worker_router.task_queue._execute_claim_row",
                side_effect=tracked_execute_claim_row,
            ),
        ):
            code, out, err = self._main(
                ["--config", str(self.config_path), "drain"]
            )
            try:
                self.assertEqual(code, 3)
                self.assertIn("runner-wedged", out + err)
                row = self._store().get(run_id)
                self.assertEqual(row["lifecycle"], "blocked")
                self.assertEqual(row["outcome"], "runner-wedged")
            finally:
                release.set()
                self.assertTrue(worker_finished.wait(timeout=2))

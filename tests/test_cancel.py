"""Cancellation semantics tests (V1.4 Task 21).

* ``pending``      -> cancelled instantly, before any worker call.
* ``running``      -> the run's dedicated process GROUP is terminated
                      (never the operator's shell session), the state moves
                      to cancelled with ``cancelled-by-user``, and both the
                      worktree and all evidence stay in place.
* ``ready-for-review`` -> cancelling records an explicit discard intent;
                      isolation artifacts are NOT auto-removed.
* terminal rows    -> refused.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests.test_queue_cli import QueueCliHarness, _valid_task


class CancelPendingTests(QueueCliHarness):
    def test_cancel_pending_is_immediate(self) -> None:
        payload = _valid_task(repository=str(self.tmp / "p1"))
        Path(payload["repository"]).mkdir(exist_ok=True)
        _, submitted = self._submit(payload)
        run_id = submitted["run_id"]

        result = self._cancel(run_id)

        self.assertEqual(result["action"], "cancelled-before-start")
        row = self._store().get(run_id)
        self.assertEqual(row["lifecycle"], "cancelled")
        self.assertEqual(row["outcome"], "cancelled-by-user")
        # Evidence remains readable and untouched.
        self.assertTrue((Path(row["evidence_path"]) / "request.json").is_file())

    def test_cancel_unknown_run_exits_two(self) -> None:
        code, out, err = self._main(["--config", str(self.config_path), "cancel", "f" * 32])
        self.assertEqual(code, 2)

    def test_cancel_terminal_blocked_row_is_refused(self) -> None:
        payload = _valid_task(repository=str(self.tmp / "blk"))
        Path(payload["repository"]).mkdir(exist_ok=True)
        _, submitted = self._submit(payload)
        run_id = submitted["run_id"]

        self._force_lifecycle(run_id, "blocked")

        code, out, err = self._cancel_cli(run_id)
        self.assertEqual(code, 2)
        self.assertIn("terminal", err)


class CancelRunningTests(QueueCliHarness):
    def test_running_cancel_kills_dedicated_group_not_self(self) -> None:
        """The runner child owns its own session; cancel spares our group."""
        payload = _valid_task(repository=str(self.tmp / "run"))
        Path(payload["repository"]).mkdir(exist_ok=True)
        _, submitted = self._submit(payload)
        run_id = submitted["run_id"]

        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(60)"],
            start_new_session=True,
        )
        try:
            self._simulate_runner_claim(run_id, sleeper.pid)

            result = self._cancel(run_id)
            deadline = time.time() + 5
            while sleeper.poll() is None and time.time() < deadline:
                time.sleep(0.05)

            self.assertIsNotNone(sleeper.poll(), "runner group survived cancel")
            self.assertEqual(result["action"], "terminated-running")
            row = self._store().get(run_id)
            self.assertEqual(row["lifecycle"], "cancelled")
            self.assertEqual(row["outcome"], "cancelled-by-user")
        finally:
            if sleeper.poll() is None:
                sleeper.kill()

    def test_rf_r_cancel_records_discard_intent_without_cleanup(self) -> None:
        payload = _valid_task(repository=str(self.tmp / "rf"))
        Path(payload["repository"]).mkdir(exist_ok=True)
        _, submitted = self._submit(payload)
        run_id = submitted["run_id"]
        self._force_lifecycle(run_id, "ready-for-review",
                              started_at=None, finished_at=None,
                              lifecycle_only=True)

        result = self._cancel(run_id)

        self.assertEqual(result["action"], "discard-intent-recorded")
        row = self._store().get(run_id)
        self.assertEqual(row["lifecycle"], "cancelled")


# --------------------------------------------------- helpers on the harness


def _force_lifecycle(
    self,
    run_id: str,
    lifecycle: str,
    *,
    started_at="2026-08-27T00:00:00.000Z",
    finished_at="2026-08-27T01:00:00.000Z",
    lifecycle_only=False,
) -> None:
    payload = {
        "lifecycle": lifecycle,
        "outcome": "escalated" if lifecycle == "blocked" else "forced",
    }
    if not lifecycle_only:
        payload["finished_at"] = finished_at
    if started_at is not None:
        payload["started_at"] = started_at
    assignments = ", ".join(f"{k}=?" for k in payload)
    values = list(payload.values()) + [run_id]
    with sqlite3.connect(str(self.db_path)) as conn:
        conn.execute(f"UPDATE runs SET {assignments} WHERE run_id=?", values)


def _simulate_runner_claim_method(self, run_id: str, pid: int) -> None:
    with sqlite3.connect(str(self.db_path)) as conn:
        conn.execute(
            "UPDATE runs SET lifecycle='running',"
            " started_at='2026-08-27T00:00:00.000Z', pid=? WHERE run_id=?",
            (pid, run_id),
        )


def _cancel(self, run_id: str) -> dict:
    from claude_worker_router.task_queue import cancel_run

    return cancel_run(run_id, self._config)


def _cancel_cli(self, run_id: str) -> tuple[int, str, str]:
    return self._main(["--config", str(self.config_path), "cancel", run_id])


QueueCliHarness._force_lifecycle = _force_lifecycle
QueueCliHarness._simulate_runner_claim = _simulate_runner_claim_method
QueueCliHarness._cancel = _cancel
QueueCliHarness._cancel_cli = _cancel_cli

if __name__ == "__main__":
    unittest.main()

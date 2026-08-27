"""Provider-epoch scheduling guard tests (V1.5 Task 23).

A concurrent batch runs under one provider fingerprint (the ``epoch``).
Before every further dispatch the current fingerprint is re-read; if CC
Switch changed underneath us, no NEW tasks start, already-running ones
finish under their own end-of-run verification, and nothing switches
automatically. Exit code 5 signals the stop.

Also covers the SQLite schema v1→v2 migration adding ``provider_epoch``.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from claude_worker_router import cli
from claude_worker_router.state_store import StateStore
from tests.test_queue_cli import QueueCliHarness, _valid_task


class SchemaMigrationTests(unittest.TestCase):
    def test_v1_database_gains_provider_epoch_column(self) -> None:
        import tempfile
        import shutil

        tmp = Path(tempfile.mkdtemp(prefix="epoch-mig-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        db = tmp / "state.db"

        # Hand-build a V1 database exactly as shipped in V1.4.
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                repository TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'edit',
                lifecycle TEXT NOT NULL,
                outcome TEXT,
                priority INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                parent_run_id TEXT,
                evidence_path TEXT NOT NULL,
                pid INTEGER
            );
            INSERT INTO runs (
                run_id, repository, lifecycle, created_at, evidence_path
            ) VALUES ('a', '/r', 'pending', '2026-08-27T00:00:00.000Z', '/e');
            PRAGMA user_version = 1;
            """
        )
        conn.commit()
        conn.close()

        store = StateStore(db)
        self.assertEqual(store.schema_version(db), 2)
        row = store.get("a")
        self.assertIn("provider_epoch", row)
        # The upgraded store can write epochs through claiming.
        claimed = store.claim_next(provider_epoch="fp-001")
        self.assertEqual(claimed["provider_epoch"], "fp-001")


class ProviderEpochDrainTests(QueueCliHarness):
    def _submit_two(self, tags=("high", "low")) -> list[str]:
        ids = []
        for index, tag in enumerate(tags):
            payload = _valid_task(
                repository=str(self.tmp / f"ep-{tag}"),
                task=f"job-{tag}",
                priority=len(tags) - index,
            )
            Path(payload["repository"]).mkdir(exist_ok=True)
            _, submitted = self._submit(payload)
            ids.append(submitted["run_id"])
        return ids

    def _drain(self):
        return self._main(["--config", str(self.config_path), "drain"])

    def test_stable_fingerprint_drains_everything(self) -> None:
        ids = self._submit_two()
        calls: list[str] = []

        def fake_execute(request, config, on_child_start=None, run_id=None):
            calls.append(request.task)
            from claude_worker_router.models import RunResult

            return RunResult(run_id=run_id or "", status="read-only")

        with (
            patch("claude_worker_router.task_queue.execute_task", fake_execute),
            patch(
                "claude_worker_router.task_queue.read_current_fingerprint",
                side_effect=["fp-A", "fp-A", "fp-A"],
            ),
            patch("claude_worker_router.task_queue.os.getpid", return_value=777),
        ):
            code, out, err = self._drain()

        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(calls), ["job-high", "job-low"])
        epochs = {
            r["run_id"]: r["provider_epoch"] for r in self._all_rows().values()
        }
        self.assertEqual(set(epochs.values()), {"fp-A"})

    def test_provider_change_stops_dispatch_without_touching_pending(self) -> None:
        ids = self._submit_two()

        executed: list[str] = []

        def fake_execute(request, config, on_child_start=None, run_id=None):
            executed.append(request.task)
            from claude_worker_router.models import RunResult

            return RunResult(run_id=run_id or "", status="read-only")

        with (
            patch("claude_worker_router.task_queue.execute_task", fake_execute),
            patch(
                "claude_worker_router.task_queue.read_current_fingerprint",
                side_effect=["fp-A", "fp-B"],
            ),
            patch("claude_worker_router.task_queue.os.getpid", return_value=778),
        ):
            code, out, err = self._drain()

        self.assertEqual(code, 5)
        # Exactly the top-priority job ran under the original epoch.
        self.assertEqual(executed, ["job-high"])
        err_text = out + err
        self.assertIn("provider", err_text.lower())
        self.assertIn("stopped", err_text.lower())

        store = self._store()
        finished = store.get(ids[0])
        untouched = store.get(ids[1])
        self.assertEqual(finished["lifecycle"], "ready-for-review")
        self.assertEqual(finished["provider_epoch"], "fp-A")
        self.assertEqual(untouched["lifecycle"], "pending")


def _rows(self):
    import sqlite3

    with sqlite3.connect(str(self.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return {r["run_id"]: dict(r) for r in conn.execute("SELECT * FROM runs")}


QueueCliHarness._all_rows = _rows

if __name__ == "__main__":
    unittest.main()

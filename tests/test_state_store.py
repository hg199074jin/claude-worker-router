"""SQLite state store tests (V1.4 Task 18).

The state database is the single source of truth for run *lifecycle*
(evidence directories remain the source of truth for *execution facts*).
It must survive restarts, hand out each pending run exactly once to a
drainer, and expose interrupted ``running`` rows so recovery can move them
to ``blocked`` without any silent retry.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claude_worker_router.models import RunLifecycle
from claude_worker_router.state_store import (
    StateStore,
    StateTransitionError,
)


def _row(**overrides):
    base = {
        "run_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "repository": "/repo/one",
        "priority": 0,
        "created_at": "2026-08-27T00:00:00.000Z",
        "evidence_path": "/repo-records/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    }
    base.update(overrides)
    return base


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="state-store-"))
        self.db_path = self.tmp / "state.db"
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, True)

    def _store(self) -> StateStore:
        return StateStore(self.db_path)

    # ------------------------------------------------------------- schema

    def test_database_persists_across_reopen(self) -> None:
        store = self._store()
        store.insert_pending(**_row())
        reopened = self._store()
        fetched = reopened.get("a" * 32)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["lifecycle"], "pending")

    def test_schema_version_is_recorded(self) -> None:
        self._store()
        self.assertEqual(StateStore.schema_version(self.db_path), 1)

    def test_insert_defaults_lifecycle_outcome_and_timestamps(self) -> None:
        row = {
            **_row(),
            "created_at": None,
        }
        store = self._store()
        store.insert_pending(
            run_id=row["run_id"],
            repository=row["repository"],
            priority=row["priority"],
            created_at=None,
            evidence_path=row["evidence_path"],
        )
        fetched = store.get(row["run_id"])
        self.assertIsNotNone(fetched["created_at"])
        self.assertIsNone(fetched["outcome"])
        self.assertIsNone(fetched["started_at"])

    # ------------------------------------------------------------ claiming

    def test_claim_next_orders_priority_desc_then_created_asc(self) -> None:
        store = self._store()
        store.insert_pending(
            **_row(
                run_id="b" * 32,
                priority=1,
                created_at="2026-08-27T01:00:00.000Z",
                evidence_path="/r/b",
            )
        )
        store.insert_pending(
            **_row(
                run_id="c" * 32,
                priority=5,
                created_at="2026-08-27T05:00:00.000Z",
                evidence_path="/r/c",
            )
        )
        store.insert_pending(**_row())

        first = store.claim_next()
        second = store.claim_next()
        third = store.claim_next()

        self.assertEqual(first["run_id"], "c" * 32)
        self.assertEqual(second["run_id"], "b" * 32)
        self.assertEqual(third["run_id"], "a" * 32)
        self.assertIsNone(store.claim_next())

    def test_claim_flips_to_running_with_pid_and_started_at(self) -> None:
        store = self._store()
        store.insert_pending(**_row())
        claimed = store.claim_next(pid=4242)

        self.assertEqual(claimed["lifecycle"], "running")
        self.assertIsNone(store.claim_next())

        row = store.get(claimed["run_id"])
        self.assertEqual(row["pid"], 4242)
        self.assertIsNotNone(row["started_at"])

    # --------------------------------------------------------- transitions

    def test_illegal_transition_raises_state_transition_error(self) -> None:
        store = self._store()
        store.insert_pending(**_row())
        with self.assertRaisesRegex(StateTransitionError, "pending -> integrated"):
            store.update_lifecycle("a" * 32, RunLifecycle.INTEGRATED)

    def test_terminal_rows_refuse_further_updates(self) -> None:
        store = self._store()
        store.insert_pending(**_row())
        store.update_lifecycle("a" * 32, RunLifecycle.CANCELLED)
        with self.assertRaises(StateTransitionError):
            store.update_lifecycle("a" * 32, RunLifecycle.RUNNING)

    def test_finish_path_ready_for_review_then_integrated(self) -> None:
        store = self._store()
        store.insert_pending(**_row())
        store.update_lifecycle("a" * 32, RunLifecycle.RUNNING)
        store.finish("a" * 32, lifecycle=RunLifecycle.READY_FOR_REVIEW,
                     outcome="read-only", finished_at="2026-08-27T02:00:00.000Z")
        store.update_lifecycle("a" * 32, RunLifecycle.INTEGRATED)
        row = store.get("a" * 32)
        self.assertEqual(row["outcome"], "read-only")
        self.assertEqual(row["lifecycle"], "integrated")
        self.assertEqual(row["finished_at"], "2026-08-27T02:00:00.000Z")

    # ------------------------------------------------------ crash recovery

    def test_find_interrupted_lists_running_with_dead_pid(self) -> None:
        store = self._store()
        store.insert_pending(**_row())
        store.claim_next(pid=111)
        store.insert_pending(
            **_row(run_id="d" * 32, evidence_path="/r/d"))
        store.update_lifecycle("d" * 32, RunLifecycle.RUNNING, pid=222)

        alive_pids = {222}
        interrupted = store.find_interrupted(
            lambda pid: pid in alive_pids
        )
        self.assertEqual([r["run_id"] for r in interrupted], ["a" * 32])

    def test_reconcile_interrupted_moves_to_blocked_with_reason(self) -> None:
        store = self._store()
        store.insert_pending(**_row())
        store.claim_next(pid=999)

        count = store.reconcile_interrupted(
            lambda pid: False, reason="runner-interrupted"
        )

        self.assertEqual(count, 1)
        row = store.get("a" * 32)
        self.assertEqual(row["lifecycle"], "blocked")
        self.assertEqual(row["outcome"], "runner-interrupted")

    # -------------------------------------------------------------- queries

    def test_list_lifecycle_filters(self) -> None:
        store = self._store()
        store.insert_pending(**_row())
        store.insert_pending(**_row(run_id="e" * 32, evidence_path="/r/e"))
        store.update_lifecycle("e" * 32, RunLifecycle.CANCELLED)

        pending_ids = [r["run_id"] for r in store.list_lifecycle(RunLifecycle.PENDING)]
        cancelled_ids = [
            r["run_id"] for r in store.list_lifecycle(RunLifecycle.CANCELLED)
        ]
        self.assertEqual(pending_ids, ["a" * 32])
        self.assertEqual(cancelled_ids, ["e" * 32])

    def test_upsert_from_unknown_run_creates_blocked_free_row(self) -> None:
        """Runs never submitted through the queue still get state tracking."""
        store = self._store()
        row = store.ensure_row(
            run_id="f" * 32,
            repository="/repo/x",
            mode="edit",
            final_status="ready-for-review",
            evidence_path="/records/f" + "0" * 25,
        )
        fetched = store.get("f" * 32)
        self.assertEqual(fetched["lifecycle"], "ready-for-review")
        self.assertIsNotNone(fetched["repository"])


if __name__ == "__main__":
    unittest.main()

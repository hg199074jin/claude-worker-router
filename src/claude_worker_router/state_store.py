"""SQLite-backed run lifecycle store (V1.4).

The database answers *lifecycle* questions only — who is pending, what is
running, what finished, what got cancelled, which runners died mid-flight.
Execution facts stay in the evidence directories; this store references
them via ``evidence_path`` and never duplicates their content.

Migration policy: standard-library ``PRAGMA user_version`` only.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .evidence import utc_timestamp
from .models import RunLifecycle, assert_lifecycle_transition

_SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    repository    TEXT NOT NULL,
    mode          TEXT NOT NULL DEFAULT 'edit',
    lifecycle     TEXT NOT NULL,
    outcome       TEXT,
    priority      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    parent_run_id TEXT,
    evidence_path TEXT NOT NULL,
    pid           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_runs_lifecycle ON runs(lifecycle);
CREATE INDEX IF NOT EXISTS idx_runs_queue ON runs(lifecycle, priority DESC, created_at ASC);
"""


def default_state_db_path(config: Any) -> Path:
    """The lifecycle database lives next to the configured run records."""
    return Path(config.run_records).parent / "state.db"


class StateTransitionError(RuntimeError):
    """Raised when a lifecycle update violates the state machine."""


def _validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not run_id or "/" in run_id:
        raise ValueError(f"unsafe run id: {run_id!r}")
    return run_id


class StateStore:
    """Owns one ``state.db`` file; safe to open repeatedly."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._migrate()

    # ------------------------------------------------------------ plumbing

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > _SCHEMA_VERSION:
                raise RuntimeError(
                    f"state.db schema {version} is newer than this router "
                    f"(supports {_SCHEMA_VERSION})"
                )
            if version < _SCHEMA_VERSION:
                conn.executescript(_SCHEMA)
            # v1 → v2: additive provider_epoch column for batch scheduling.
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "provider_epoch" not in columns and version < 2:
                try:
                    conn.execute(
                        "ALTER TABLE runs ADD COLUMN provider_epoch TEXT"
                    )
                except sqlite3.OperationalError as exc:
                    # Two processes racing the first migration: the winner
                    # already added the column. Anything else is real.
                    if "duplicate column" not in str(exc).lower():
                        raise
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @staticmethod
    def schema_version(db_path: Path) -> int:
        with sqlite3.connect(str(db_path)) as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    @staticmethod
    def _to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    # ------------------------------------------------------------- writes

    def insert_pending(
        self,
        *,
        run_id: str,
        repository: str,
        priority: int = 0,
        created_at: str | None = None,
        parent_run_id: str | None = None,
        evidence_path: str,
        mode: str = "edit",
    ) -> None:
        """Register a queued task; ``created_at=None`` stamps UTC now."""
        _validate_run_id(run_id)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, repository, mode, lifecycle, outcome, priority,
                    created_at, started_at, finished_at, parent_run_id,
                    evidence_path, pid
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, NULL, ?, ?, NULL)
                """,
                (
                    run_id,
                    repository,
                    mode,
                    RunLifecycle.PENDING.value,
                    int(priority),
                    created_at or utc_timestamp(),
                    parent_run_id,
                    evidence_path,
                ),
            )

    def ensure_row(
        self,
        *,
        run_id: str,
        repository: str,
        mode: str,
        final_status: str,
        evidence_path: str,
    ) -> dict[str, Any]:
        """Lazily track a run that bypassed ``submit`` (e.g. legacy stdin)."""
        from .models import lifecycle_from_outcome

        row = self.get(run_id)
        if row is not None:
            return row
        lifecycle = lifecycle_from_outcome(final_status)
        created = utc_timestamp()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO runs (
                    run_id, repository, mode, lifecycle, outcome, priority,
                    created_at, started_at, finished_at, parent_run_id,
                    evidence_path, pid
                ) VALUES (?, ?, ?, ?, ?, 0, ?, NULL, ?, NULL, ?, NULL)
                """,
                (
                    run_id,
                    repository,
                    mode,
                    lifecycle.value,
                    final_status,
                    created,
                    created,
                    evidence_path,
                ),
            )
        return self.get(run_id)

    def update_lifecycle(
        self,
        run_id: str,
        target: RunLifecycle,
        *,
        pid: int | None = None,
    ) -> None:
        """Apply a validated lifecycle transition."""
        _validate_run_id(run_id)
        with self._connect() as conn:
            current_row = conn.execute(
                "SELECT lifecycle FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if current_row is None:
                raise KeyError(f"no such run in state db: {run_id}")
            current = RunLifecycle(current_row["lifecycle"])
            try:
                assert_lifecycle_transition(current, target)
            except ValueError as exc:
                raise StateTransitionError(str(exc)) from exc
            fields = ["lifecycle = ?"]
            params: list[Any] = [target.value]
            if pid is not None:
                fields.append("pid = ?")
                params.append(pid)
            if target is RunLifecycle.RUNNING:
                fields.append("started_at = COALESCE(started_at, ?)")
                params.append(utc_timestamp())
            params.append(run_id)
            conn.execute(
                f"UPDATE runs SET {', '.join(fields)} WHERE run_id = ?",
                params,
            )

    def finish(
        self,
        run_id: str,
        *,
        lifecycle: RunLifecycle,
        outcome: str,
        finished_at: str | None = None,
    ) -> None:
        """Record a terminal-or-final execution result with its outcome."""
        _validate_run_id(run_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT lifecycle FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no such run in state db: {run_id}")
            current = RunLifecycle(row["lifecycle"])
            try:
                assert_lifecycle_transition(current, lifecycle)
            except ValueError as exc:
                raise StateTransitionError(str(exc)) from exc
            stamp = finished_at or utc_timestamp()
            conn.execute(
                """
                UPDATE runs
                   SET lifecycle = ?, outcome = ?, finished_at = ?, pid = NULL
                 WHERE run_id = ?
                """,
                (lifecycle.value, outcome, stamp, run_id),
            )

    def set_outcome(self, run_id: str, outcome: str) -> None:
        """Attach/refresh an outcome label without changing lifecycle."""
        _validate_run_id(run_id)
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET outcome = ? WHERE run_id = ?", (outcome, run_id)
            )

    # -------------------------------------------------------------- reads

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (_validate_run_id(run_id),)
            ).fetchone()
            return self._to_dict(row)

    def list_lifecycle(self, lifecycle: RunLifecycle) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE lifecycle = ? ORDER BY created_at DESC",
                (lifecycle.value,),
            ).fetchall()
            return [dict(r) for r in rows]

    def claim_next(
        self, *, pid: int | None = None, provider_epoch: str | None = None
    ) -> dict[str, Any] | None:
        """Atomically hand out the next pending task as ``running``.

        Claiming IS the pending→running transition: priority desc, then
        creation order. ``pid`` records the runner process for crash
        recovery and cancellation.
        """
        now = utc_timestamp()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM runs
                 WHERE lifecycle = ?
                 ORDER BY priority DESC, created_at ASC
                 LIMIT 1
                """,
                (RunLifecycle.PENDING.value,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            conn.execute(
                """
                UPDATE runs
                   SET lifecycle = ?,
                       pid = COALESCE(?, pid),
                       provider_epoch = COALESCE(?, provider_epoch),
                       started_at = COALESCE(started_at, ?)
                 WHERE run_id = ?
                """,
                (
                    RunLifecycle.RUNNING.value,
                    pid,
                    provider_epoch,
                    now,
                    row["run_id"],
                ),
            )
            conn.commit()
            return self.get(row["run_id"])

    # ------------------------------------------------------ crash recovery

    def set_pid(self, run_id: str, pid: int) -> None:
        """Refresh the live runner/worker pid on a running row."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET pid = ? WHERE run_id = ?", (int(pid), run_id)
            )

    def claim_specific(
        self,
        run_id: str,
        *,
        pid: int | None = None,
        provider_epoch: str | None = None,
        started_at: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically flip ONE pending row to running; ``None`` on any miss.

        The conditional UPDATE is the whole race guard: a row is either
        still pending (we win it) or it is not (a concurrent drainer or a
        cancel won first). Unlike :meth:`claim_next`, the caller names the
        exact run so a scheduler's deliberate skip can never be stolen.
        """
        _validate_run_id(run_id)
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE runs
                   SET lifecycle = ?,
                       pid = ?,
                       provider_epoch = ?,
                       started_at = COALESCE(started_at, ?)
                 WHERE run_id = ? AND lifecycle = ?
                """,
                (
                    RunLifecycle.RUNNING.value,
                    pid,
                    provider_epoch,
                    started_at or utc_timestamp(),
                    run_id,
                    RunLifecycle.PENDING.value,
                ),
            )
            if cur.rowcount != 1:
                return None
        return self.get(run_id)

    def peek_next_pending(self) -> dict[str, Any] | None:
        """Next pending row WITHOUT claiming it (pure read)."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                 WHERE lifecycle = ?
                 ORDER BY priority DESC, created_at ASC
                 LIMIT 1
                """,
                (RunLifecycle.PENDING.value,),
            ).fetchone()
            return self._to_dict(row)

    def find_interrupted(
        self, pid_alive: Callable[[int], bool]
    ) -> list[dict[str, Any]]:
        """Running rows whose stored process no longer exists.

        Callers inject ``pid_alive`` so tests stay deterministic; production
        uses :func:`_pid_alive`.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE lifecycle = ? AND pid IS NOT NULL",
                (RunLifecycle.RUNNING.value,),
            ).fetchall()
        dead = []
        for row in rows:
            try:
                if not pid_alive(int(row["pid"])):
                    dead.append(dict(row))
            except (OSError, ValueError):
                dead.append(dict(row))
        return dead

    def reconcile_interrupted(
        self, pid_alive: Callable[[int], bool], *, reason: str
    ) -> int:
        """Move interrupted running rows to blocked; returns affected count.

        Blocked is terminal by design: a crashed runner must NEVER be
        silently re-executed. Re-execution requires a new run.
        """
        moved = 0
        for row in self.find_interrupted(pid_alive):
            self.finish(row["run_id"], lifecycle=RunLifecycle.BLOCKED, outcome=reason)
            moved += 1
        return moved


def _pid_alive(pid: int) -> bool:
    """Default liveness probe; works on POSIX without external tools."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True

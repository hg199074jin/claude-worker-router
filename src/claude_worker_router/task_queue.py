"""Queue management core for V1.4 run management.

``submit`` validates a task exactly like legacy stdin mode, pre-creates its
evidence directory so ``show``/``list`` work while the task is still
pending, and registers it in the lifecycle state store. Queue-only metadata
(``priority``, ``parent_run_id``) lives in the state database only and never
enters the evidence contract.

``drain`` is deliberately boring: a strictly sequential single worker.
Concurrency is a V1.5 concern behind real-usage evidence.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from .config import RouterConfig
from .evidence import EvidenceWriter
from .executor import execute_task
from .models import RunLifecycle, RunResult, TaskRequest
from .state_store import StateStore, default_state_db_path


def open_store(config: RouterConfig) -> StateStore:
    return StateStore(default_state_db_path(config))


def read_current_fingerprint(config: RouterConfig) -> str | None:
    """Non-secret provider routing fingerprint, or ``None`` if unavailable.

    Tests patch this symbol to script epoch sequences deterministically.
    """
    from .provider import (
        ProviderConfigError,
        fingerprint_provider,
        read_provider_snapshot,
    )

    try:
        snapshot = read_provider_snapshot(config.claude_settings)
        return fingerprint_provider(snapshot)
    except ProviderConfigError:
        return None



_QUEUE_METADATA_KEYS = ("priority", "parent_run_id")


def submit_task(
    request_data: dict[str, Any], config: RouterConfig
) -> dict[str, Any]:
    """Validate, persist evidence, register pending; return submission doc."""
    from .models import TaskRequest

    priority = request_data.get("priority", 0)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise ValueError("priority must be an integer")
    parent_run_id = request_data.get("parent_run_id")
    if parent_run_id is not None:
        if not isinstance(parent_run_id, str) or not parent_run_id.isalnum():
            raise ValueError("parent_run_id must be an alphanumeric run id")

    # Validation happens against the pure task contract; queue metadata is
    # already stripped by the caller at this point when relevant. Unknown
    # provider-selection keys are rejected here exactly as in legacy mode.
    request = TaskRequest.from_dict(request_data)

    run_id = uuid.uuid4().hex
    writer = EvidenceWriter(config.run_records, run_id)
    writer.create_run(request)

    store = open_store(config)
    store.insert_pending(
        run_id=run_id,
        repository=str(request.repository),
        mode=request.mode.value,
        priority=int(priority),
        parent_run_id=parent_run_id,
        evidence_path=str(writer.run_dir),
    )
    return {"run_id": run_id, "lifecycle": RunLifecycle.PENDING.value}


def list_backlog(
    config: RouterConfig, *, state: str = "pending", limit: int | None = None
) -> list[dict[str, Any]]:
    """Rows for ``queue`` output; ``state='all'`` returns everything."""
    from .models import RunLifecycle

    store = open_store(config)
    if state == "all":
        rows: list[dict[str, Any]] = []
        for lifecycle in RunLifecycle:
            rows.extend(store.list_lifecycle(lifecycle))
        rows.sort(key=lambda r: r["created_at"], reverse=True)
    else:
        rows = store.list_lifecycle(RunLifecycle(state))
        if state == RunLifecycle.PENDING.value:
            # Present the backlog in exactly the order ``drain`` will
            # consume it: highest priority first, then submission order.
            rows.sort(key=lambda r: (-int(r["priority"]), str(r["created_at"])))
    if limit is not None and limit >= 0:
        rows = rows[:limit]
    return rows


def reconcile_before_drain(config: RouterConfig, *, out) -> int:
    """Crash recovery gate: interrupted runners become blocked terminal runs.

    Re-execution after a crash must be an explicit new run; never silent.
    """
    from .state_store import _pid_alive

    store = open_store(config)
    moved = store.reconcile_interrupted(_pid_alive, reason="runner-interrupted")
    if moved:
        print(
            f"reconciled {moved} interrupted running run(s) to blocked "
            "(runner-interrupted); re-run them explicitly with new run ids",
            file=out,
        )
    return moved


def _execute_claimed(
    row: dict[str, Any],
    config: RouterConfig,
    *,
    on_child_start: Callable[[int], None] | None = None,
) -> RunResult:
    """Rebuild the request from pre-created evidence and execute it."""
    evidence_path = Path(row["evidence_path"])
    raw = (evidence_path / "request.json").read_text(encoding="utf-8")
    request = TaskRequest.from_dict(json.loads(raw))
    return execute_task(
        request,
        config,
        on_child_start=on_child_start,
        run_id=str(row["run_id"]),
    )


def drain_once(
    config: RouterConfig,
    *,
    runner_pid: int | None = None,
    provider_epoch: str | None = None,
) -> dict[str, Any]:
    """Claim one task (or none), execute it synchronously, record outcome."""
    from .state_store import StateTransitionError

    store = open_store(config)
    claimed = store.claim_next(
        pid=os.getpid() if runner_pid is None else runner_pid,
        provider_epoch=provider_epoch,
    )
    if claimed is None:
        return {"claimed": False}

    run_id = claimed["run_id"]

    def record_worker_child(child_pid: int) -> None:
        # The session-isolated Claude process is what ``cancel`` must hit;
        # the drainer pid alone would strand its whole subtree.
        try:
            store.set_pid(run_id, child_pid)
        except Exception:  # noqa: BLE001
            pass

    result = _execute_claimed(claimed, config, on_child_start=record_worker_child)

    from .models import lifecycle_from_outcome

    final_lifecycle = lifecycle_from_outcome(result.status)
    try:
        store.finish(run_id, lifecycle=final_lifecycle, outcome=result.status)
    except StateTransitionError:
        # A concurrent explicit cancel already moved this run to a terminal
        # state; never overwrite an operator's cancellation with our result.
        step_row = store.get(run_id) or {}
        return {
            "claimed": True,
            "run_id": run_id,
            "lifecycle": step_row.get("lifecycle", RunLifecycle.CANCELLED.value),
            "outcome": step_row.get("outcome"),
            "escalation_reason": result.escalation_reason,
            "externally_finalized": True,
        }

    updated = store.get(run_id) or {}
    return {
        "claimed": True,
        "run_id": run_id,
        "lifecycle": updated.get("lifecycle", final_lifecycle.value),
        "outcome": result.status,
        "escalation_reason": result.escalation_reason,
    }


PROVIDER_STOP_EXIT_CODE = 5

import threading


def _request_scope(evidence_path: str) -> tuple[tuple[str, ...], bool]:
    """Read (allowed_paths-scope, exclusive) straight from stored evidence."""
    try:
        raw = (Path(evidence_path) / "request.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        paths = tuple(data.get("allowed_paths") or ())
        exclusive = bool(data.get("exclusive_tests", False))
        return paths, exclusive
    except (OSError, ValueError, TypeError):
        # Unreadable scope fails closed: treat as maximally conflicting,
        # never as freely parallelizable.
        return ("**unreadable**",), True


def _select_batch(
    store: StateStore, config: RouterConfig, limit: int
) -> list[dict[str, Any]]:
    """Pick the next claimable rows: priority order, conflict-free pairing.

    Conflict comparison happens inside a repository; rows from different
    repositories never conflict (V1.5 design §44). Repository identity is
    therefore prepended as the first scope component.
    """
    from .scheduler import paths_conflict

    pending = sorted(
        store.list_lifecycle(RunLifecycle.PENDING),
        key=lambda r: (-int(r["priority"]), str(r["created_at"])),
    )

    chosen: list[dict[str, Any]] = []
    chosen_scopes: list[tuple[str, ...]] = []
    batch_exclusive = False

    for row in pending:
        if len(chosen) >= limit:
            break
        raw_repo = str(row.get("repository") or "?")
        # Repository row stores whatever submit recorded; compare realpaths.
        try:
            import os as _os

            repo_key = _os.path.realpath(raw_repo)
        except OSError:
            repo_key = raw_repo

        scope, exclusive = _request_scope(str(row["evidence_path"]))
        # Compose repository+path into ONE synthetic POSIX path so the
        # pure prefix engine sees cross-repo scopes as disjoint trees.
        import hashlib as _hl

        repo_hash = _hl.sha1(repo_key.encode("utf-8")).hexdigest()[:16]
        synthetic_root = f"/__router-repos/{repo_hash}"
        full_tuple = tuple(
            str(PurePosixPath(synthetic_root) / path) for path in scope
        )
        if batch_exclusive:
            continue  # once an exclusive run is chosen, the batch is closed
        if exclusive and chosen:
            continue  # a later exclusive run may not join others either
        if any(paths_conflict(full_tuple, other) for other in chosen_scopes):
            continue
        chosen.append(row)
        chosen_scopes.append(full_tuple)
        if exclusive:
            batch_exclusive = True
    return chosen


def _claim_rows(
    store: StateStore, rows: list[dict[str, Any]], *, provider_epoch: str | None
) -> list[dict[str, Any]]:
    """Atomically flip selected rows to running with the batch epoch."""
    claimed = []
    for row in rows:
        with_store = store.get(row["run_id"])
        if with_store and with_store["lifecycle"] == RunLifecycle.PENDING.value:
            direct = store.claim_next(provider_epoch=provider_epoch)
            # claim_next may surface a different row than requested if a
            # concurrent drainer raced; loop-guard below handles reality.
            if direct and direct["run_id"] != row["run_id"]:
                continue
            if direct:
                claimed.append(direct)
        if not claimed or claimed[-1]["run_id"] != row["run_id"]:
            # fall back: the exact-row claim via state UPDATE guard
            forced = _force_claim(store, row["run_id"], provider_epoch)
            if forced:
                claimed.append(forced)
    return claimed


def _force_claim(
    store: StateStore, run_id: str, provider_epoch: str | None
) -> dict[str, Any] | None:
    import sqlite3 as _sq

    try:
        with _sq.connect(str(store.db_path)) as conn:
            cur = conn.execute(
                """
                UPDATE runs SET lifecycle='running',
                       pid=COALESCE(pid,pid),
                       started_at=COALESCE(started_at, datetime('now')),
                       provider_epoch=COALESCE(?,provider_epoch)
                 WHERE run_id=? AND lifecycle='pending'
                """,
                (provider_epoch, run_id),
            )
            if cur.rowcount != 1:
                return None
        return store.get(run_id)
    except Exception:  # noqa: BLE001
        return None


def drain(
    config: RouterConfig,
    *,
    once: bool = False,
    log=None,
) -> tuple[int, list[dict[str, Any]]]:
    """Provider-epoch guarded drain; concurrency bounded by configuration.

    Exit codes: 0 clean completion, 3 one-or-more blocked outcomes,
    5 dispatch stopped because the provider fingerprint changed.
    """
    log = log or sys.stdout
    reconcile_before_drain(config, out=log)

    provider_epoch = read_current_fingerprint(config)

    def _finish(code: int, processed: list[dict[str, Any]]):
        blocked = sum(
            1
            for step in processed
            if step.get("lifecycle") == RunLifecycle.BLOCKED.value
        )
        print(f"summary: completed {len(processed)} run(s)", file=log)
        if code == 0 and blocked:
            code = 3
        return code, processed

    processed: list[dict[str, Any]] = []

    if getattr(config, "max_concurrency", 1) == 1:
        step = drain_once(config, provider_epoch=provider_epoch)
        if not step.get("claimed"):
            return _finish(0, processed)
        while True:
            processed.append(step)
            print(
                f"completed {step['run_id']} -> {step['lifecycle']}"
                + (f" ({step['outcome']})" if step["outcome"] else ""),
                file=log,
            )
            if once:
                return _finish(0, processed)

            current = read_current_fingerprint(config)
            if current != provider_epoch:
                remaining = len(list_backlog(config, state="pending"))
                print(
                    f"dispatch stopped: provider changed since batch start "
                    f"(epoch {'set' if provider_epoch else 'none'} -> "
                    f"{'set' if current else 'none'}); "
                    f"{remaining} pending task(s) untouched; no automatic switch",
                    file=log,
                )
                return PROVIDER_STOP_EXIT_CODE, processed

            step = drain_once(config, provider_epoch=provider_epoch)
            if not step.get("claimed"):
                return _finish(0, processed)

    # ---- concurrency == 2: batched execution ---------------------------
    store = open_store(config)
    first_batch = True

    while True:
        if not first_batch:
            current = read_current_fingerprint(config)
            if current != provider_epoch:
                remaining = len(list_backlog(config, state="pending"))
                print(
                    f"dispatch stopped: provider changed since batch start "
                    f"(epoch {'set' if provider_epoch else 'none'} -> "
                    f"{'set' if current else 'none'}); "
                    f"{remaining} pending task(s) untouched; no automatic switch",
                    file=log,
                )
                return PROVIDER_STOP_EXIT_CODE, processed

        rows = (
            [store.peek_next_pending()]
            if once and not processed
            else _select_batch(store, config, getattr(config, 'max_concurrency', 1))
        )
        rows = [r for r in rows if r]
        if once:
            rows = rows[:1]
        if not rows:
            return _finish(0, processed)

        claimed_rows = _claim_rows(store, rows, provider_epoch=provider_epoch)
        first_batch = False

        steps: dict[str, dict[str, Any]] = {}

        def _run_one(row: dict[str, Any]) -> None:
            from .state_store import StateTransitionError

            try:
                result = _execute_claim_row(row, config, store)
                steps[row["run_id"]] = result
            except Exception as exc:  # noqa: BLE001 - isolate worker crash per slot
                print(f"worker thread error on {row['run_id']}: {exc!r}", file=sys.stderr)
                try:
                    store.finish(
                        row["run_id"],
                        lifecycle=RunLifecycle.BLOCKED,
                        outcome="runner-crashed",
                    )
                except StateTransitionError:
                    pass
                steps[row["run_id"]] = {
                    "claimed": True,
                    "run_id": row["run_id"],
                    "lifecycle": RunLifecycle.BLOCKED.value,
                    "outcome": "runner-crashed",
                    "escalation_reason": str(exc)[:200],
                }

        threads = [
            threading.Thread(target=_run_one, args=(row,), daemon=False)
            for row in claimed_rows
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for row in claimed_rows:
            step = steps.get(row["run_id"])
            if not step:
                continue
            processed.append(step)
            print(
                f"completed {step['run_id']} -> {step['lifecycle']}"
                + (f" ({step['outcome']})" if step["outcome"] else ""),
                file=log,
            )
        if once:
            return _finish(0, processed)


def _execute_claim_row(
    row: dict[str, Any], config: RouterConfig, store: StateStore
) -> dict[str, Any]:
    """Execute an already-claimed row; mirrors drain_once's tail."""
    from .state_store import StateTransitionError

    def record_worker_child(child_pid: int) -> None:
        try:
            store.set_pid(row["run_id"], child_pid)
        except Exception:  # noqa: BLE001
            pass

    result = _execute_claimed(row, config, on_child_start=record_worker_child)
    from .models import lifecycle_from_outcome

    final_lifecycle = lifecycle_from_outcome(result.status)
    try:
        store.finish(run_id=row["run_id"], lifecycle=final_lifecycle, outcome=result.status)
    except StateTransitionError:
        step_row = store.get(row["run_id"]) or {}
        return {
            "claimed": True,
            "run_id": row["run_id"],
            "lifecycle": step_row.get("lifecycle", RunLifecycle.CANCELLED.value),
            "outcome": step_row.get("outcome"),
            "escalation_reason": result.escalation_reason,
            "externally_finalized": True,
        }
    updated = store.get(row["run_id"]) or {}
    return {
        "claimed": True,
        "run_id": row["run_id"],
        "lifecycle": updated.get("lifecycle", final_lifecycle.value),
        "outcome": result.status,
        "escalation_reason": result.escalation_reason,
    }


class CancelRefused(RuntimeError):
    """Raised when the target run can no longer be cancelled."""


def _append_cancel_event(
    config: RouterConfig, run_id: str, *, stage: str, detail: dict[str, Any]
) -> None:
    try:
        EvidenceWriter(config.run_records, run_id).append_event(
            "cancelled", stage=stage, **detail
        )
    except OSError:
        pass


def cancel_run(run_id: str, config: RouterConfig, *, log=None) -> dict[str, Any]:
    """Cancel a queued or running task per the V1.4 cancellation contract."""
    import signal
    import time

    from .state_store import _pid_alive

    log = log or sys.stderr
    store = open_store(config)
    try:
        row = store.get(run_id)
    except ValueError as exc:
        raise CancelRefused(str(exc)) from exc
    if row is None:
        raise CancelRefused(f"no such run in queue: {run_id}")

    lifecycle = RunLifecycle(row["lifecycle"])

    if lifecycle is RunLifecycle.PENDING:
        store.finish(
            run_id,
            lifecycle=RunLifecycle.CANCELLED,
            outcome="cancelled-by-user",
        )
        _append_cancel_event(config, run_id, stage="pending", detail={})
        return {"action": "cancelled-before-start"}

    if lifecycle is RunLifecycle.RUNNING:
        pid = row.get("pid")
        used_group_kill = False
        if isinstance(pid, int) and pid > 0:
            if _pid_alive(pid):
                own_pgid = os.getpgrp()
                try:
                    pgid = os.getpgid(pid)
                except OSError:
                    pgid = None
                try:
                    if pgid and pgid != own_pgid:
                        # Session-isolated runner: the whole worker subtree
                        # can die without touching the operator's shell.
                        os.killpg(pgid, signal.SIGTERM)
                        used_group_kill = True
                    else:
                        os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                deadline = time.time() + 5.0
                while _pid_alive(pid) and time.time() < deadline:
                    time.sleep(0.05)
                if used_group_kill and _pid_alive(pid):
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
            else:
                print(f"note: recorded runner pid {pid} no longer exists", file=log)
        store.finish(
            run_id,
            lifecycle=RunLifecycle.CANCELLED,
            outcome="cancelled-by-user",
        )
        _append_cancel_event(
            config,
            run_id,
            stage="running",
            detail={"group_kill": used_group_kill},
        )
        return {
            "action": "terminated-running",
            "process_group_terminated": used_group_kill,
        }

    if lifecycle is RunLifecycle.READY_FOR_REVIEW:
        # Cancelling a finished result records an explicit discard intent;
        # the isolated worktree stays until cleanup decides otherwise.
        store.finish(
            run_id,
            lifecycle=RunLifecycle.CANCELLED,
            outcome="cancelled-by-user",
        )
        _append_cancel_event(
            config,
            run_id,
            stage="ready-for-review",
            detail={"worktree_preserved": True},
        )
        return {"action": "discard-intent-recorded"}

    raise CancelRefused(
        f"cannot cancel run in terminal state '{lifecycle.value}'"
    )


def execute_single(config: RouterConfig) -> int:
    """One claimed execution inside an isolated process session.

    Exit codes: 0 finished acceptable, 3 blocked, 4 nothing claimable.
    """
    step = drain_once(config, runner_pid=os.getpid())
    if not step.get("claimed"):
        return 4
    if step.get("lifecycle") == RunLifecycle.BLOCKED.value:
        return 3
    return 0


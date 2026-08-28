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


def _request_scope(
    evidence_path: str, config: RouterConfig | None = None
) -> tuple[tuple[str, ...], bool]:
    """Read (allowed_paths-scope, exclusive) straight from stored evidence."""
    try:
        raw = (Path(evidence_path) / "request.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        paths = tuple(data.get("allowed_paths") or ())
        exclusive = bool(data.get("exclusive_tests", False))
        profile_name = data.get("test_profile")
        if not exclusive and profile_name and config is not None:
            profile = config.test_profiles.get(str(profile_name))
            if profile is not None and profile.exclusive:
                exclusive = True
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

        scope, exclusive = _request_scope(
            str(row["evidence_path"]), config
        )
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
    store: StateStore,
    rows: list[dict[str, Any]],
    *,
    provider_epoch: str | None,
    runner_pid: int | None,
) -> list[dict[str, Any]]:
    """Flip exactly the SELECTED rows to running via per-row atomic claim.

    ``claim_next`` is deliberately not used here: it always returns the
    globally highest-priority pending row, so it would steal rows the
    scheduler skipped (e.g. path-conflicting mid-priority tasks) and leave
    them stranded in ``running`` with no executor. Each
    ``claim_specific`` is a single conditional UPDATE -- atomic on its
    own -- so a row is either won by this batch or left untouched for a
    later batch or another drainer.
    """
    if not rows:
        return []
    from .evidence import utc_timestamp as _utc_now

    started_at = _utc_now()  # one stamp per batch for auditability
    claimed: list[dict[str, Any]] = []
    for row in rows:
        won = store.claim_specific(
            row["run_id"],
            pid=runner_pid,
            provider_epoch=provider_epoch,
            started_at=started_at,
        )
        if won is not None:
            claimed.append(won)
    return claimed


def _join_budget(config: RouterConfig, *, n_test_commands: int) -> float:
    """Seconds a batch thread may legitimately need before we call it wedged.

    A single run performs up to ``correction_limit + 1`` worker attempts,
    each individually bounded by ``timeout_seconds`` and followed by one
    test phase per command (also ``timeout_seconds`` each). The old fixed
    ``timeout_seconds + 30`` budget covered exactly ONE attempt, so any
    legitimately correcting run tripped a false wedge diagnosis.
    """
    attempts = max(1, config.correction_limit + 1)
    phases = 1 + max(1, n_test_commands)
    return attempts * phases * float(config.timeout_seconds) + 60.0


def _row_test_command_count(row: dict[str, Any]) -> int:
    """Best-effort count of a queued row's test commands (>=1)."""
    try:
        raw = (Path(row["evidence_path"]) / "request.json").read_text(
            encoding="utf-8"
        )
        data = json.loads(raw)
        commands = data.get("test_commands") or []
        return max(1, len(commands))
    except (OSError, ValueError, TypeError):
        return 1


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

        claimed_rows = _claim_rows(
            store,
            rows,
            provider_epoch=provider_epoch,
            runner_pid=os.getpid(),
        )
        first_batch = False
        if not claimed_rows:
            # Selected rows existed but every claim lost the race (another
            # drainer or a cancel won them). Re-selecting would spin on the
            # same losers forever, so stop cleanly instead.
            print(
                "dispatch stopped: batch selection made no progress "
                "(rows claimed by someone else); no pending work for this drainer",
                file=log,
            )
            return _finish(0, processed)

        import traceback as _traceback
        steps: dict[str, dict[str, Any]] = {}
        thread_errors: dict[str, str] = {}

        def _run_one(row: dict[str, Any]) -> None:
            from .state_store import StateTransitionError

            try:
                result = _execute_claim_row(row, config, store)
                steps[row["run_id"]] = result
            except Exception as exc:  # noqa: BLE001 - isolate worker crash per slot
                # Buffer the diagnostic for the main thread to print, so
                # threads never touch ``sys.stderr`` directly (StringIO
                # patches and concurrent writes are both unsafe).
                thread_errors[row["run_id"]] = "".join(
                    _traceback.format_exception(type(exc), exc, exc.__traceback__)
                )[:400]
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

        # Daemon threads: a genuinely wedged worker must not hold the CLI
        # hostage at interpreter exit; the stranded ``running`` row is
        # exactly what crash recovery (reconcile_before_drain) owns.
        threads = [
            threading.Thread(target=_run_one, args=(row,), daemon=True)
            for row in claimed_rows
        ]
        for t in threads:
            t.start()
        budget = max(
            _join_budget(
                config, n_test_commands=_row_test_command_count(row)
            )
            for row in claimed_rows
        )
        wedged = False
        for t in threads:
            t.join(timeout=budget)
            if t.is_alive():
                wedged = True
        if wedged:
            # A worker thread outlived its full legitimate budget. Mark the
            # affected rows blocked and STOP draining entirely -- claiming
            # further batches would exceed max_concurrency with live
            # stragglers.
            from .state_store import StateTransitionError

            for row in claimed_rows:
                if row["run_id"] not in steps:
                    thread_errors[row["run_id"]] = (
                        f"worker thread did not finish within {budget:.0f}s"
                    )
                    try:
                        store.finish(
                            row["run_id"],
                            lifecycle=RunLifecycle.BLOCKED,
                            outcome="runner-wedged",
                        )
                    except StateTransitionError:
                        pass
                    steps[row["run_id"]] = {
                        "claimed": True,
                        "run_id": row["run_id"],
                        "lifecycle": RunLifecycle.BLOCKED.value,
                        "outcome": "runner-wedged",
                        "escalation_reason": "runner-wedged",
                    }
                    processed.append(steps[row["run_id"]])
            print(
                f"dispatch stopped: worker thread exceeded {budget:.0f}s "
                "legitimate budget; marked runner-wedged and stopped",
                file=log,
            )
            return _finish(3, processed)

        if thread_errors:
            for run_id, detail in thread_errors.items():
                print(
                    f"worker thread error on {run_id}: {detail}",
                    file=sys.stderr,
                    flush=True,
                )

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
        writer = EvidenceWriter(config.run_records, run_id)
        writer.append_event("cancelled", stage=stage, **detail)
        # events.jsonl grew after the run finalized its manifest; without
        # re-finalizing, every later integrity check would report drift.
        writer.finalize_manifest()
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


def mark_integrated_sync(
    run_id: str, config: RouterConfig, *, log=None
) -> bool:
    """Best-effort lifecycle sync after a successful integrate command.

    Runs that never went through ``submit`` (legacy stdin) get tracked
    lazily here so *integrate 后状态准确* holds across entry points.
    Failures never undo the integration itself; they surface as stderr
    notes only.
    """
    from .run_store import RunStore

    log = log or sys.stderr
    try:
        record = RunStore(config.run_records).load_run(run_id)
        metadata = record.get("metadata") or {}
        request = record.get("request") or {}
        store = open_store(config)
        row = store.ensure_row(
            run_id=run_id,
            repository=str(metadata.get("repository") or "?"),
            mode=str(request.get("mode") or "edit"),
            final_status="ready-for-review",
            evidence_path=str(record.get("run_dir", "")),
        )
        if row["lifecycle"] == RunLifecycle.INTEGRATED.value:
            return True
        store.update_lifecycle(run_id, RunLifecycle.INTEGRATED)
        return True
    except Exception as exc:  # noqa: BLE001 - sync must not break integration UX
        print(f"warning: state sync skipped: {exc}", file=log)
        return False


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


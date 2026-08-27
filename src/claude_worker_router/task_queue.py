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
from pathlib import Path
from typing import Any

from .config import RouterConfig
from .evidence import EvidenceWriter
from .executor import execute_task
from .models import RunLifecycle, RunResult, TaskRequest
from .state_store import StateStore


def default_state_db_path(config: RouterConfig) -> Path:
    """The state db lives next to the configured run records root."""
    return Path(config.run_records).parent / "state.db"


def open_store(config: RouterConfig) -> StateStore:
    return StateStore(default_state_db_path(config))


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


def _execute_claimed(row: dict[str, Any], config: RouterConfig) -> RunResult:
    """Rebuild the request from pre-created evidence and execute it."""
    evidence_path = Path(row["evidence_path"])
    raw = (evidence_path / "request.json").read_text(encoding="utf-8")
    request = TaskRequest.from_dict(json.loads(raw))
    return execute_task(request, config)


def drain_once(config: RouterConfig, *, runner_pid: int | None = None) -> dict[str, Any]:
    """Claim one task (or none), execute it synchronously, record outcome."""
    store = open_store(config)
    claimed = store.claim_next(pid=os.getpid() if runner_pid is None else runner_pid)
    if claimed is None:
        return {"claimed": False}

    result = _execute_claimed(claimed, config)
    from .models import lifecycle_from_outcome

    final_lifecycle = lifecycle_from_outcome(result.status)
    store.finish(
        claimed["run_id"],
        lifecycle=final_lifecycle,
        outcome=result.status,
    )
    updated = store.get(claimed["run_id"]) or {}
    return {
        "claimed": True,
        "run_id": claimed["run_id"],
        "lifecycle": updated.get("lifecycle", final_lifecycle.value),
        "outcome": result.status,
        "escalation_reason": result.escalation_reason,
    }


def drain(
    config: RouterConfig,
    *,
    once: bool = False,
    log=None,
) -> tuple[int, list[dict[str, Any]]]:
    """Sequential single-worker drain. Exit code 3 signals blocked results."""
    log = log or sys.stdout
    reconcile_before_drain(config, out=log)

    processed: list[dict[str, Any]] = []
    while True:
        step = drain_once(config)
        if not step.get("claimed"):
            break
        processed.append(step)
        print(
            f"completed {step['run_id']} -> {step['lifecycle']}"
            + (f" ({step['outcome']})" if step["outcome"] else ""),
            file=log,
        )
        if once:
            break

    blocked = sum(
        1 for step in processed if step["lifecycle"] == RunLifecycle.BLOCKED.value
    )
    print(f"summary: completed {len(processed)} run(s)", file=log)
    return (3 if blocked else 0), processed


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

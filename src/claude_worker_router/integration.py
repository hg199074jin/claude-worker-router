"""Explicit, verified integration of reviewed worker runs.

``integrate_run`` is the only sanctioned path from ``ready-for-review``
to a change on the main checkout. Its preflight order is fixed (see the
module-level sequence); the merge itself is exclusively
``git merge --ff-only <worker_commit>`` -- no rebase, no force, no
merge-commit fabrication, no conflict auto-resolution. Base divergence is
a refusal, never an automatic rewrite.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import RouterConfig
from .evidence import EvidenceWriter, sha256_file, utc_timestamp
from .run_store import RunNotFoundError, RunStore, validate_run_id


class IntegrationError(RuntimeError):
    """Structured integration refusal with a machine-readable ``reason``."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def integrate_run(run_id: str, config: RouterConfig) -> str:
    """Verify a run end-to-end and fast-forward main to its worker commit.

    Returns the merged commit SHA on success; raises :class:`IntegrationError`
    otherwise. On failure the repository is untouched.
    """
    try:
        validate_run_id(run_id)
        record = RunStore(config.run_records).load_run(run_id)
    except (ValueError, RunNotFoundError) as exc:
        raise IntegrationError("integration-run-unavailable", str(exc)) from exc

    metadata = record["metadata"]
    result = record["result"]
    request = record["request"]

    # 1-2: mode must be edit.
    if request.get("mode") != "edit":
        raise IntegrationError(
            "integration-mode-invalid", "only edit-mode runs are integratable"
        )

    # 3: status gate.
    final_status = metadata.get("final_status") or result.get("status")
    if final_status != "ready-for-review":
        raise IntegrationError(
            "integration-status-invalid",
            f"final status {final_status!r} is not ready-for-review",
        )

    # 4: worker commit presence.
    worker_commit = result.get("commit") or metadata.get("worker_commit")
    if not isinstance(worker_commit, str) or not worker_commit:
        raise IntegrationError(
            "integration-worker-commit-missing",
            "run completed without a worker commit",
        )

    repository_raw = metadata.get("repository")
    if not isinstance(repository_raw, str) or not repository_raw:
        raise IntegrationError(
            "integration-repository-missing", "metadata has no repository"
        )
    repository = Path(repository_raw)

    # 5: identity consistency between evidence and disk.
    stored_realpath = metadata.get("repository_realpath") or ""
    if (
        not repository.exists()
        or os.path.realpath(repository) != str(stored_realpath)
    ):
        raise IntegrationError(
            "integration-repository-diverged",
            f"recorded repository realpath {stored_realpath!r} does not match "
            f"current location",
        )

    # 6: clean main checkout.
    porcelain = _git_capture(
        repository,
        ["status", "--porcelain"],
        error_reason="git-status-failed",
    )
    if porcelain.strip():
        raise IntegrationError(
            "integration-dirty-checkout",
            f"{repository} has uncommitted changes; refusing integration",
        )

    # 7: base drift check.
    base_sha = metadata.get("base_sha")
    current_head = _git_capture(
        repository, ["rev-parse", "HEAD"], error_reason="git-head-failed"
    )
    if not isinstance(base_sha, str) or not base_sha:
        raise IntegrationError(
            "integration-base-sha-missing",
            "run recorded no base SHA; refuse rather than guess",
        )
    if current_head != base_sha:
        raise IntegrationError(
            "integration-base-diverged",
            f"current HEAD {current_head[:12]} != recorded base {base_sha[:12]}",
        )

    # 8: worker commit belongs to the worker branch.
    worker_branch = metadata.get("worker_branch")
    if (
        not isinstance(worker_branch, str)
        or not worker_branch
        or subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "merge-base",
                "--is-ancestor",
                worker_commit,
                worker_branch,
            ],
            shell=False,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    ):
        raise IntegrationError(
            "integration-branch-missing",
            f"commit {worker_commit[:12]} is no longer reachable from "
            f"branch {worker_branch!r}",
        )

    # 9: evidence manifest integrity.
    _verify_manifest(record, run_id, config)

    # 10: all tests passed and were executed at all.
    tests = result.get("tests")
    if not isinstance(tests, list) or not tests or any(
        t.get("exit_code") != 0 for t in tests if isinstance(t, dict)
    ):
        raise IntegrationError(
            "integration-tests-failed",
            "preflight requires every executor-run test to have passed",
        )

    # 11: no residual escalation facts.
    if result.get("escalation_reason"):
        raise IntegrationError(
            "integration-status-invalid",
            f"result still carries escalation reason "
            f"{result['escalation_reason']!r}",
        )

    from .scheduler import RepositoryBusy, repository_integration_lock

    try:
        lock = repository_integration_lock(
            Path(config.run_records).parent / "locks", repository
        )
        lock.__enter__()
    except RepositoryBusy as exc:
        raise IntegrationError("integration-lock-busy", str(exc)) from exc

    writer = EvidenceWriter(config.run_records, run_id)
    writer.append_event(
        "integration-started",
        worker_commit=worker_commit,
        base_sha=base_sha,
    )

    proc = subprocess.run(
        ["git", "-C", str(repository), "merge", "--ff-only", worker_commit],
        shell=False,
        check=False,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown git error"
        lock.__exit__(None, None, None)
        raise IntegrationError("integration-merge-failed", detail)

    merged_head = _git_capture(
        repository, ["rev-parse", "HEAD"], error_reason="git-head-failed"
    )

    metadata_path = writer.run_dir / "metadata.json"
    metadata_from_disk = json_loads_safe(metadata_path)
    metadata_from_disk["integrated_at"] = utc_timestamp()
    metadata_from_disk["integrated_sha"] = merged_head
    writer.write_metadata(metadata_from_disk)
    writer.append_event("integration-completed", integrated_sha=merged_head)
    writer.finalize_manifest()

    lock.__exit__(None, None, None)
    return merged_head


def _verify_manifest(record: dict, run_id: str, config: RouterConfig) -> None:
    manifest_data = json_loads_safe(
        EvidenceWriter(config.run_records, run_id).run_dir
        / "evidence_manifest.json"
    )
    if not isinstance(manifest_data, dict):
        raise IntegrationError(
            "integration-evidence-mismatch", "manifest is not an object"
        )
    for name, digest in manifest_data.items():
        path = EvidenceWriter(config.run_records, run_id).run_dir / name
        if not path.is_file():
            raise IntegrationError(
                "integration-evidence-mismatch", f"missing evidence file {name}"
            )
        if sha256_file(path) != digest:
            raise IntegrationError(
                "integration-evidence-mismatch",
                f"digest mismatch for {name}; evidence was altered",
            )


def json_loads_safe(path: Path) -> dict:
    import json

    return json.loads(Path(path).read_text(encoding="utf-8"))


def _git_capture(repository: Path, args: list[str], *, error_reason: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repository), *args],
        shell=False,
        check=False,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise IntegrationError(error_reason, proc.stderr.strip() or "git failed")
    return proc.stdout.strip()

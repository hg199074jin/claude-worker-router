"""Worktree lifecycle management and stale-run reporting.

Rules encoded here:

* Evidence is *never* deleted -- cleanup touches only the isolation
  artifacts (worktree directory and temporary branch).
* A run that still holds unintegrated worker modifications is refused
  unless ``discard=True``, which marks an explicit user decision.
* Staleness is a *classification*, not a permission: ``--stale`` reports,
  the operator acts run by run.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import RouterConfig
from .evidence import EvidenceWriter, parse_utc_timestamp, sha256_file, utc_timestamp
from .run_store import RunStore, validate_run_id


class CleanupError(RuntimeError):
    """Raised when a cleanup request cannot be carried out at all."""


class CleanupRefused(RuntimeError):
    """Raised when policy forbids deleting this run's isolation artifacts."""


STALE_REPORT_HINT = "review with `show`/`integrate`, then rerun with --discard"


@dataclass(frozen=True)
class CleanupOutcome:
    run_id: str
    removed_worktree: bool
    removed_branch: bool
    already_cleaned: bool = False
    discarded: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class StaleEntry:
    run_id: str
    detail: str


@dataclass(frozen=True)
class StaleReport:
    auto_candidates: list[StaleEntry]
    report_only: list[StaleEntry]


def cleanup_run(
    run_id: str, config: RouterConfig, *, discard: bool = False
) -> CleanupOutcome:
    """Remove one run's worktree and branch; evidence always survives."""
    try:
        validate_run_id(run_id)
    except ValueError as exc:
        raise CleanupError(str(exc)) from exc

    store = RunStore(config.run_records)
    try:
        record = store.load_run(run_id)
    except Exception as exc:
        raise CleanupError(f"cannot load run {run_id}: {exc}") from exc

    metadata = record.get("metadata") or {}
    worktree_raw = metadata.get("worktree")
    repository_raw = metadata.get("repository")
    if not isinstance(worktree_raw, str) or not isinstance(repository_raw, str):
        raise CleanupError(f"run {run_id} lacks location metadata")

    repository_root = Path(os.path.realpath(repository_raw))
    # GitWorkspace places worktrees next to the repository; also accept an
    # in-repo location for hand-migrated setups.
    allowed_roots = (
        repository_root / ".codex-worktrees",
        repository_root.parent / ".codex-worktrees",
    )
    worktree_path = Path(os.path.realpath(worktree_raw))
    if not any(worktree_path.is_relative_to(root) for root in allowed_roots):
        raise CleanupError(
            f"refusing to remove {worktree_path}: it is not inside a router "
            f"worktree area ({', '.join(str(r) for r in allowed_roots)})"
        )

    notes: list[str] = []
    if Path(os.path.realpath(worktree_raw)) == repository_root:
        # Read-only runs operate in place and have no isolation artifacts.
        return CleanupOutcome(
            run_id=run_id,
            removed_worktree=False,
            removed_branch=False,
            already_cleaned=True,
            discarded=discard,
        )

    worktree_present = worktree_path.is_dir()
    if not worktree_present and not _branch_exists(repository_root, metadata):
        return CleanupOutcome(
            run_id=run_id,
            removed_worktree=False,
            removed_branch=False,
            already_cleaned=True,
            discarded=discard,
        )

    uncommitted_dirty = (
        _worktree_has_local_changes(worktree_path) if worktree_present else False
    )
    worker_commit = None
    raw_result = record.get("result") or {}
    candidate = raw_result.get("commit") or metadata.get("worker_commit")
    if isinstance(candidate, str) and candidate:
        worker_commit = candidate
    unintegrated_commit = bool(worker_commit) and not bool(
        metadata.get("integrated_sha")
    )
    pending = uncommitted_dirty or unintegrated_commit
    if pending and not discard:
        raise CleanupRefused(
            f"run {run_id} still holds an unintegrated worker change "
            f"(commit={bool(worker_commit)}, dirty_worktree="
            f"{uncommitted_dirty}); pass --discard to abandon it explicitly "
            f"({STALE_REPORT_HINT})"
        )

    removed_worktree = False
    removed_branch = False
    if worktree_present:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "worktree",
                "remove",
                "--force",
                str(worktree_path),
            ],
            shell=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            removed_worktree = True
        else:
            notes.append(
                f"worktree removal failed: {proc.stderr.strip() or 'git error'}"
            )

    branch = metadata.get("worker_branch")
    if isinstance(branch, str) and _branch_exists(repository_root, {"worker_branch": branch}):
        proc = subprocess.run(
            ["git", "-C", str(repository_root), "branch", "-D", branch],
            shell=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            removed_branch = True
        else:
            notes.append(f"branch deletion failed: {proc.stderr.strip()}")

    _record_cleanup(run_id, config, discarded=discard, notes=notes)

    return CleanupOutcome(
        run_id=run_id,
        removed_worktree=removed_worktree,
        removed_branch=removed_branch,
        already_cleaned=not removed_worktree and not removed_branch,
        discarded=discard,
        notes=tuple(notes),
    )


def classify_stale_run(metadata: dict, *, cutoff) -> str:
    """Verdict one run against a cutoff datetime: ``auto``/``report``/``keep``."""
    finished_at = metadata.get("finished_at")
    if not isinstance(finished_at, str) or not finished_at:
        return "keep"
    try:
        finished = parse_utc_timestamp(finished_at)
    except ValueError:
        return "keep"
    if finished >= cutoff:
        return "keep"

    if metadata.get("integrated_sha"):
        return "auto"

    status = metadata.get("final_status")
    has_changes = bool(metadata.get("changed_files"))
    if status == "escalated" and not has_changes:
        return "auto"
    if status == "ready-for-review":
        return "report"
    return "report"


def find_stale_runs(config: RouterConfig, *, cutoff) -> StaleReport:
    """Classify every recorded run whose finish time precedes ``cutoff``."""
    store = RunStore(config.run_records)
    listing = store.list_runs()
    auto: list[StaleEntry] = []
    report: list[StaleEntry] = []

    for row in listing.rows:
        run_id = str(row["run_id"])
        verdict_reasons: list[str] = []
        try:
            record = store.load_run(run_id)
        except Exception as exc:
            report.append(
                StaleEntry(run_id, f"evidence unreadable ({exc}); manual review")
            )
            continue

        verdict = classify_stale_run(record.get("metadata") or {}, cutoff=cutoff)
        if verdict == "keep":
            continue

        if not _manifest_verifies(store, run_id):
            report.append(
                StaleEntry(
                    run_id,
                    "evidence manifest mismatch; do not trust this run, review manually",
                )
            )
            continue

        detail = f"finished_at={(row.get('created_at') or '?')}"
        if verdict == "auto":
            auto.append(StaleEntry(run_id, detail))
        else:
            report.append(StaleEntry(run_id, f"{detail}; requires decision"))

    return StaleReport(auto_candidates=auto, report_only=report)


# ------------------------------------------------------------- internals


def _branch_exists(repository_root: Path, metadata: dict) -> bool:
    branch = metadata.get("worker_branch")
    if not isinstance(branch, str) or not branch:
        return False
    return (
        subprocess.run(
            ["git", "-C", str(repository_root), "rev-parse", "--verify", branch],
            shell=False,
            capture_output=True,
        ).returncode
        == 0
    )


def _worktree_has_local_changes(worktree: Path) -> bool:
    """Authoritative pending-change probe; unknown states count as changes."""
    def _output(*args: str) -> str | None:
        proc = subprocess.run(
            ["git", "-C", str(worktree), *args],
            shell=False,
            capture_output=True,
            text=True,
        )
        return proc.stdout if proc.returncode == 0 else None

    tracked = _output("diff", "--name-only", "HEAD")
    untracked = _output("ls-files", "--others", "--exclude-standard")
    if tracked is None and untracked is None:
        return True
    has_tracked = bool(tracked and tracked.strip())
    has_untracked = bool(untracked and untracked.strip())
    return has_tracked or has_untracked


def _manifest_verifies(store: RunStore, run_id: str) -> bool:
    writer_dir = store.records_root / run_id
    manifest_path = writer_dir / "evidence_manifest.json"
    if not manifest_path.is_file():
        return False
    import json

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, dict):
        return False
    for name, digest in manifest.items():
        path = writer_dir / name
        if not path.is_file() or sha256_file(path) != digest:
            return False
    return True


def _record_cleanup(
    run_id: str, config: RouterConfig, *, discarded: bool, notes: list[str]
) -> None:
    """Best-effort audit trail updates; filesystem removal already happened."""
    writer = EvidenceWriter(config.run_records, run_id)
    meta_path = writer.run_dir / "metadata.json"
    try:
        import json

        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        metadata["worktree_removed_at"] = utc_timestamp()
        if discarded:
            metadata["discarded_without_integration"] = True
        writer.write_metadata(metadata)
        event = "cleanup-discarded" if discarded else "cleanup-completed"
        writer.append_event(event, **{"notes": list(notes)})
        writer.finalize_manifest()
    except (OSError, ValueError) as exc:
        notes.append(f"post-cleanup evidence update failed: {exc}")

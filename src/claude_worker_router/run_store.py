"""Read-only access to the run-records tree (``list`` / ``show`` backend).

Run identifiers arrive from the command line, so they are untrusted input.
:meth:`RunStore.validate_run_id` rejects anything that is not a single
safe path segment *before* the filesystem is touched; every lookup then
resolves strictly inside the configured ``run_records`` root.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RunNotFoundError(ValueError):
    """Raised when a validated run id has no evidence directory."""


@dataclass(frozen=True)
class RunListing:
    rows: list[dict[str, Any]]
    warnings: list[str]


def validate_run_id(run_id: str) -> str:
    """Reject run ids that could escape the records root."""
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run id must be a non-empty string")
    if run_id in (".", ".."):
        raise ValueError(f"unsafe run id: {run_id!r}")
    if any(sep in run_id for sep in ("/", "\\", "\0")):
        raise ValueError(f"unsafe run id: {run_id!r}")
    if Path(run_id).name != run_id:
        raise ValueError(f"unsafe run id: {run_id!r}")
    return run_id


class RunStore:
    """Query the flat per-run directories under ``run_records``."""

    def __init__(self, records_root: Path) -> None:
        self.records_root = Path(records_root)

    # ------------------------------------------------------------- queries

    def list_runs(
        self,
        *,
        repository: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> RunListing:
        """Return summary rows sorted by ``created_at`` descending."""
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []

        if not self.records_root.is_dir():
            return RunListing(rows=[], warnings=[])

        for entry in sorted(self.records_root.iterdir()):
            if not entry.is_dir():
                continue
            try:
                row = self._summarize(entry)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                warnings.append(f"skipping malformed run {entry.name}: {exc}")
                continue
            if repository is not None and row.get("repository") != repository:
                continue
            if status is not None and row.get("status") != status:
                continue
            rows.append(row)

        rows.sort(key=lambda row: (row.get("created_at") or "", row["run_id"]), reverse=True)
        if limit is not None and limit >= 0:
            rows = rows[:limit]
        return RunListing(rows=rows, warnings=warnings)

    def run_exists(self, run_id: str) -> bool:
        validated = validate_run_id(run_id)
        candidate = self.records_root / validated
        # Defense in depth: even a hostile symlinked entry must stay inside.
        return (
            candidate.is_dir()
            and os.path.realpath(candidate).startswith(
                os.path.realpath(self.records_root) + os.sep
            )
        )

    def load_run(self, run_id: str) -> dict[str, Any]:
        """Return ``{"metadata", "request", "result"}``; missing → error."""
        if not self.run_exists(run_id):
            raise RunNotFoundError(f"no such run: {run_id}")

        run_dir = self.records_root / run_id
        record: dict[str, Any] = {
            "metadata": {},
            "request": {},
            "result": {},
        }
        for part in record:
            path = run_dir / f"{part}.json"
            if path.exists():
                try:
                    record[part] = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"evidence file {path.name} is corrupt: {exc}"
                    ) from exc
                if not isinstance(record[part], dict):
                    raise ValueError(f"evidence file {path.name} must be an object")
        record["run_dir"] = str(run_dir)
        return record

    # ------------------------------------------------------------ internal

    def _summarize(self, run_dir: Path) -> dict[str, Any]:
        metadata = self._read_object(run_dir, "metadata.json")
        result = self._read_object(run_dir, "result.json", required=False)

        provider = metadata.get("provider") or {}
        endpoint_host = ""
        model = ""
        if isinstance(provider, dict):
            endpoint_host = str(provider.get("endpoint_host") or "")
            model = str(provider.get("model") or "")
        else:
            raise TypeError("metadata.provider must be an object")

        status = metadata.get("final_status")
        if status is None:
            status = result.get("status")

        return {
            "run_id": metadata.get("run_id") or run_dir.name,
            "created_at": metadata.get("created_at"),
            "repository": metadata.get("repository"),
            "mode": metadata.get("mode"),
            "provider": f"{endpoint_host}/{model}".strip("/") or "-",
            "status": status,
            "changed_files": len(metadata.get("changed_files") or []),
            "diff_lines": metadata.get("diff_lines") or 0,
            "escalation_reason": (
                metadata.get("escalation_reason")
                if metadata.get("escalation_reason") is not None
                else result.get("escalation_reason")
            ),
            "integrated_at": metadata.get("integrated_at"),
            "integrated_sha": metadata.get("integrated_sha"),
        }

    @staticmethod
    def _read_object(run_dir: Path, name: str, *, required: bool = True) -> dict[str, Any]:
        path = run_dir / name
        if not path.exists():
            if required:
                raise FileNotFoundError(f"{name} missing in {run_dir.name}")
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError(f"{name} must be a JSON object")
        return data

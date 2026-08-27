"""Structured, hash-manifested run evidence storage.

Every run directory contains ``request.json``, ``result.json``,
``metadata.json``, ``tests.json``, an optional ``diff.patch``, an
append-only ``events.jsonl`` timeline, and a final ``evidence_manifest.json``
holding the SHA-256 digest of every other evidence file. The manifest is
always written last so a complete directory can be verified for accidental
truncation or partial overwrite. This provides tamper *detection*, not
non-repudiation.

Timestamps are UTC ISO-8601 strings ending in ``Z``. No credential value is
ever accepted by this API: callers persist redacted metadata only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_NAME = "evidence_manifest.json"


def utc_timestamp() -> str:
    """Return the current time as a UTC ISO-8601 timestamp ending in ``Z``."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_utc_timestamp(stamp: str) -> datetime:
    """Parse a timestamp produced by :func:`utc_timestamp`."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 hex digest of a file's bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    """Write JSON atomically: temp file first, then an atomic rename."""
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)


class EvidenceWriter:
    """Owns one run's evidence directory under the run-records root."""

    def __init__(self, records_root: Path, run_id: str) -> None:
        self.records_root = Path(records_root)
        self.run_id = run_id
        self.run_dir = self.records_root / run_id

    def create_run(self, request: Any) -> None:
        """Create the run directory, persist the request, open the timeline."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.run_dir / "request.json", request.to_dict())
        self.append_event("run-created", mode=request.mode.value)

    def append_event(self, event: str, **fields: Any) -> None:
        """Append one observable execution fact to ``events.jsonl``."""
        payload = {"event": event, "timestamp": utc_timestamp(), **fields}
        with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")

    def write_metadata(self, metadata: dict[str, Any]) -> None:
        atomic_write_json(self.run_dir / "metadata.json", metadata)

    def write_tests(self, tests: list[dict[str, Any]]) -> None:
        atomic_write_json(self.run_dir / "tests.json", tests)

    def write_result(self, result: dict[str, Any]) -> None:
        atomic_write_json(self.run_dir / "result.json", result)

    def write_diff(self, patch_text: str) -> None:
        """Persist the final textual patch; may be zero-length when clean."""
        (self.run_dir / "diff.patch").write_text(patch_text, encoding="utf-8")

    def finalize_manifest(self) -> dict[str, str]:
        """Hash every evidence file present and write the manifest last."""
        manifest: dict[str, str] = {}
        for entry in sorted(self.run_dir.iterdir()):
            if not entry.is_file() or entry.name == MANIFEST_NAME:
                continue
            manifest[entry.name] = sha256_file(entry)
        atomic_write_json(self.run_dir / MANIFEST_NAME, manifest)
        return manifest

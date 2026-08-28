"""Tiny shared probe: does a run's evidence manifest verify on disk?"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def manifest_ok(records_root: Path, run_id: str) -> bool:
    run_dir = Path(records_root) / run_id
    manifest_path = run_dir / "evidence_manifest.json"
    if not manifest_path.is_file():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, digest in manifest.items():
        path = run_dir / name
        if not path.is_file():
            return False
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            return False
    return True

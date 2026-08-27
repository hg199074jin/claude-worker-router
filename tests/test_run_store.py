"""Run history inspection tests (V1.2 Task 5).

``RunStore`` reads the run-records tree produced by the executor and backs
the ``list`` / ``show`` CLI commands. Run identifiers are untrusted input:
any value that could traverse outside ``run_records`` must be rejected
before the filesystem is touched.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from claude_worker_router.run_store import RunNotFoundError, RunStore


class RunStoreFactory(unittest.TestCase):
    """Shared fixture: seed synthetic but schema-faithful run directories."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="run-store-"))
        self.addCleanup(_cleanup_tree, self.tmp)
        self.root = self.tmp / "runs"
        self.store = RunStore(self.root)

    def _write_run(
        self,
        run_id: str,
        *,
        created_at: str,
        repository: str = "/repo/one",
        status: str = "ready-for-review",
        mode: str = "edit",
        include_metadata: bool = True,
        include_result: bool = True,
        result_overrides: dict | None = None,
    ) -> Path:
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        request = {
            "repository": repository,
            "task": f"task {run_id}",
            "acceptance_criteria": [],
            "mode": mode,
            "test_commands": [],
            "allowed_paths": [],
        }
        (run_dir / "request.json").write_text(
            json.dumps(request), encoding="utf-8"
        )
        if include_result:
            result = {
                "run_id": run_id,
                "status": status,
                "escalation_reason": None,
            }
            result.update(result_overrides or {})
            (run_dir / "result.json").write_text(
                json.dumps(result), encoding="utf-8"
            )
        if include_metadata:
            metadata = {
                "schema_version": 1,
                "run_id": run_id,
                "created_at": created_at,
                "finished_at": created_at,
                "repository": repository,
                "mode": mode,
                "final_status": status,
                "escalation_reason": None,
                "changed_files": ["a.txt"],
                "diff_lines": 12,
                "attempts": 1,
                "provider": {"endpoint_host": "api.example.test", "model": "M"},
            }
            (run_dir / "metadata.json").write_text(
                json.dumps(metadata), encoding="utf-8"
            )
        return run_dir


class ListRunsTests(RunStoreFactory):
    def test_lists_runs_newest_first(self) -> None:
        self._write_run("run-old", created_at="2026-08-20T10:00:00.000Z")
        self._write_run("run-new", created_at="2026-08-26T10:00:00.000Z")

        listing = self.store.list_runs()

        self.assertEqual(listing.warnings, [])
        self.assertEqual(
            [r["run_id"] for r in listing.rows], ["run-new", "run-old"]
        )

    def test_repository_filter_matches_normalized_path(self) -> None:
        self._write_run("run-one", created_at="2026-08-20T10:00:00.000Z")
        self._write_run(
            "run-two",
            created_at="2026-08-21T10:00:00.000Z",
            repository="/repo/two",
        )

        listing = self.store.list_runs(repository="/repo/two")
        self.assertEqual([r["run_id"] for r in listing.rows], ["run-two"])

    def test_status_filter(self) -> None:
        self._write_run("run-good", created_at="2026-08-20T10:00:00.000Z")
        self._write_run(
            "run-bad",
            created_at="2026-08-21T10:00:00.000Z",
            status="escalated",
            result_overrides={"escalation_reason": "worker-timeout"},
        )

        listing = self.store.list_runs(status="escalated")
        self.assertEqual([r["run_id"] for r in listing.rows], ["run-bad"])

    def test_limit_truncates_after_sorting(self) -> None:
        for index in range(5):
            self._write_run(
                f"run-{index}",
                created_at=f"2026-08-2{index}T10:00:00.000Z",
            )

        listing = self.store.list_runs(limit=2)
        self.assertEqual([r["run_id"] for r in listing.rows], ["run-4", "run-3"])

    def test_malformed_runs_are_skipped_with_warnings(self) -> None:
        self._write_run("run-ok", created_at="2026-08-22T10:00:00.000Z")
        self._write_run(
            "run-nometadata",
            created_at="not-even-parsed",
            include_metadata=False,
        )
        broken = self.root / "run-broken"
        broken.mkdir()
        (broken / "result.json").write_text("{bad json", encoding="utf-8")
        empty = self.root / "run-empty"
        empty.mkdir()

        listing = self.store.list_runs()
        self.assertEqual([r["run_id"] for r in listing.rows], ["run-ok"])
        self.assertEqual(len(listing.warnings), 3)


class RunIdSafetyTests(RunStoreFactory):
    def test_traversal_run_ids_are_rejected_before_filesystem_access(self) -> None:
        for unsafe in ("../secret", "../../secret", "a/b", ".", "..", "", "/abs"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(ValueError):
                    self.store.run_exists(unsafe)
                with self.assertRaises(ValueError):
                    self.store.load_run(unsafe)

    def test_valid_hex_style_ids_pass_validation(self) -> None:
        good = "e0d0f1a2b3c4d5e6a7b8c9d0e1f2a3b4"
        (self.root / good).mkdir(parents=True)
        self.assertTrue(self.store.run_exists(good))

    def test_load_missing_run_raises_run_not_found(self) -> None:
        with self.assertRaises(RunNotFoundError):
            self.store.load_run("ffffffffffffffffffffffffffffffff")


class LoadRunTests(RunStoreFactory):
    def test_load_run_returns_all_evidence_parts(self) -> None:
        self._write_run(
            "run-full",
            created_at="2026-08-25T08:00:00.000Z",
            status="ready-for-review",
        )
        record = self.store.load_run("run-full")
        self.assertEqual(record["metadata"]["created_at"], "2026-08-25T08:00:00.000Z")
        self.assertEqual(record["request"]["task"], "task run-full")
        self.assertEqual(record["result"]["status"], "ready-for-review")


class RunCliTests(RunStoreFactory):
    """End-to-end CLI behavior for ``list`` and ``show`` against a config."""

    def setUp(self) -> None:
        super().setUp()
        from tests.test_cli_commands import _run_main

        self._run_main = _run_main
        self.config_path = self.tmp / "config.toml"
        self.config_path.write_text(
            f"""
[worker]
command = "definitely-missing-claude"
provider = "cc-switch-current"
max_turns = 12
timeout_seconds = 1200
correction_limit = 1
max_changed_files = 5
max_diff_lines = 500
allowed_test_binaries = ["uv"]

run_records = "{self.root}"
test_output_limit_bytes = 65536
claude_settings = "{self.tmp / 'settings.json'}"
""".strip(),
            encoding="utf-8",
        )

    def test_list_json_is_stable_and_sorted(self) -> None:
        self._write_run("run-a", created_at="2026-08-20T10:00:00.000Z")
        self._write_run("run-b", created_at="2026-08-26T10:00:00.000Z")

        first = self._run_main(["--config", str(self.config_path), "list", "--json"])
        second = self._run_main(["--config", str(self.config_path), "list", "--json"])

        code_first, out_first, err_first = first
        self.assertEqual(code_first, 0, err_first)
        payload = json.loads(out_first)
        self.assertEqual(
            [r["run_id"] for r in payload["runs"]], ["run-b", "run-a"]
        )
        self.assertEqual(out_first, second[1])

    def test_show_traversal_run_id_exits_two_without_leaking_paths(self) -> None:
        code, out, err = self._run_main(
            ["--config", str(self.config_path), "show", "../../secret"]
        )
        self.assertEqual(code, 2)
        self.assertIn("invalid run id", err)

    def test_show_missing_run_exits_two(self) -> None:
        code, out, err = self._run_main(
            ["--config", str(self.config_path), "show", "f" * 32]
        )
        self.assertEqual(code, 2)
        self.assertIn("no such run", err)

    def test_show_json_for_existing_run(self) -> None:
        self._write_run("run-visible", created_at="2026-08-25T08:00:00.000Z")
        code, out, err = self._run_main(
            ["--config", str(self.config_path), "show", "run-visible", "--json"]
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["metadata"]["created_at"], "2026-08-25T08:00:00.000Z")


if __name__ == "__main__":
    unittest.main()


def _cleanup_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, True)

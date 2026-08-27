"""Structured run evidence tests (V1.2 Task 2).

The evidence layer upgrades the plain ``request.json``/``result.json`` pair
into a complete, tamper-evident run record:

    RUN_ID/
    ├── request.json
    ├── result.json
    ├── metadata.json
    ├── tests.json
    ├── diff.patch
    ├── events.jsonl
    └── evidence_manifest.json

Every test here is deterministic: unit tests drive ``EvidenceWriter``
directly, and one integration test drives the full executor through the
shared fake Claude fixture and inspects the produced directory.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from claude_worker_router.evidence import EvidenceWriter, atomic_write_json, utc_timestamp
from claude_worker_router.models import RunMode, RunResult, TaskRequest, TestCommand


def _request() -> TaskRequest:
    return TaskRequest(
        repository=Path("/tmp/example").resolve(),
        task="fix the fixture",
        acceptance_criteria=("example.txt contains worker",),
        mode=RunMode.EDIT,
        test_commands=(TestCommand(argv=("uv", "run", "python", "-m", "unittest")),),
        allowed_paths=("example.txt",),
    )


class EvidenceWriterUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="evidence-unit-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.writer = EvidenceWriter(self.tmp / "runs", "run-abc")

    def test_creates_full_run_directory(self) -> None:
        self.writer.create_run(_request())
        self.writer.write_metadata({"schema_version": 1, "run_id": "run-abc"})
        self.writer.write_tests([{"argv": ["uv"], "exit_code": 0}])
        self.writer.write_diff("diff --git a/example.txt b/example.txt\n")
        self.writer.write_result({"status": "ready-for-review"})
        self.writer.finalize_manifest()

        present = {p.name for p in self.writer.run_dir.iterdir()}
        expected = {
            "diff.patch",
            "evidence_manifest.json",
            "events.jsonl",
            "metadata.json",
            "request.json",
            "result.json",
            "tests.json",
        }
        self.assertEqual(present, expected)


    def test_events_are_append_only_jsonl(self) -> None:
        self.writer.create_run(_request())
        self.writer.append_event("worker-started", attempt=1)
        self.writer.append_event("tests-passed")

        lines = (self.writer.run_dir / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(lines), 3)
        payloads = [json.loads(line) for line in lines]
        self.assertEqual(payloads[0]["event"], "run-created")
        self.assertEqual(payloads[1]["event"], "worker-started")
        self.assertEqual(payloads[1]["attempt"], 1)
        self.assertEqual(payloads[2]["event"], "tests-passed")
        for payload in payloads:
            self.assertIn("timestamp", payload)

    def test_events_are_isolated_per_writer_instance(self) -> None:
        """Appending to an existing run never rewrites earlier lines."""
        self.writer.create_run(_request())
        first = (self.writer.run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.writer.append_event("later-event")
        second = (self.writer.run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertTrue(second.startswith(first))

    def test_metadata_contains_no_provider_credentials(self) -> None:
        metadata = {
            "schema_version": 1,
            "provider": {
                "endpoint_host": "api.example.test",
                "model": "Test-Model",
                "fingerprint": "0" * 64,
            },
        }
        self.writer.create_run(_request())
        self.writer.write_metadata(metadata)
        raw = (self.writer.run_dir / "metadata.json").read_text(encoding="utf-8")
        self.assertNotIn("secret-token", raw)
        self.assertIn("api.example.test", raw)

    def test_manifest_contains_sha256_for_all_evidence_files(self) -> None:
        self.writer.create_run(_request())
        self.writer.write_metadata({"a": 1})
        self.writer.write_result({"status": "ready-for-review"})
        manifest = self.writer.finalize_manifest()

        expected_files = {"request.json", "metadata.json", "result.json", "events.jsonl"}
        self.assertTrue(expected_files.issubset(manifest))
        self.assertNotIn("evidence_manifest.json", manifest)
        for name, digest in manifest.items():
            self.assertRegex(digest, r"^[0-9a-f]{64}$", name)
            actual = hashlib.sha256(
                (self.writer.run_dir / name).read_bytes()
            ).hexdigest()
            self.assertEqual(digest, actual, name)

    def test_manifest_detects_later_tampering(self) -> None:
        self.writer.create_run(_request())
        self.writer.write_result({"status": "ready-for-review"})
        manifest = self.writer.finalize_manifest()
        self.assertIn("result.json", manifest)

        result_path = self.writer.run_dir / "result.json"
        result_path.write_text('{"status": "tampered"}', encoding="utf-8")
        stale = json.loads(
            (self.writer.run_dir / "evidence_manifest.json").read_text(encoding="utf-8")
        )
        current = hashlib.sha256(result_path.read_bytes()).hexdigest()
        self.assertNotEqual(stale["result.json"], current)

    def test_atomic_json_write_survives_replacement(self) -> None:
        target = self.tmp / "payload.json"
        atomic_write_json(target, {"v": 1})
        atomic_write_json(target, {"v": 2})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"v": 2})
        leftovers = [p.name for p in self.tmp.iterdir() if p != target]
        self.assertEqual(leftovers, [])

    def test_utc_timestamp_is_iso8601_utc(self) -> None:
        stamp = utc_timestamp()
        self.assertTrue(stamp.endswith("Z"))
        # UTC midnight epoch math via datetime parsing.
        from datetime import datetime

        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)


class EvidenceIntegrationTests(unittest.TestCase):
    """Drive the real executor end-to-end and inspect its evidence directory."""

    ENV_KEYS: tuple[str, ...] = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "FAKE_CLAUDE_BEHAVIOR",
        "FAKE_CLAUDE_SETTINGS_PATH",
        "FAKE_CLAUDE_INVOCATION_COUNTER",
        "FAKE_CLAUDE_ARGV_LOG",
        "PATH",
    )

    def setUp(self) -> None:
        self._saved_env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        self.addCleanup(self._restore_env)
        self.tmp = Path(tempfile.mkdtemp(prefix="evidence-int-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _restore_env(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _seed_repository(self) -> Path:
        from tests.helpers import init_repository
        from tests.test_executor_cli import SMOKE_TEST_SOURCE, _git

        repository = init_repository(self.tmp / "fixture-repo")
        smoke_path = repository / "test_smoke.py"
        smoke_path.write_text(SMOKE_TEST_SOURCE, encoding="utf-8")
        _git(repository, "add", "test_smoke.py")
        _git(repository, "commit", "--quiet", "-m", "seed smoke test")
        return repository

    def _run_edit_fixture(self, token: str = "top-secret-token"):
        from claude_worker_router.config import RouterConfig
        from claude_worker_router.executor import execute_task
        from tests.helpers import _PATH_EXTENSIONS

        fake_executable = self.tmp / "fake-claude.py"
        shutil.copyfile(
            Path(__file__).resolve().parent / "fake_claude.py", fake_executable
        )
        fake_executable.chmod(0o755)

        settings_path = self.tmp / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_AUTH_TOKEN": token,
                        "ANTHROPIC_BASE_URL": "https://api.example.test/anthropic",
                        "ANTHROPIC_MODEL": "Test-Model",
                    }
                }
            ),
            encoding="utf-8",
        )

        repository = self._seed_repository()
        runs_root = self.tmp / "runs"

        with _FakeEnv(
            {
                "FAKE_CLAUDE_BEHAVIOR": "fix",
                "FAKE_CLAUDE_SETTINGS_PATH": str(settings_path),
                "FAKE_CLAUDE_INVOCATION_COUNTER": str(self.tmp / "counter"),
                "FAKE_CLAUDE_ARGV_LOG": str(self.tmp / "argv.json"),
                "PATH": _PATH_EXTENSIONS(),
            }
        ):
            config = RouterConfig(
                command=str(fake_executable),
                provider="cc-switch-current",
                max_turns=5,
                timeout_seconds=180,
                correction_limit=1,
                max_changed_files=5,
                max_diff_lines=500,
                allowed_test_binaries=("uv",),
                run_records=runs_root,
                test_output_limit_bytes=65536,
                claude_settings=settings_path,
            )
            request = _request_with_repository(repository)
            result = execute_task(request, config)
        return result, runs_root

    def test_edit_run_produces_complete_evidence_directory(self) -> None:
        result, runs_root = self._run_edit_fixture(token="top-secret-token")
        self.assertEqual(result.status, "ready-for-review")

        run_dirs = [p for p in runs_root.iterdir() if p.is_dir()]
        self.assertEqual(len(run_dirs), 1)
        run_dir = run_dirs[0]

        names = {p.name for p in run_dir.iterdir()}
        required = {
            "request.json",
            "result.json",
            "metadata.json",
            "tests.json",
            "diff.patch",
            "events.jsonl",
            "evidence_manifest.json",
        }
        self.assertEqual(required - names, set())

        # Legacy compatibility: request/result keep their original shapes.
        legacy_request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(legacy_request["task"], "fix the fixture")
        legacy_result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(legacy_result["status"], "ready-for-review")

        # Events: append-only, parseable, lifecycle ordered.
        lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        payloads = [json.loads(line) for line in lines]
        self.assertEqual(payloads[0]["event"], "run-created")
        worker_starts = [p for p in payloads if p["event"] == "worker-started"]
        self.assertEqual(len(worker_starts), 1)
        terminal_events = {
            "ready-for-review",
            "escalated",
            "read-only",
            "integration-completed",
        }
        self.assertIn(payloads[-1]["event"], terminal_events)

        # Metadata: no credential material anywhere.
        metadata_raw = (run_dir / "metadata.json").read_text(encoding="utf-8")
        self.assertNotIn("top-secret-token", metadata_raw)
        metadata = json.loads(metadata_raw)
        for key in (
            "schema_version",
            "run_id",
            "created_at",
            "finished_at",
            "repository",
            "mode",
            "provider",
            "attempts",
            "final_status",
        ):
            self.assertIn(key, metadata)
        self.assertEqual(metadata["final_status"], "ready-for-review")
        self.assertEqual(metadata["provider"]["endpoint_host"], "api.example.test")
        self.assertEqual(metadata["provider"]["model"], "Test-Model")
        self.assertNotIn("top-secret-token", json.dumps(metadata))

        # Tests: preserved separately and consistent with the result.
        tests = json.loads((run_dir / "tests.json").read_text(encoding="utf-8"))
        self.assertEqual(len(tests), len(legacy_result["tests"]))
        self.assertEqual(tests[0]["exit_code"], 0)

        # Diff: contains the worker's allowed-path change.
        patch = (run_dir / "diff.patch").read_text(encoding="utf-8")
        self.assertIn("example.txt", patch)

        # Manifest hashes match on-disk content.
        manifest = json.loads(
            (run_dir / "evidence_manifest.json").read_text(encoding="utf-8")
        )
        for name, digest in manifest.items():
            actual = hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
            self.assertEqual(digest, actual, name)


class _FakeEnv:
    def __init__(self, overrides: dict[str, str]) -> None:
        self._overrides = overrides
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> None:
        for key, value in self._overrides.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value

    def __exit__(self, exc_type, exc, tb) -> None:
        for key, old in self._saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _request_with_repository(repository: Path) -> TaskRequest:
    return TaskRequest(
        repository=repository,
        task="fix the fixture",
        acceptance_criteria=("example.txt should contain 'worker'",),
        mode=RunMode.EDIT,
        test_commands=(TestCommand(argv=("uv", "run", "python", "-m", "unittest")),),
        allowed_paths=("example.txt",),
    )


if __name__ == "__main__":
    unittest.main()

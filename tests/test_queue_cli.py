"""Queue management CLI tests (V1.4 Task 19).

``submit`` accepts the same task JSON as legacy stdin mode (plus optional
queue metadata ``priority``/``parent_run_id``, which never leak into the
evidence contract), registers a ``pending`` row with pre-created evidence,
and returns immediately. ``queue`` inspects the backlog. ``drain`` is a
strictly sequential single-worker executor that claims tasks atomically,
never retries, and maps outcomes onto the lifecycle machine.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from claude_worker_router import cli
from claude_worker_router.config import load_config
from claude_worker_router.models import RunResult
from claude_worker_router.state_store import StateStore


def _valid_task(**extra) -> dict:
    payload = {
        "repository": str(Path(tempfile.gettempdir()) / "queued-target"),
        "task": f"bounded fixture {len(extra)}",
        "acceptance_criteria": ["criterion"],
        "mode": "edit",
        "test_commands": [["uv", "run", "python", "-m", "unittest"]],
        "allowed_paths": ["example.txt"],
    }
    payload.update(extra)
    return payload


class QueueCliHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_stdin = sys.stdin
        self.tmp = Path(tempfile.mkdtemp(prefix="queue-cli-"))
        self.addCleanup(self._cleanup)

        self.runs_root = self.tmp / "runs"
        self.db_path = self.tmp / "state.db"
        self.settings = self.tmp / "settings.json"
        self.settings.write_text(
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://api.example.test/anthropic",
                        "ANTHROPIC_MODEL": "Test-Model",
                    }
                }
            ),
            encoding="utf-8",
        )
        self.fake_claude = self.tmp / "fake-claude.py"
        self.fake_claude.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        self.fake_claude.chmod(0o755)

        self.config_path = self.tmp / "config.toml"
        self.config_path.write_text(
            f"""
[worker]
command = "{self.fake_claude}"
provider = "cc-switch-current"
max_turns = 5
timeout_seconds = 60
correction_limit = 0
max_changed_files = 5
max_diff_lines = 500
allowed_test_binaries = ["uv"]

run_records = "{self.runs_root}"
test_output_limit_bytes = 65536
claude_settings = "{self.settings}"
""".strip(),
            encoding="utf-8",
        )
        self._config = load_config(self.config_path)

    def tearDown(self) -> None:
        sys.stdin = self._saved_stdin

    def _cleanup(self) -> None:
        shutil.rmtree(self.tmp, True)

    def _main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _store(self) -> StateStore:
        return StateStore(self.db_path)

    def _feed_stdin(self, payload: dict) -> None:
        self.sys_stdin_saved = getattr(self, "sys_stdin_saved", sys.stdin)
        sys.stdin = io.StringIO(json.dumps(payload))

    def _restore_stdin(self) -> None:
        if getattr(self, "sys_stdin_saved", None) is not None:
            sys.stdin = self.sys_stdin_saved
            self.sys_stdin_saved = None

    def _submit(self, payload: dict) -> tuple[int, str]:
        self._feed_stdin(payload)
        code, out, err = self._main(["--config", str(self.config_path), "submit"])
        self._restore_stdin()
        self.assertEqual(code, 0, err)
        return code, json.loads(out)


class SubmitTests(QueueCliHarness):
    def test_submit_registers_pending_row_with_precreated_evidence(self) -> None:
        payload = _valid_task(repository=str(self.tmp / "target"))
        (self.tmp / "target").mkdir()

        _, response = self._submit(payload)

        self.assertEqual(response["lifecycle"], "pending")
        run_id = response["run_id"]
        row = self._store().get(run_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["priority"], 0)
        self.assertIsNone(row["parent_run_id"])

        evidence = Path(row["evidence_path"])
        request_file = evidence / "request.json"
        self.assertTrue(request_file.is_file())
        events = (evidence / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(events[0])["event"], "run-created")

    def test_submit_records_priority_and_parent_metadata_outside_evidence(self) -> None:
        payload = _valid_task(priority=7, parent_run_id="p" * 32)
        Path(payload["repository"]).mkdir(exist_ok=True)

        _, response = self._submit(payload)
        row = self._store().get(response["run_id"])

        self.assertEqual(row["priority"], 7)
        self.assertEqual(row["parent_run_id"], "p" * 32)
        evidence_request = json.loads(
            (Path(row["evidence_path"]) / "request.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("priority", evidence_request)
        self.assertNotIn("parent_run_id", evidence_request)

    def test_submit_rejects_provider_override_fields(self) -> None:
        bad = _valid_task(model="alternate-provider")
        Path(bad["repository"]).mkdir(exist_ok=True)
        sys.stdin = io.StringIO(json.dumps(bad))
        code, _, err = self._main(["--config", str(self.config_path), "submit"])
        self.assertEqual(code, 2)
        self.assertIn("manual-only", err)


class QueueListTests(QueueCliHarness):
    def test_queue_orders_by_priority_and_supports_json(self) -> None:
        high = _submit_helper(self, priority=9, tag="high")
        low = _submit_helper(self, priority=1, tag="low")

        code, out, err = self._main(
            ["--config", str(self.config_path), "queue", "--json"]
        )

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        ids = [row["run_id"] for row in payload["runs"]]
        self.assertEqual(ids, [high["run_id"], low["run_id"]])
        states = {row["run_id"]: row["lifecycle"] for row in payload["runs"]}
        self.assertEqual(set(states.values()), {"pending"})


def _submit_helper(testcase: QueueCliHarness, *, priority: int, tag: str) -> dict:
    payload = _valid_task(
        repository=str(testcase.tmp / f"q-{tag}"),
        task=f"queued {tag}",
        priority=priority,
    )
    Path(payload["repository"]).mkdir(exist_ok=True)
    saved = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        code, out, err = testcase._main(
            ["--config", str(testcase.config_path), "submit"]
        )
    finally:
        sys.stdin = saved
    testcase.assertEqual(code, 0, err)
    return json.loads(out)


class DrainTests(QueueCliHarness):
    def _seed_results(self, script):
        """Patch task_queue.execute_task with an ordered scripted response."""
        calls: list[str] = []

        def fake_execute(request, config, on_child_start=None):
            run_marker = request.task
            calls.append(run_marker)
            return script[len(calls) - 1]

        return calls, fake_execute

    def test_drain_executes_backlog_sequentially_in_priority_order(self) -> None:
        high_payload = _valid_task(task="job-high", priority=5)
        low_payload = _valid_task(task="job-low", priority=1)
        for payload in (high_payload, low_payload):
            Path(payload["repository"]).mkdir(exist_ok=True)
            self._feed_stdin(payload)
            self._main(["--config", str(self.config_path), "submit"])
        self._restore_stdin()

        calls: list[str] = []

        def fake_execute(request, config, on_child_start=None):
            calls.append(request.task)
            return RunResult(run_id="x" * 32, status="ready-for-review")

        with (
            patch("claude_worker_router.task_queue.execute_task", fake_execute),
            patch("claude_worker_router.task_queue.os.getpid", return_value=123456),
        ):
            code, out, err = self._main(["--config", str(self.config_path), "drain"])

        self.assertEqual(code, 0, err)
        self.assertEqual(calls, ["job-high", "job-low"])
        rows = self._list_rows()
        self.assertEqual({r["lifecycle"] for r in rows}, {"ready-for-review"})
        for row in rows:
            self.assertEqual(row["outcome"], "ready-for-review")

    def _list_rows(self) -> list[dict]:
        import sqlite3

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM runs")]

    def test_drain_once_processes_a_single_claim(self) -> None:
        for index in range(2):
            payload = _valid_task(task=f"once-{index}", repository=str(self.tmp / f"o{index}"))
            Path(payload["repository"]).mkdir(parents=True, exist_ok=True)
            self._feed_stdin(payload)
            self._main(["--config", str(self.config_path), "submit"])
        self._restore_stdin()

        def fake_execute(request, config, on_child_start=None):
            return RunResult(run_id="y" * 32, status="ready-for-review")

        with (
            patch("claude_worker_router.task_queue.execute_task", fake_execute),
            patch("claude_worker_router.task_queue.os.getpid", return_value=123456),
        ):
            code_first, out_first, _ = self._main(
                ["--config", str(self.config_path), "drain", "--once"]
            )
            code_second, out_second, _ = self._main(
                ["--config", str(self.config_path), "drain", "--once"]
            )

        self.assertEqual(code_first, 0)
        self.assertIn("completed 1", out_first)
        self.assertEqual(code_second, 0)
        self.assertIn("completed 1", out_second)

    def test_drain_maps_escalation_to_blocked_and_exits_three(self) -> None:
        payload = _valid_task(repository=str(self.tmp / "esc"))
        Path(payload["repository"]).mkdir(exist_ok=True)
        self._feed_stdin(payload)
        self._main(["--config", str(self.config_path), "submit"])
        self._restore_stdin()

        def fake_execute(request, config, on_child_start=None):
            return RunResult(
                run_id="z" * 32,
                status="escalated",
                escalation_reason="provider-unreachable",
            )

        with (
            patch("claude_worker_router.task_queue.execute_task", fake_execute),
            patch("claude_worker_router.task_queue.os.getpid", return_value=123456),
        ):
            code, out, err = self._main(["--config", str(self.config_path), "drain"])

        self.assertEqual(code, 3)
        rows = self._list_rows()
        self.assertEqual(rows[0]["lifecycle"], "blocked")
        self.assertEqual(rows[0]["outcome"], "escalated")

    def test_drain_starts_with_crash_reconciliation(self) -> None:
        """A stale running row with a dead pid becomes blocked, not rerun."""
        payload = _valid_task(repository=str(self.tmp / "live"))
        Path(payload["repository"]).mkdir(exist_ok=True)
        _, submit_out = self._submit(_valid_task(repository=str(self.tmp / "live")))
        run_id = submit_out["run_id"]
        self._restore_stdin()

        # Simulate a crashed previous drainer: force lifecycle/running+pid.
        import sqlite3

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "UPDATE runs SET lifecycle='running', started_at=?, pid=? WHERE run_id=?",
                ("2026-08-27T00:00:00.000Z", 999999999, run_id),
            )
        # enqueue one healthy task behind it
        other = _valid_task(task="healthy", repository=str(self.tmp / "healthy"))
        Path(other["repository"]).mkdir(exist_ok=True)
        self._feed_stdin(other)
        self._main(["--config", str(self.config_path), "submit"])
        self._restore_stdin()

        executed: list[str] = []

        def fake_execute(request, config, on_child_start=None):
            executed.append(request.task)
            return RunResult(run_id="w" * 32, status="read-only")

        with (
            patch("claude_worker_router.task_queue.execute_task", fake_execute),
            patch("claude_worker_router.task_queue.os.getpid", return_value=987654),
        ):
            code, out, err = self._main(["--config", str(self.config_path), "drain"])

        self.assertEqual(code, 0, err)
        self.assertNotIn(run_id, " ".join(executed))  # never re-executed
        crashed_row = self._store().get(run_id)
        self.assertEqual(crashed_row["lifecycle"], "blocked")
        self.assertEqual(crashed_row["outcome"], "runner-interrupted")


if __name__ == "__main__":
    unittest.main()

"""CLI command dispatch tests (V1.2 Task 1).

Locks two contracts:

* The original no-subcommand stdin JSON mode keeps its exact behavior
  (read one JSON object, run the executor, emit ``RunResult`` JSON, and
  use exit codes 0/2/3). The Codex skill depends on this.
* The new V1.2 subcommands (``doctor``, ``list``, ``show``, ``integrate``,
  ``cleanup``) parse and route through the dispatcher. Until each one is
  implemented by its own task, invoking it must fail closed with a
  non-zero exit instead of silently doing nothing.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from claude_worker_router import cli
from claude_worker_router.models import RunResult


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    """Call ``cli.main`` capturing stdout/stderr; return (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _valid_request_dict() -> dict:
    return {
        "repository": "/tmp/example",
        "task": "fix the fixture",
        "acceptance_criteria": ["example.txt contains worker"],
        "mode": "edit",
        "test_commands": [["uv", "run", "python", "-m", "unittest"]],
        "allowed_paths": ["example.txt"],
    }


class LegacyStdinModeTests(unittest.TestCase):
    """The no-subcommand invocation must remain byte-compatible."""

    def setUp(self) -> None:
        self._saved_stdin = sys.stdin

    def tearDown(self) -> None:
        sys.stdin = self._saved_stdin

    def _write_config(self, tmp: Path) -> Path:
        config_path = tmp / "config.toml"
        config_path.write_text("[worker]\nplaceholder = true\n", encoding="utf-8")
        return config_path

    def test_no_subcommand_runs_executor_and_prints_result_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            config_path = self._write_config(tmp)
            request_data = _valid_request_dict()
            result = RunResult(run_id="legacy-run", status="ready-for-review")

            captured: dict[str, object] = {}

            def fake_execute(request, config):
                captured["request"] = request
                captured["config"] = config
                return result

            sys.stdin = io.StringIO(json.dumps(request_data))
            with (
                patch.object(cli, "load_config", return_value="sentinel-config"),
                patch.object(cli, "execute_task", side_effect=fake_execute),
            ):
                code, out, err = _run_main(["--config", str(config_path)])

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            payload = json.loads(out)
            self.assertEqual(payload["run_id"], "legacy-run")
            self.assertEqual(payload["status"], "ready-for-review")
            self.assertEqual(captured["config"], "sentinel-config")
            request = captured["request"]
            self.assertEqual(
                request.repository, Path("/tmp/example").resolve()
            )
            self.assertEqual(request.task, "fix the fixture")

    def test_escalated_legacy_result_exits_three(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            config_path = self._write_config(Path(raw_tmp))
            result = RunResult(
                run_id="esc-run",
                status="escalated",
                escalation_reason="provider-unreachable",
            )
            sys.stdin = io.StringIO(json.dumps(_valid_request_dict()))
            with (
                patch.object(cli, "load_config", return_value="sentinel-config"),
                patch.object(cli, "execute_task", return_value=result),
            ):
                code, out, _ = _run_main([])

            self.assertEqual(code, 3)
            self.assertEqual(json.loads(out)["escalation_reason"], "provider-unreachable")

    def test_invalid_stdin_json_returns_two_without_executor_call(self) -> None:
        sys.stdin = io.StringIO("{not json")
        with (
            patch.object(cli, "load_config") as fake_load,
            patch.object(cli, "execute_task") as fake_execute,
        ):
            code, _, err = _run_main([])

        self.assertEqual(code, 2)
        self.assertIn("invalid JSON", err)
        fake_load.assert_not_called()
        fake_execute.assert_not_called()

    def test_non_object_stdin_json_returns_two(self) -> None:
        sys.stdin = io.StringIO("[]")
        with (
            patch.object(cli, "load_config") as fake_load,
            patch.object(cli, "execute_task") as fake_execute,
        ):
            code, _, err = _run_main([])

        self.assertEqual(code, 2)
        self.assertIn("object", err)
        fake_load.assert_not_called()
        fake_execute.assert_not_called()


class SubcommandDispatchTests(unittest.TestCase):
    """Known subcommands route through the dispatcher; placeholders fail closed."""

    def test_placeholder_subcommands_return_nonzero_until_implemented(self) -> None:
        # Superseded per-command when Tasks 8/9 land; kept here so the
        # dispatcher never grows a silent success for an unbuilt command.
        # ``doctor`` left in Task 4; ``list``/``show`` left in Task 5.
        for argv in (
            ["integrate", "some-run-id"],
            ["cleanup", "some-run-id"],
        ):
            with self.subTest(argv=argv):
                with patch.object(cli, "load_config", return_value="sentinel-config"):
                    code, _, err = _run_main(argv)
                self.assertNotEqual(code, 0)
                self.assertNotEqual(err.strip(), "")

    def test_config_flag_is_accepted_before_a_subcommand(self) -> None:
        # Task 1 only guarantees argument plumbing; the placeholder still
        # exits non-zero even with an explicit --config value.
        with patch.object(cli, "load_config", return_value="sentinel-config"):
            code, _, err = _run_main(["--config", "/tmp/config.toml", "integrate", "abc"])
        self.assertNotEqual(code, 0)
        self.assertNotEqual(err.strip(), "")

    def test_unknown_subcommand_fails_with_usage_error(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(io.StringIO()):
                cli.main(["bogus-command"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_show_requires_run_id_argument(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(io.StringIO()):
                cli.main(["show"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_integrate_requires_run_id_argument(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(io.StringIO()):
                cli.main(["integrate"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_cleanup_requires_run_id_argument(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(io.StringIO()):
                cli.main(["cleanup"])
        self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()

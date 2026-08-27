"""Router doctor diagnostics tests (V1.2 Task 4).

``doctor`` verifies the local environment before a worker run: Python
runtime, configuration, Git, the Claude executable, provider settings,
run-record storage, and configured test binaries. With ``--repo`` it adds
repository checks (root, branch, HEAD, clean status, worktree support,
stale router worktrees). Exit codes: 0 READY, 1 warnings only, 2 NOT READY.
No check output may contain credentials.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from claude_worker_router.config import load_config
from claude_worker_router.doctor import (
    DoctorCheck,
    overall_status,
    run_doctor,
)
from tests.helpers import init_repository


def _write_config(tmp: Path, **overrides: str) -> Path:
    values = {
        "command": "/definitely/missing/claude-fixture",
        "claude_settings": str(tmp / "settings.json"),
        "run_records": str(tmp / "runs"),
        "binaries": '["uv", "definitely-missing-binary-fixture"]',
    }
    values.update(overrides)
    config_path = tmp / "config.toml"
    config_path.write_text(
        f"""
[worker]
command = "{values['command']}"
provider = "cc-switch-current"
max_turns = 12
timeout_seconds = 1200
correction_limit = 1
max_changed_files = 5
max_diff_lines = 500
allowed_test_binaries = {values['binaries']}

run_records = "{values['run_records']}"
test_output_limit_bytes = 65536
claude_settings = "{values['claude_settings']}"
""".strip(),
        encoding="utf-8",
    )
    return config_path


def _write_settings(tmp: Path) -> Path:
    settings = tmp / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": "secret-doctor-token",
                    "ANTHROPIC_BASE_URL": "https://api.example.test/anthropic",
                    "ANTHROPIC_MODEL": "Test-Model",
                }
            }
        ),
        encoding="utf-8",
    )
    return settings


class OverallStatusTests(unittest.TestCase):
    def test_error_dominates_warning(self) -> None:
        self.assertEqual(
            overall_status(
                [
                    DoctorCheck("a", "ok", ""),
                    DoctorCheck("b", "warning", ""),
                    DoctorCheck("c", "ok", ""),
                ]
            ),
            "warning",
        )
        self.assertEqual(
            overall_status(
                [
                    DoctorCheck("a", "warning", ""),
                    DoctorCheck("b", "error", ""),
                ]
            ),
            "error",
        )
        self.assertEqual(
            overall_status([DoctorCheck("a", "ok", "")]),
            "ok",
        )


class RunDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="doctor-"))
        self.addCleanup(_cleanup_tree, self.tmp)

    def _fixture_config(self, **kwargs) -> object:
        return load_config(_write_config(self.tmp, **kwargs))

    def _healthy_environment(self) -> None:
        _write_settings(self.tmp)
        # A real executable for the Claude command slot.
        fake_claude = self.tmp / "fake-claude.py"
        fake_claude.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        fake_claude.chmod(0o755)
        self._config = self._fixture_config(command=str(fake_claude))

    def test_healthy_environment_has_no_errors(self) -> None:
        self._healthy_environment()
        checks = run_doctor(self._config)

        names = [c.name for c in checks]
        for expected in (
            "python-runtime",
            "router-config",
            "git-executable",
            "claude-executable",
            "claude-settings",
            "provider-routing",
            "run-records-storage",
            "test-binary:uv",
        ):
            self.assertIn(expected, names)

        errors = [c for c in checks if c.status == "error"]
        self.assertEqual(
            [f"{c.name}: {c.detail}" for c in errors],
            [],
        )
        missing_binary = next(c for c in checks if c.name == "test-binary:definitely-missing-binary-fixture")
        self.assertEqual(missing_binary.status, "warning")

        serialized = json.dumps([c.__dict__ for c in checks])
        self.assertNotIn("secret-doctor-token", serialized)

    def test_missing_claude_executable_is_an_error(self) -> None:
        _write_settings(self.tmp)
        checks = run_doctor(self._fixture_config())
        claude_check = next(c for c in checks if c.name == "claude-executable")
        self.assertEqual(claude_check.status, "error")

    def test_malformed_provider_settings_are_an_error(self) -> None:
        settings = self.tmp / "broken-settings.json"
        settings.write_text("{not json", encoding="utf-8")
        fake_claude = self.tmp / "fake-claude.py"
        fake_claude.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        fake_claude.chmod(0o755)

        checks = run_doctor(
            self._fixture_config(claude_settings=str(settings), command=str(fake_claude))
        )
        statuses = {c.name: c.status for c in checks}
        self.assertEqual(statuses["claude-settings"], "error")
        self.assertEqual(statuses["provider-routing"], "error")

    def test_run_records_storage_becomes_writable_after_check(self) -> None:
        self._healthy_environment()
        run_doctor(self._config)
        records = self.tmp / "runs"
        self.assertTrue(records.is_dir())
        self.assertEqual(list(records.iterdir()), [])


class RepositoryModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="doctor-repo-"))
        self.addCleanup(_cleanup_tree, self.tmp)
        settings = self.tmp / "settings.json"
        settings.write_text(
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
        fake_claude = self.tmp / "fake-claude.py"
        fake_claude.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        fake_claude.chmod(0o755)
        self.config = load_config(
            _write_config(
                self.tmp,
                command=str(fake_claude),
                claude_settings=str(settings),
            )
        )

    def test_clean_repository_reports_no_errors(self) -> None:
        repository = init_repository(self.tmp / "repo")
        checks = run_doctor(self.config, repository=repository)

        by_name = {c.name: c for c in checks}
        for name in (
            "repository",
            "repository-branch",
            "repository-head",
            "repository-clean",
            "worktree-support",
            "stale-worktrees",
        ):
            self.assertIn(name, by_name)
            self.assertNotEqual(by_name[name].status, "error", name)
        self.assertEqual(by_name["repository-branch"].detail, "main")
        self.assertEqual(by_name["repository-clean"].status, "ok")

    def test_dirty_repository_is_a_warning_not_an_error(self) -> None:
        repository = init_repository(self.tmp / "repo")
        (repository / "example.txt").write_text("dirty\n", encoding="utf-8")

        checks = run_doctor(self.config, repository=repository)
        clean = next(c for c in checks if c.name == "repository-clean")
        self.assertEqual(clean.status, "warning")

        errors = [c.name for c in checks if c.status == "error"]
        self.assertEqual(errors, [])

    def test_non_git_directory_is_an_error(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        checks = run_doctor(self.config, repository=plain)
        repo_check = next(c for c in checks if c.name == "repository")
        self.assertEqual(repo_check.status, "error")


class DoctorCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="doctor-cli-"))
        self.addCleanup(_cleanup_tree, self.tmp)

    def test_json_mode_is_machine_readable_and_exits_zero_when_healthy(self) -> None:
        from tests.test_cli_commands import _run_main

        settings = self.tmp / "settings.json"
        settings.write_text(
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
        repository = init_repository(self.tmp / "repo")
        fake_claude = self.tmp / "fake-claude.py"
        fake_claude.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        fake_claude.chmod(0o755)
        config_path = _write_config(
            self.tmp,
            command=str(fake_claude),
            claude_settings=str(settings),
            binaries='["uv"]',
        )

        code, out, err = _run_main(
            ["--config", str(config_path), "doctor", "--json", "--repo", str(repository)]
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertIn(payload["status"], ("ok", "warning"))
        check_names = {c["name"] for c in payload["checks"]}
        self.assertIn("git-executable", check_names)
        self.assertIn("repository", check_names)

    def test_missing_config_still_produces_structured_failure(self) -> None:
        from tests.test_cli_commands import _run_main

        code, out, err = _run_main(
            ["--config", "/definitely/missing/config.toml", "doctor", "--json"]
        )
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["checks"][0]["name"], "router-config")


def _cleanup_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, True)


class QueueHealthTests(unittest.TestCase):
    """Crashed runners surface in doctor as actionable warnings (V1.4 T20)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="doctor-queue-"))
        self.addCleanup(_cleanup_tree, self.tmp)
        settings = self.tmp / "settings.json"
        settings.write_text(
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
        fake_claude = self.tmp / "fake-claude.py"
        fake_claude.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        fake_claude.chmod(0o755)
        self.config = load_config(
            _write_config(
                self.tmp,
                command=str(fake_claude),
                claude_settings=str(settings),
                run_records=str(self.tmp / "runs"),
            )
        )

    def test_missing_state_database_is_ok_not_an_error(self) -> None:
        checks = run_doctor(self.config)
        queue_check = next(c for c in checks if c.name == "queue-health")
        self.assertEqual(queue_check.status, "ok")

    def test_interrupted_running_row_is_reported_for_explicit_recovery(self) -> None:
        from claude_worker_router.state_store import StateStore

        store = StateStore(self.tmp / "state.db")
        store.insert_pending(
            run_id="a" * 32,
            repository="/repo/q",
            evidence_path="/records/" + "a" * 32,
        )
        store.claim_next(pid=2_000_000_000)

        checks = run_doctor(self.config)
        queue_check = next(c for c in checks if c.name == "queue-health")

        # PID 2000000000 is astronomically unlikely to exist in tests.
        self.assertEqual(queue_check.status, "warning")
        self.assertIn("runner-interrupted", queue_check.detail)
        self.assertIn("never", queue_check.detail.lower())


if __name__ == "__main__":
    unittest.main()


class QueueHealthRobustnessTests(unittest.TestCase):
    """Regression (review C4): corrupt/locked state.db must not crash doctor."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="doctor-sqlite-"))
        self.addCleanup(_cleanup_tree, self.tmp)
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
        fake = self.tmp / "fake-claude.py"
        fake.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        fake.chmod(0o755)
        self.config = load_config(
            _write_config(
                self.tmp,
                command=str(fake),
                claude_settings=str(self.settings),
                run_records=str(self.tmp / "runs"),
            )
        )

    def test_corrupt_state_db_yields_error_check_not_traceback(self) -> None:
        # Where resolve_effective_policy's state db would live: next to runs.
        db = self.tmp / "state.db"
        db.write_bytes(b"this is definitely not a sqlite database")
        checks = run_doctor(self.config)
        queue_check = next(c for c in checks if c.name == "queue-health")
        self.assertEqual(queue_check.status, "error")
        self.assertIn("state db", queue_check.detail.lower())

"""End-to-end executor and CLI tests driven by ``tests/fake_claude.py``.

Each test seeds a temporary Git repository, sets the fake Claude's behavior via
environment variables, and runs the executor against a bounded request. The
fake Claude never touches real credentials and writes its argv to a JSON log so
the tests can assert against the executor's command-line contract.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from unittest.mock import patch

from claude_worker_router.config import RouterConfig
from claude_worker_router.executor import execute_task
from claude_worker_router.git_workspace import GitWorkspace
from claude_worker_router.models import RunMode, RunResult, TaskRequest, TestCommand
from tests.helpers import _git, init_repository


FAKE_CLAUDE_SOURCE = Path(__file__).resolve().parent / "fake_claude.py"
WORKTEE_REPO_NAME = "fixture-repo"
ENV_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "DATABASE_URL",
    "FAKE_CLAUDE_BEHAVIOR",
    "FAKE_CLAUDE_WORKTREE",
    "FAKE_CLAUDE_SETTINGS_PATH",
    "FAKE_CLAUDE_INVOCATION_COUNTER",
    "FAKE_CLAUDE_ARGV_LOG",
    "GITHUB_TOKEN",
    "PATH",
)


SMOKE_TEST_SOURCE = """import unittest
from pathlib import Path


class SmokeTest(unittest.TestCase):
    def test_example_txt_is_worker(self):
        self.assertEqual(Path("example.txt").read_text(encoding="utf-8"), "worker\\n")


if __name__ == "__main__":
    unittest.main()
"""


@dataclass(frozen=True)
class FixtureRun:
    result: RunResult
    captured_claude_argv: tuple[str, ...]
    record_paths: tuple[Path, ...]
    claude_invocation_count: int


class _FakeEnvironment:
    """Context manager that pins the parent-process environment for the fake."""

    def __init__(self, overrides: dict[str, str]) -> None:
        self._overrides = overrides
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> None:
        for key, value in self._overrides.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value

    def __exit__(self, exc_type, exc, tb) -> None:
        for key in self._saved:
            if self._saved[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = self._saved[key]


class ExecutorCliTests(unittest.TestCase):
    """End-to-end tests that exercise the executor against a fake Claude."""

    def setUp(self) -> None:
        # Snapshot and replace the env vars the fake consults. The tests never
        # rely on a pre-existing value, so an empty dict is a safe default.
        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        self._env_root = Path(tempfile.mkdtemp(prefix="executor-cli-env-"))
        self.addCleanup(shutil.rmtree, self._env_root, True)

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def run_fixture(
        self,
        mode: str = "edit",
        fake_behavior: str = "fix",
        token: str = "irrelevant-token",
        test_commands: tuple[TestCommand, ...] | None = None,
        allowed_paths: tuple[str, ...] = ("example.txt",),
        timeout_seconds: float = 180,
    ) -> FixtureRun:
        tmp = Path(tempfile.mkdtemp(prefix="executor-cli-"))
        self.addCleanup(shutil.rmtree, tmp, True)

        repository = _init_fixture_repository(tmp / WORKTEE_REPO_NAME)

        return self._run_executor_against(
            tmp,
            repository,
            mode=mode,
            fake_behavior=fake_behavior,
            token=token,
            test_commands=test_commands,
            allowed_paths=allowed_paths,
            timeout_seconds=timeout_seconds,
        )

    def _run_executor_against(
        self,
        tmp: Path,
        repository: Path,
        mode: str = "edit",
        fake_behavior: str = "fix",
        token: str = "irrelevant-token",
        test_commands: tuple[TestCommand, ...] | None = None,
        allowed_paths: tuple[str, ...] = ("example.txt",),
        timeout_seconds: float = 180,
    ) -> FixtureRun:
        """Drive ``execute_task`` against an arbitrary pre-seeded repository.

        Tests that need the main checkout to be dirty, or the repository to be
        a plain non-Git directory, build their own ``repository`` and call this
        helper instead of going through ``run_fixture``.
        """
        fake_executable = tmp / "fake-claude.py"
        shutil.copyfile(FAKE_CLAUDE_SOURCE, fake_executable)
        fake_executable.chmod(0o755)

        settings_path = tmp / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_AUTH_TOKEN": token,
                        "ANTHROPIC_BASE_URL": "https://api.example.test/anthropic",
                        "ANTHROPIC_MODEL": "Test-Model",
                        "ANTHROPIC_DEFAULT_SONNET_MODEL": "Test-Model",
                    }
                }
            ),
            encoding="utf-8",
        )

        counter_file = tmp / "claude.counter"
        argv_log = tmp / "claude.argv.json"

        path_extensions = self._build_path()
        with _FakeEnvironment(
            {
                "FAKE_CLAUDE_BEHAVIOR": fake_behavior,
                "FAKE_CLAUDE_SETTINGS_PATH": str(settings_path),
                "FAKE_CLAUDE_INVOCATION_COUNTER": str(counter_file),
                "FAKE_CLAUDE_ARGV_LOG": str(argv_log),
                "PATH": path_extensions,
            }
        ):
            config = RouterConfig(
                command=str(fake_executable),
                provider="cc-switch-current",
                max_turns=5,
                timeout_seconds=timeout_seconds,
                correction_limit=1,
                max_changed_files=5,
                max_diff_lines=500,
                allowed_test_binaries=("uv",),
                run_records=tmp / "runs",
                test_output_limit_bytes=65536,
                claude_settings=settings_path,
            )

            request = TaskRequest(
                repository=repository,
                task="fix the fixture",
                acceptance_criteria=("example.txt should contain 'worker'",),
                mode=RunMode(mode),
                test_commands=test_commands
                if test_commands is not None
                else (
                    TestCommand(argv=("uv", "run", "python", "-m", "unittest")),
                ),
                allowed_paths=allowed_paths,
            )

            result = execute_task(request, config)

        captured_argv: tuple[str, ...] = ()
        if argv_log.exists():
            captured_argv = tuple(json.loads(argv_log.read_text(encoding="utf-8")))

        invocation_count = (
            int(counter_file.read_text(encoding="utf-8"))
            if counter_file.exists()
            else 0
        )

        record_paths: tuple[Path, ...] = ()
        if config.run_records.exists():
            record_paths = tuple(sorted(config.run_records.rglob("*.json")))

        return FixtureRun(
            result=result,
            captured_claude_argv=captured_argv,
            record_paths=record_paths,
            claude_invocation_count=invocation_count,
        )

    def _build_path(self) -> str:
        # Ensure subprocesses invoked by the executor can locate ``uv``.
        existing = os.environ.get("PATH", "")
        return os.pathsep.join(
            ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", existing]
        )

    # ---------------------------------------------------------------- tests

    def test_passes_prompt_on_stdin_and_never_grants_bash(self) -> None:
        fixture = self.run_fixture(mode="edit", fake_behavior="fix")
        argv = fixture.captured_claude_argv
        self.assertIn("--safe-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertIn("--tools", argv)
        tools = argv[argv.index("--tools") + 1]
        self.assertEqual(tools, "Read,Glob,Grep,Edit,Write")
        self.assertIn("--allowedTools", argv)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], tools)
        self.assertNotIn("Bash", tools)
        self.assertNotIn("--settings", argv)
        self.assertNotIn("--model", argv)
        self.assertNotIn("fix the fixture", " ".join(argv))
        self.assertEqual(fixture.result.status, "ready-for-review")

    def test_read_only_mode_exposes_no_edit_tools_and_never_mutates_repository(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="executor-cli-read-only-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        repository = _init_fixture_repository(tmp / WORKTEE_REPO_NAME)

        fixture = self._run_executor_against(
            tmp,
            repository,
            mode="read-only",
            fake_behavior="attempt-write-if-enabled",
        )

        argv = fixture.captured_claude_argv
        self.assertIn("--safe-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Glob,Grep")
        self.assertEqual(fixture.result.status, "read-only")
        self.assertEqual(fixture.result.tests, [])
        self.assertIsNone(fixture.result.commit)
        self.assertEqual(
            (repository / "example.txt").read_text(encoding="utf-8"),
            "main\n",
        )

    def test_changed_path_outside_allowed_scope_escalates_without_commit(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="executor-cli-scope-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        repository = _init_fixture_repository(tmp / WORKTEE_REPO_NAME)

        fixture = self._run_executor_against(
            tmp,
            repository,
            fake_behavior="fix-and-outside",
            allowed_paths=("example.txt",),
        )

        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "path-scope-exceeded")
        self.assertIsNone(fixture.result.commit)
        self.assertEqual(
            (repository / "example.txt").read_text(encoding="utf-8"),
            "main\n",
        )

    def test_outside_path_overrides_failed_test_escalation(self) -> None:
        fixture = self.run_fixture(
            fake_behavior="fix-and-outside",
            allowed_paths=("example.txt",),
            test_commands=(
                TestCommand(
                    argv=("uv", "run", "python", "-c", "raise SystemExit(1)")
                ),
            ),
        )

        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "path-scope-exceeded")

    def test_outside_path_is_measured_after_worker_error(self) -> None:
        fixture = self.run_fixture(
            fake_behavior="outside-then-error",
            allowed_paths=("example.txt",),
        )

        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "path-scope-exceeded")

    def test_passing_tests_with_no_changes_skip_empty_commit(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="executor-cli-no-change-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        repository = _init_fixture_repository(tmp / WORKTEE_REPO_NAME)

        fixture = self._run_executor_against(
            tmp,
            repository,
            fake_behavior="no-change",
            test_commands=(
                TestCommand(argv=("uv", "run", "python", "-c", "pass")),
            ),
        )

        self.assertEqual(fixture.result.status, "ready-for-review")
        self.assertEqual(fixture.result.changed_files, [])
        self.assertIsNone(fixture.result.commit)

    def test_runs_approved_tests_with_shell_false(self) -> None:
        fixture = self.run_fixture(mode="edit", fake_behavior="fix")
        first_test = fixture.result.tests[0]
        self.assertEqual(first_test["exit_code"], 0)
        self.assertEqual(
            first_test["argv"],
            ["uv", "run", "python", "-m", "unittest"],
        )

    def test_executor_tests_do_not_create_python_cache_scope_changes(self) -> None:
        fixture = self.run_fixture(
            mode="edit",
            fake_behavior="fix",
            allowed_paths=("example.txt",),
        )

        self.assertEqual(fixture.result.status, "ready-for-review")
        self.assertEqual(fixture.result.changed_files, ["example.txt"])

    def test_executor_tests_receive_only_minimal_safe_environment(self) -> None:
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "parent-process-secret"
        os.environ["ANTHROPIC_API_KEY"] = "parent-api-secret"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "parent-aws-secret"
        os.environ["DATABASE_URL"] = "postgres://secret@example.invalid/db"
        os.environ["GITHUB_TOKEN"] = "parent-github-secret"
        fixture = self.run_fixture(
            mode="edit",
            fake_behavior="fix",
            test_commands=(
                TestCommand(
                    argv=(
                        "uv",
                        "run",
                        "python",
                        "-c",
                        (
                            "import os,sys; "
                            "blocked=('ANTHROPIC_AUTH_TOKEN','ANTHROPIC_API_KEY',"
                            "'AWS_SECRET_ACCESS_KEY','DATABASE_URL','GITHUB_TOKEN'); "
                            "sys.exit(9 if any(k in os.environ for k in blocked) "
                            "or 'PATH' not in os.environ else 0)"
                        ),
                    )
                ),
            ),
        )

        self.assertEqual(fixture.result.tests[0]["exit_code"], 0)
        self.assertEqual(fixture.result.status, "ready-for-review")

    def test_allows_one_correction_after_test_failure(self) -> None:
        fixture = self.run_fixture(mode="edit", fake_behavior="fail-then-fix")
        self.assertEqual(fixture.result.attempts, 2)
        self.assertEqual(fixture.result.status, "ready-for-review")

    def test_escalates_after_second_failure(self) -> None:
        fixture = self.run_fixture(mode="edit", fake_behavior="always-fail")
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "tests-failed-after-correction")

    def test_provider_change_blocks_integration(self) -> None:
        fixture = self.run_fixture(mode="edit", fake_behavior="provider-change")
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "provider-configuration-changed")

    def test_provider_change_is_detected_after_worker_error(self) -> None:
        fixture = self.run_fixture(
            mode="edit",
            fake_behavior="provider-change-error",
        )
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "provider-configuration-changed")

    def test_path_scope_remains_primary_when_provider_also_changes(self) -> None:
        fixture = self.run_fixture(
            mode="edit",
            fake_behavior="outside-and-provider-change",
            allowed_paths=("example.txt",),
        )
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "path-scope-exceeded")
        self.assertIn("provider configuration also changed", fixture.result.summary)

    def test_provider_failure_does_not_try_another_profile(self) -> None:
        fixture = self.run_fixture(mode="edit", fake_behavior="provider-error")
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.attempts, 1)
        self.assertEqual(fixture.result.escalation_reason, "provider-unreachable")
        self.assertEqual(fixture.claude_invocation_count, 1)

    def test_permission_denial_has_specific_escalation_reason(self) -> None:
        fixture = self.run_fixture(fake_behavior="permission-denied")
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "worker-permission-denied")
        self.assertEqual(fixture.result.attempts, 1)

    def test_turn_limit_has_specific_escalation_reason(self) -> None:
        fixture = self.run_fixture(fake_behavior="turn-limit")
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "worker-turn-limit")
        self.assertEqual(fixture.result.attempts, 1)

    def test_malformed_worker_output_has_specific_escalation_reason(self) -> None:
        fixture = self.run_fixture(fake_behavior="malformed-output")
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "worker-output-invalid")
        self.assertEqual(fixture.result.attempts, 1)

    def test_compatibility_warning_alone_is_not_a_provider_failure(self) -> None:
        fixture = self.run_fixture(fake_behavior="compat-warning-only")
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "worker-output-invalid")
        self.assertEqual(fixture.result.attempts, 1)

    def test_unknown_nonzero_cli_failure_is_distinct(self) -> None:
        fixture = self.run_fixture(fake_behavior="cli-error")
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "worker-cli-failed")
        self.assertEqual(fixture.result.attempts, 1)

    def test_worker_timeout_has_specific_escalation_reason(self) -> None:
        fixture = self.run_fixture(
            fake_behavior="worker-timeout",
            timeout_seconds=0.05,
        )
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "worker-timeout")
        self.assertEqual(fixture.result.attempts, 1)

    def test_test_timeout_is_recorded_and_escalated(self) -> None:
        fixture = self.run_fixture(
            fake_behavior="fix",
            timeout_seconds=1.0,
            test_commands=(
                TestCommand(
                    argv=(
                        "uv",
                        "run",
                        "python",
                        "-c",
                        "import time; time.sleep(1.5)",
                    )
                ),
            ),
        )
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "test-timeout")
        self.assertEqual(fixture.result.tests[0]["timeout"], True)

    def test_worktree_creation_failure_is_structured(self) -> None:
        error = subprocess.CalledProcessError(1, ["git", "worktree", "add"])
        with patch.object(GitWorkspace, "create", side_effect=error):
            fixture = self.run_fixture(fake_behavior="fix")
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "worktree-failed")
        self.assertEqual(fixture.claude_invocation_count, 0)

    def test_commit_failure_is_structured(self) -> None:
        error = subprocess.CalledProcessError(1, ["git", "commit"])
        with patch.object(GitWorkspace, "commit_worker_change", side_effect=error):
            fixture = self.run_fixture(fake_behavior="fix")
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "git-commit-failed")
        self.assertIsNone(fixture.result.commit)

    def test_change_measurement_failure_is_structured(self) -> None:
        error = subprocess.CalledProcessError(1, ["git", "diff", "--name-only"])
        with patch.object(GitWorkspace, "measure_changes", side_effect=error):
            fixture = self.run_fixture(fake_behavior="fix")
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "git-measure-failed")
        self.assertIsNone(fixture.result.commit)

    def test_result_files_do_not_contain_token(self) -> None:
        fixture = self.run_fixture(
            mode="edit", fake_behavior="fix", token="top-secret-token"
        )
        self.assertTrue(fixture.record_paths, "expected record files to be written")
        for path in fixture.record_paths:
            self.assertNotIn("top-secret-token", path.read_text(encoding="utf-8"))

    def test_dirty_checkout_escalates_without_running_worker(self) -> None:
        """A dirty main checkout in edit mode must escalate and never invoke Claude.

        The previous implementation set ``escalation_reason="dirty-checkout"`` but
        still proceeded to run the worker against ``request.repository``, which
        meant a fake-Claude (and, in production, a real Claude) edit would land
        on the dirty main checkout.
        """
        tmp = Path(tempfile.mkdtemp(prefix="executor-cli-dirty-"))
        self.addCleanup(shutil.rmtree, tmp, True)

        repository = _init_fixture_repository(tmp / WORKTEE_REPO_NAME)
        # Introduce an uncommitted change so ``GitWorkspace.create`` raises
        # ``DirtyCheckoutError`` during workspace preparation.
        (repository / "example.txt").write_text("dirty\n", encoding="utf-8")

        fixture = self._run_executor_against(
            tmp, repository, mode="edit", fake_behavior="fix"
        )
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(fixture.result.escalation_reason, "dirty-checkout")
        self.assertEqual(fixture.claude_invocation_count, 0)
        # The dirty main checkout must remain untouched.
        self.assertEqual(
            (repository / "example.txt").read_text(encoding="utf-8"),
            "dirty\n",
        )

    def test_non_git_edit_request_escalates_without_running_worker(self) -> None:
        """An edit request against a non-Git directory must escalate.

        Only read-only operation is permitted against a non-Git target. The
        executor must not run the worker (or write evidence of a worker run)
        for an edit request that points at a plain directory.
        """
        tmp = Path(tempfile.mkdtemp(prefix="executor-cli-nongit-"))
        self.addCleanup(shutil.rmtree, tmp, True)

        # A plain directory with no Git history. The fake Claude would happily
        # create ``example.txt`` here if it were invoked, which is exactly the
        # outcome we are guarding against.
        repository = tmp / "plain-dir"
        repository.mkdir(parents=True)

        fixture = self._run_executor_against(
            tmp, repository, mode="edit", fake_behavior="fix"
        )
        self.assertEqual(fixture.result.status, "escalated")
        self.assertEqual(
            fixture.result.escalation_reason, "non-git-edit-disabled"
        )
        self.assertEqual(fixture.claude_invocation_count, 0)
        self.assertFalse((repository / "example.txt").exists())


def _init_fixture_repository(repository: Path) -> Path:
    """Initialize a temporary repository pre-seeded with a smoke unittest."""
    init_repository(repository)
    _seed_smoke_test(repository)
    return repository


def _seed_smoke_test(repository: Path) -> None:
    """Add a smoke unittest to the repository's HEAD so worker worktrees inherit it.

    The test asserts ``example.txt`` contains ``"worker\\n"``. The fake Claude
    writes exactly that text on a successful fix, so the executor's test run
    only passes when the worker actually edited the worktree.
    """
    smoke_path = repository / "test_smoke.py"
    smoke_path.write_text(SMOKE_TEST_SOURCE, encoding="utf-8")
    _git(repository, "add", "test_smoke.py")
    _git(repository, "commit", "--quiet", "-m", "seed smoke test")


if __name__ == "__main__":
    unittest.main()

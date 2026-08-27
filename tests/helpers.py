"""Shared helpers for worker-router tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from claude_worker_router.config import RouterConfig
from claude_worker_router.executor import execute_task
from claude_worker_router.models import RunMode, TaskRequest, TestCommand


def _PATH_EXTENSIONS() -> str:
    """A PATH that lets executor subprocesses locate ``uv``."""

    existing = os.environ.get("PATH", "")
    return os.pathsep.join(
        ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", existing]
    )


def _git(repository: Path, *args: str) -> None:
    """Run a git command with ``-C <repository>`` and raise on failure."""

    subprocess.run(
        ["git", "-C", str(repository), *args],
        shell=False,
        check=True,
        text=True,
        capture_output=True,
    )


def git_head(repository: Path) -> str:
    """Return the current HEAD commit SHA of a repository."""

    result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        shell=False,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class FixtureOutcome:
    result: object
    runs_root: Path
    repository: Path
    invocation_count: int
    captured_argv: tuple[str, ...]
    config: RouterConfig | None = None


def seed_smoke_test(repository: Path) -> Path:
    """Add the fixture smoke unittest to a repository's HEAD."""

    smoke = SMOKE_TEST_SOURCE.replace("{value}", "worker")
    smoke_path = repository / "test_smoke.py"
    smoke_path.write_text(smoke, encoding="utf-8")
    _git(repository, "add", "test_smoke.py")
    _git(repository, "commit", "--quiet", "-m", "seed smoke test")
    return repository


class GitWorkspaceScaffold:
    """Minimal isolated-workspace stand-in for unit-level safety checks."""

    def __init__(self, repository: Path, path: Path) -> None:
        self.repository = repository
        self.path = path

    @classmethod
    def create(cls, repository: Path) -> "GitWorkspaceScaffold":
        root = Path(
            subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
                shell=False,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
        ).resolve()
        repo_name = root.name
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            shell=False,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        import uuid as _uuid

        token = _uuid.uuid4().hex[:8]
        branch = f"safety-scaffold/{token}"
        worktree_root = root.parent / f".safety-scaffolds-{repo_name}" / token
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree_root),
                head,
            ],
            shell=False,
            check=True,
            capture_output=True,
            text=True,
        )
        scaffold = cls(root, worktree_root)
        scaffold.branch = branch

        import atexit as _atexit

        def _cleanup() -> None:
            subprocess.run(
                ["git", "-C", str(root), "worktree", "remove", "--force", str(worktree_root)],
                shell=False,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "branch", "-D", branch],
                shell=False,
                capture_output=True,
            )

        _atexit.register(_cleanup)
        return scaffold


SMOKE_TEST_SOURCE = '''import unittest
from pathlib import Path


class SmokeTest(unittest.TestCase):
    def test_example_txt_is_worker(self):
        self.assertEqual(Path("example.txt").read_text(encoding="utf-8"), "{value}\\n")


if __name__ == "__main__":
    unittest.main()
'''


def run_bounded_fixture(
    tmp: Path,
    *,
    behavior: str = "fix",
    mode: str = "edit",
    repository: Path | None = None,
    allowed_paths: tuple[str, ...] = ("example.txt",),
    token: str = "fixture-secret-token",
    timeout_seconds: float = 180,
    command_override: str | None = None,
    global_policy_body: str | None = None,
    project_policy_body: str | None = None,
) -> FixtureOutcome:
    """Drive ``execute_task`` end-to-end with the deterministic fake Claude.

    Seeds a fresh Git repository (unless one is supplied), writes provider
    settings containing ``token`` for leak assertions, and returns the run
    outcome plus the evidence root so tests can inspect the run directory.
    """

    if repository is None:
        repository = init_repository(tmp / "bounded-fixture-repo")
        seed_smoke_test(repository)

    fake_executable = tmp / "fake-claude.py"
    shutil.copyfile(
        Path(__file__).resolve().parent / "fake_claude.py", fake_executable
    )
    fake_executable.chmod(0o755)

    settings_path = tmp / "settings.json"
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

    counter_file = tmp / "claude.counter"
    argv_log = tmp / "claude.argv.json"

    global_policy_path: Path | None = None
    if global_policy_body is not None:
        global_policy_path = tmp / "global-policy.toml"
        global_policy_path.write_text(global_policy_body, encoding="utf-8")
    if project_policy_body is not None and repository is not None:
        (repository / ".claude-worker-router").mkdir(exist_ok=True)
        (repository / ".claude-worker-router" / "policy.toml").write_text(
            project_policy_body, encoding="utf-8"
        )

    overrides = {
        "FAKE_CLAUDE_BEHAVIOR": behavior,
        "FAKE_CLAUDE_SETTINGS_PATH": str(settings_path),
        "FAKE_CLAUDE_INVOCATION_COUNTER": str(counter_file),
        "FAKE_CLAUDE_ARGV_LOG": str(argv_log),
        "PATH": _PATH_EXTENSIONS(),
    }
    saved: dict[str, str | None] = {}
    try:
        for key, value in overrides.items():
            saved[key] = os.environ.get(key)
            os.environ[key] = value

        config = RouterConfig(
            command=command_override or str(fake_executable),
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
            global_policy_path=global_policy_path,
        )
        request = TaskRequest(
            repository=repository,
            task="fix the fixture",
            acceptance_criteria=("example.txt should contain 'worker'",),
            mode=RunMode(mode),
            test_commands=(
                (TestCommand(argv=("uv", "run", "python", "-m", "unittest")),)
                if mode == "edit"
                else ()
            ),
            allowed_paths=allowed_paths,
        )
        result = execute_task(request, config)
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    captured: tuple[str, ...] = ()
    if argv_log.exists():
        captured = tuple(json.loads(argv_log.read_text(encoding="utf-8")))
    count = (
        int(counter_file.read_text(encoding="utf-8"))
        if counter_file.exists()
        else 0
    )

    return FixtureOutcome(
        result=result,
        runs_root=tmp / "runs",
        repository=repository,
        invocation_count=count,
        captured_argv=captured,
        config=config,
    )


def init_repository(repository: Path) -> Path:
    """Initialize a temporary repository on ``main`` with an ``example.txt`` commit.

    Returns the repository path after creating it so the caller can chain
    directory operations directly.
    """

    repository = Path(repository)
    repository.mkdir(parents=True, exist_ok=True)
    _git(repository, "init", "--initial-branch=main", "--quiet")
    _git(repository, "config", "user.email", "router-test@example.invalid")
    _git(repository, "config", "user.name", "Router Test")
    _git(repository, "config", "commit.gpgsign", "false")
    (repository / "example.txt").write_text("main\n", encoding="utf-8")
    _git(repository, "add", "example.txt")
    _git(repository, "commit", "--quiet", "-m", "seed example.txt")
    return repository

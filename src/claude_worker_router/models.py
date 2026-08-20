from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class RunMode(StrEnum):
    READ_ONLY = "read-only"
    EDIT = "edit"


@dataclass(frozen=True)
class TestCommand:
    argv: tuple[str, ...]

    @classmethod
    def from_value(cls, value: Any) -> "TestCommand":
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
            raise ValueError("each test command must be a non-empty argv array")
        return cls(tuple(value))


@dataclass(frozen=True)
class TaskRequest:
    repository: Path
    task: str
    acceptance_criteria: tuple[str, ...]
    mode: RunMode
    test_commands: tuple[TestCommand, ...]
    allowed_paths: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskRequest":
        forbidden = {"provider_profile", "settings", "model"}.intersection(data)
        if forbidden:
            raise ValueError("provider selection is manual-only in CC Switch")
        task = data.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be non-empty")
        repository = data.get("repository")
        if not isinstance(repository, str) or not repository:
            raise ValueError("repository must be a non-empty path")
        criteria = data.get("acceptance_criteria", [])
        if not isinstance(criteria, list) or not all(isinstance(item, str) and item for item in criteria):
            raise ValueError("acceptance_criteria must be strings")
        mode = RunMode(data.get("mode", "edit"))
        commands = tuple(TestCommand.from_value(item) for item in data.get("test_commands", []))
        allowed_paths = data.get("allowed_paths", [])
        if not isinstance(allowed_paths, list) or not all(isinstance(item, str) and item for item in allowed_paths):
            raise ValueError("allowed_paths must be strings")
        return cls(Path(repository).resolve(), task.strip(), tuple(criteria), mode, commands, tuple(allowed_paths))

    def to_dict(self) -> dict[str, Any]:
        """Serialize the request for the redacted run record (no env, no tokens)."""
        return {
            "repository": str(self.repository),
            "task": self.task,
            "acceptance_criteria": list(self.acceptance_criteria),
            "mode": self.mode.value,
            "test_commands": [list(cmd.argv) for cmd in self.test_commands],
            "allowed_paths": list(self.allowed_paths),
        }


@dataclass(frozen=True)
class RouterConfig:
    command: str
    provider: str
    max_turns: int
    timeout_seconds: int
    correction_limit: int
    max_changed_files: int
    max_diff_lines: int
    allowed_test_binaries: tuple[str, ...]
    run_records: Path
    test_output_limit_bytes: int
    claude_settings: Path


@dataclass
class RunResult:
    run_id: str
    status: str
    branch: str | None = None
    worktree: str | None = None
    commit: str | None = None
    changed_files: list[str] = field(default_factory=list)
    diff_lines: int = 0
    tests: list[dict[str, Any]] = field(default_factory=list)
    provider: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    escalation_reason: str | None = None
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "branch": self.branch,
            "worktree": self.worktree,
            "commit": self.commit,
            "changed_files": self.changed_files,
            "diff_lines": self.diff_lines,
            "tests": self.tests,
            "provider": self.provider,
            "summary": self.summary,
            "escalation_reason": self.escalation_reason,
            "attempts": self.attempts,
        }
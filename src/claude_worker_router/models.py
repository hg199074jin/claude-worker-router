from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any


class RunMode(StrEnum):
    READ_ONLY = "read-only"
    EDIT = "edit"


class RunLifecycle(StrEnum):
    """Management-pipeline state of a run (V1.4).

    Deliberately separate from :attr:`RunResult.status`, which keeps its
    legacy execution-outcome vocabulary for stdin-API compatibility.
    """

    PENDING = "pending"
    RUNNING = "running"
    READY_FOR_REVIEW = "ready-for-review"
    INTEGRATED = "integrated"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


#: Lifecycle states that accept no further transitions.
TERMINAL_LIFECYCLES: frozenset[RunLifecycle] = frozenset(
    {
        RunLifecycle.INTEGRATED,
        RunLifecycle.BLOCKED,
        RunLifecycle.CANCELLED,
    }
)

#: Allowed lifecycle transitions; anything else must be refused explicitly.
_LIFECYCLE_TRANSITIONS: dict[RunLifecycle, frozenset[RunLifecycle]] = {
    RunLifecycle.PENDING: frozenset(
        {RunLifecycle.RUNNING, RunLifecycle.CANCELLED}
    ),
    RunLifecycle.RUNNING: frozenset(
        {
            RunLifecycle.READY_FOR_REVIEW,
            RunLifecycle.BLOCKED,
            RunLifecycle.CANCELLED,
        }
    ),
    RunLifecycle.READY_FOR_REVIEW: frozenset(
        {RunLifecycle.INTEGRATED, RunLifecycle.CANCELLED}
    ),
    RunLifecycle.INTEGRATED: frozenset(),
    RunLifecycle.BLOCKED: frozenset(),
    RunLifecycle.CANCELLED: frozenset(),
}


def assert_lifecycle_transition(
    current: RunLifecycle, target: RunLifecycle
) -> None:
    """Raise ``ValueError`` when ``current → target`` is not allowed."""
    if target not in _LIFECYCLE_TRANSITIONS[current]:
        raise ValueError(
            f"illegal lifecycle transition: {current.value} -> {target.value}"
        )


def lifecycle_from_outcome(status: str) -> RunLifecycle:
    """Map a legacy execution outcome to its lifecycle; never guess.

    A successful read-only run maps to ``ready-for-review`` while the
    outcome itself stays recorded separately, exactly as the V1.4 design
    specifies.
    """
    if status in ("ready-for-review", "read-only"):
        return RunLifecycle.READY_FOR_REVIEW
    if status == "escalated":
        return RunLifecycle.BLOCKED
    raise ValueError(f"unknown execution outcome: {status!r}")


@dataclass(frozen=True)
class TestCommand:
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.argv or not all(
            isinstance(item, str) and item for item in self.argv
        ):
            raise ValueError("each test command must be a non-empty argv array")

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

    def __post_init__(self) -> None:
        normalized_paths = tuple(
            _normalize_allowed_path(path) for path in self.allowed_paths
        )
        object.__setattr__(self, "allowed_paths", normalized_paths)
        if self.mode == RunMode.EDIT and not normalized_paths:
            raise ValueError("edit mode requires allowed_paths")
        if self.mode == RunMode.EDIT and not self.test_commands:
            raise ValueError("edit mode requires test_commands")
        if self.mode == RunMode.READ_ONLY and self.test_commands:
            raise ValueError("read-only mode does not accept test_commands")

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
        raw_allowed_paths = data.get("allowed_paths", [])
        if not isinstance(raw_allowed_paths, list) or not all(isinstance(item, str) and item for item in raw_allowed_paths):
            raise ValueError("allowed_paths must be strings")
        allowed_paths = tuple(_normalize_allowed_path(item) for item in raw_allowed_paths)
        return cls(Path(repository).resolve(), task.strip(), tuple(criteria), mode, commands, allowed_paths)

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
    binary_edit_policy: str = "deny"


@dataclass
class RunResult:
    run_id: str
    status: str
    branch: str | None = None
    worktree: str | None = None
    commit: str | None = None
    base_branch: str | None = None
    base_sha: str | None = None
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
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "changed_files": self.changed_files,
            "diff_lines": self.diff_lines,
            "tests": self.tests,
            "provider": self.provider,
            "summary": self.summary,
            "escalation_reason": self.escalation_reason,
            "attempts": self.attempts,
        }


def _normalize_allowed_path(value: str) -> str:
    raw_parts = value.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise ValueError(
            "allowed_paths must be relative paths without empty or traversal components"
        )
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise ValueError(
            "allowed_paths must be relative paths without empty or traversal components"
        )
    return str(path)

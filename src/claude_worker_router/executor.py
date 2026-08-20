"""Bounded Claude worker, test runner, and evidence writer.

The executor never grants the worker Bash, never places the task on argv, and
treats a single provider failure as ``provider-unreachable`` without trying an
alternate profile. Subprocess invocations always use argv lists with
``shell=False``; the only ``check=False`` exception (already in
``git_workspace._count_diff_lines``) is preserved verbatim.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RouterConfig
from .git_workspace import (
    DirtyCheckoutError,
    GitWorkspace,
    PathScopeExceededError,
    ScopeExceededError,
)
from .models import RunMode, RunResult, TaskRequest, TestCommand
from .provider import (
    ProviderConfigError,
    fingerprint_provider,
    read_provider_snapshot,
)


#: The complete tool whitelist exposed to the Claude worker. ``Bash`` is
#: deliberately excluded; the executor alone runs the approved test argv arrays.
WORKER_TOOLS = "Read,Glob,Grep,Edit,Write"
READ_ONLY_TOOLS = "Read,Glob,Grep"


@dataclass(frozen=True)
class _WorkerLoopResult:
    attempts: int
    tests: list[dict[str, Any]]
    summary: str
    worker_failed: bool


@dataclass(frozen=True)
class _WorkerOutcome:
    status: str
    summary: str


def run_test_command(
    command: TestCommand,
    cwd: Path,
    timeout: int,
    output_limit: int,
) -> dict[str, Any]:
    """Run a single test argv array with ``shell=False`` and bounded capture."""
    argv = list(command.argv)
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        shell=False,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return {
        "argv": argv,
        "exit_code": proc.returncode,
        "stdout": _truncate(proc.stdout or "", output_limit),
        "stderr": _truncate(proc.stderr or "", output_limit),
        "timeout": False,
    }


def execute_task(request: TaskRequest, config: RouterConfig) -> RunResult:
    """Run one bounded worker task and return review evidence without integrating it."""
    run_id = _new_run_id()
    result = RunResult(run_id=run_id, status="escalated")
    result.provider = {"endpoint_host": "", "model": ""}

    workspace = _prepare_workspace(request, run_id, result)

    # ``_prepare_workspace`` escalates without returning a workspace when the
    # edit target is unsafe: a dirty Git checkout or a non-Git directory. Stop
    # here so the worker loop never runs against an unsafe target.
    if workspace is None and result.escalation_reason is not None:
        _write_records(config, run_id, request, result)
        return result

    try:
        before_snapshot = read_provider_snapshot(config.claude_settings)
    except ProviderConfigError as exc:
        _set_escalation(result, "provider-config-error", str(exc))
        _write_records(config, run_id, request, result)
        return result

    result.provider = {
        "endpoint_host": before_snapshot.endpoint_host,
        "model": before_snapshot.model,
    }
    before_fingerprint = fingerprint_provider(before_snapshot)

    loop_result = _run_worker_loop(
        request, config, workspace.path if workspace else request.repository, run_id
    )
    result.attempts = loop_result.attempts
    result.tests = loop_result.tests
    last_summary = loop_result.summary

    if loop_result.worker_failed:
        _set_escalation(result, "provider-unreachable", last_summary)
        _write_records(config, run_id, request, result)
        return result

    if request.mode == RunMode.READ_ONLY:
        _set_status(result, "read-only")
    elif loop_result.tests and all(t["exit_code"] == 0 for t in loop_result.tests):
        _set_status(result, "ready-for-review")
    else:
        _set_escalation(result, "tests-failed-after-correction", last_summary)

    if workspace is not None and result.status == "ready-for-review":
        try:
            measure = workspace.measure_changes(
                config.max_changed_files,
                config.max_diff_lines,
                request.allowed_paths,
            )
            result.changed_files = list(measure.files)
            result.diff_lines = measure.diff_lines
        except PathScopeExceededError as exc:
            _set_escalation(result, "path-scope-exceeded", str(exc))
        except ScopeExceededError as exc:
            _set_escalation(result, "scope-exceeded", str(exc))

    try:
        after_snapshot = read_provider_snapshot(config.claude_settings)
        after_fingerprint = fingerprint_provider(after_snapshot)
    except ProviderConfigError as exc:
        _set_escalation(result, "provider-config-error", str(exc))
        _write_records(config, run_id, request, result)
        return result

    if after_fingerprint != before_fingerprint:
        _set_escalation(result, "provider-configuration-changed", last_summary)
        _write_records(config, run_id, request, result)
        return result

    if (
        workspace is not None
        and result.status == "ready-for-review"
        and result.changed_files
    ):
        commit_sha = workspace.commit_worker_change(
            f"codex-worker: {_short_task(request.task)}"
        )
        result.commit = commit_sha

    _write_records(config, run_id, request, result)
    return result


def _prepare_workspace(
    request: TaskRequest, run_id: str, result: RunResult
) -> GitWorkspace | None:
    """Create a worktree for edit mode on a Git repo; otherwise return ``None``.

    Read-only mode (and edit mode against a non-Git input) operates in-place.
    """
    if request.mode != RunMode.EDIT:
        result.worktree = str(request.repository)
        result.branch = _detect_branch(request.repository)
        return None

    if not _is_git_repo(request.repository):
        _set_escalation(
            result,
            "non-git-edit-disabled",
            "edit mode requires a Git repository",
        )
        return None

    try:
        workspace = GitWorkspace.create(request.repository, run_id)
    except DirtyCheckoutError as exc:
        _set_escalation(result, "dirty-checkout", str(exc))
        return None
    result.worktree = str(workspace.path)
    result.branch = workspace.branch
    return workspace


def _run_worker_loop(
    request: TaskRequest,
    config: RouterConfig,
    cwd: Path,
    run_id: str,
) -> _WorkerLoopResult:
    """Invoke the worker up to ``correction_limit + 1`` times; return outcome."""
    attempts = 0
    tests: list[dict[str, Any]] = []
    last_summary = ""
    prompt = _build_prompt(request, config)

    while attempts <= config.correction_limit:
        attempts += 1
        outcome = _invoke_worker(config, prompt, cwd, request.mode)
        last_summary = outcome.summary

        if outcome.status == "provider-unreachable":
            return _WorkerLoopResult(
                attempts=attempts,
                tests=tests,
                summary=last_summary,
                worker_failed=True,
            )

        if request.mode == RunMode.READ_ONLY:
            return _WorkerLoopResult(
                attempts=attempts,
                tests=[],
                summary=last_summary,
                worker_failed=False,
            )

        tests = _run_all_tests(request.test_commands, cwd, config)
        if tests and all(t["exit_code"] == 0 for t in tests):
            return _WorkerLoopResult(
                attempts=attempts,
                tests=tests,
                summary=last_summary,
                worker_failed=False,
            )

        if attempts > config.correction_limit:
            return _WorkerLoopResult(
                attempts=attempts,
                tests=tests,
                summary=last_summary,
                worker_failed=False,
            )

        prompt = _build_correction_prompt(
            request, config, tests, last_summary
        )

    return _WorkerLoopResult(
        attempts=attempts,
        tests=tests,
        summary=last_summary,
        worker_failed=False,
    )


def _invoke_worker(
    config: RouterConfig,
    prompt: str,
    cwd: Path,
    mode: RunMode,
) -> _WorkerOutcome:
    """Invoke Claude with the bounded argv contract; never raises."""
    tools, permission_mode = _worker_policy(mode)
    argv = [
        config.command,
        "--safe-mode",
        "--print",
        "--input-format", "text",
        "--output-format", "json",
        "--max-turns", str(config.max_turns),
        "--permission-mode", permission_mode,
        "--tools", tools,
        "--allowedTools", tools,
    ]

    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            shell=False,
            check=False,
            text=True,
            capture_output=True,
            timeout=config.timeout_seconds,
            input=prompt,
        )
    except subprocess.TimeoutExpired as exc:
        return _WorkerOutcome(
            status="provider-unreachable",
            summary=f"worker timed out after {exc.timeout} seconds",
        )
    except OSError as exc:
        return _WorkerOutcome(
            status="provider-unreachable",
            summary=f"worker could not be launched: {exc}",
        )

    if proc.returncode != 0:
        return _WorkerOutcome(
            status="provider-unreachable",
            summary=_bounded_stderr(proc.stderr),
        )

    summary = _parse_worker_output(proc.stdout)
    if summary is None:
        return _WorkerOutcome(
            status="provider-unreachable",
            summary="worker output was not a valid JSON object",
        )

    return _WorkerOutcome(status="ok", summary=summary)


def _parse_worker_output(stdout: str) -> str | None:
    """Parse the worker JSON defensively; ``None`` means escalation.

    Stderr-only compatibility messages (such as ``claude-code:unrecognized_model``)
    are ignored because the executor already requires exit ``0`` and a valid
    JSON payload.
    """
    if not stdout:
        return None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("result")
    if isinstance(raw, str):
        return raw[:1024]
    return ""


def _build_prompt(request: TaskRequest, config: RouterConfig) -> str:
    criteria = "\n".join(f"- {c}" for c in request.acceptance_criteria) or "- (none)"
    allowed = "\n".join(f"- {p}" for p in request.allowed_paths) or "- (none)"
    return (
        "TASK\n"
        f"{request.task}\n\n"
        "ACCEPTANCE CRITERIA\n"
        f"{criteria}\n\n"
        "ALLOWED PATHS\n"
        f"{allowed}\n\n"
        "CHANGE BUDGETS\n"
        f"- max_changed_files: {config.max_changed_files}\n"
        f"- max_diff_lines: {config.max_diff_lines}\n\n"
        "CONSTRAINTS\n"
        "- Before acting, locate and read applicable AGENTS.md and CLAUDE.md files in the repository.\n"
        f"- Use only these tools: {_worker_policy(request.mode)[0]}. Never use Bash.\n"
        f"- Mode: {request.mode.value}; do not edit in read-only mode.\n"
        "- Do not modify Claude settings or provider configuration.\n"
        "- Stop immediately on hard risk gates.\n"
    )


def _worker_policy(mode: RunMode) -> tuple[str, str]:
    if mode == RunMode.READ_ONLY:
        return READ_ONLY_TOOLS, "dontAsk"
    return WORKER_TOOLS, "acceptEdits"


def _build_correction_prompt(
    request: TaskRequest,
    config: RouterConfig,
    test_results: list[dict[str, Any]],
    last_summary: str,
) -> str:
    criteria = "\n".join(f"- {c}" for c in request.acceptance_criteria) or "- (none)"
    failing_blocks: list[str] = []
    half_limit = max(1, config.test_output_limit_bytes // 2)
    for t in test_results:
        if t["exit_code"] == 0:
            continue
        failing_blocks.append(
            f"- argv: {t['argv']}\n"
            f"  exit_code: {t['exit_code']}\n"
            f"  stdout: {t.get('stdout', '')[:half_limit]}\n"
            f"  stderr: {t.get('stderr', '')[:half_limit]}\n"
        )
    failing_text = "\n".join(failing_blocks) if failing_blocks else "(none)"
    return (
        "CORRECTION\n"
        "Your previous edit failed the project's tests. Re-edit the worktree.\n\n"
        "ACCEPTANCE CRITERIA\n"
        f"{criteria}\n\n"
        "PREVIOUS WORKER SUMMARY\n"
        f"{last_summary or '(none)'}\n\n"
        "FAILING TESTS\n"
        f"{failing_text}\n\n"
        "CONSTRAINTS\n"
        "- Use only Read, Glob, Grep, Edit, Write tools. Never use Bash.\n"
        "- Do not modify Claude settings.\n"
        f"- Bounded test output: at most {config.test_output_limit_bytes} bytes.\n"
    )


def _run_all_tests(
    commands: tuple[TestCommand, ...],
    cwd: Path,
    config: RouterConfig,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for cmd in commands:
        first = cmd.argv[0]
        if first not in config.allowed_test_binaries:
            results.append(
                {
                    "argv": list(cmd.argv),
                    "exit_code": 126,
                    "stdout": "",
                    "stderr": (
                        f"test binary '{first}' is not in the allowlist"
                    ),
                    "timeout": False,
                }
            )
            continue
        results.append(
            run_test_command(
                cmd, cwd, config.timeout_seconds, config.test_output_limit_bytes
            )
        )
    return results


def _set_status(result: RunResult, status: str) -> None:
    result.status = status
    if status == "ready-for-review":
        result.escalation_reason = None


def _set_escalation(result: RunResult, reason: str, summary: str) -> None:
    result.status = "escalated"
    result.escalation_reason = reason
    result.summary = summary


def _write_records(
    config: RouterConfig,
    run_id: str,
    request: TaskRequest,
    result: RunResult,
) -> None:
    record_dir = config.run_records / run_id
    record_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(record_dir / "request.json", request.to_dict())
    _atomic_write_json(record_dir / "result.json", result.to_dict())


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


def _bounded_stderr(stderr: str | None) -> str:
    return _truncate(stderr or "", 200)


def _detect_branch(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
            shell=False,
            check=True,
            text=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return result.stdout.strip() or None


def _is_git_repo(path: Path) -> bool:
    try:
        subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            shell=False,
            check=True,
            text=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return True


def _new_run_id() -> str:
    return uuid.uuid4().hex


def _short_task(task: str) -> str:
    cleaned = " ".join(task.split())
    return cleaned[:60] if cleaned else "task"

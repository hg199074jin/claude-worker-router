"""Bounded Claude worker, test runner, and evidence writer.

The executor never grants the worker Bash, never places the task on argv, and
never tries an alternate provider profile. Subprocess invocations always use
argv lists with ``shell=False``; the only ``check=False`` exception (already
in ``git_workspace._count_diff_lines``) is preserved verbatim.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import RouterConfig
from .evidence import EvidenceWriter, parse_utc_timestamp, utc_timestamp
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
from .policy import (
    BUILTIN_DENY_PATHS,
    PolicyRelaxationRejected,
    default_global_policy_path,
    resolve_effective_policy,
)
from .safety import (
    ExternalSymlinkError,
    find_binary_changes,
    validate_symlinks,
)


#: The complete tool whitelist exposed to the Claude worker. ``Bash`` is
#: deliberately excluded; the executor alone runs the approved test argv arrays.
WORKER_TOOLS = "Read,Glob,Grep,Edit,Write"
READ_ONLY_TOOLS = "Read,Glob,Grep"
TEST_ENV_ALLOWLIST = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "TZ",
    "USER",
)


@dataclass(frozen=True)
class _WorkerLoopResult:
    attempts: int
    tests: list[dict[str, Any]]
    summary: str
    escalation_reason: str | None


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
    """Run a test without provider credentials or Python cache byproducts."""
    argv = list(command.argv)
    test_env = {
        key: os.environ[key]
        for key in TEST_ENV_ALLOWLIST
        if key in os.environ
    }
    test_env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=test_env,
            shell=False,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": argv,
            "exit_code": 124,
            "stdout": _truncate(_timeout_text(exc.stdout), output_limit),
            "stderr": _truncate(_timeout_text(exc.stderr), output_limit),
            "timeout": True,
        }
    except OSError as exc:
        return {
            "argv": argv,
            "exit_code": 127,
            "stdout": "",
            "stderr": _truncate(str(exc), output_limit),
            "timeout": False,
            "error": "test-launch-failed",
        }
    return {
        "argv": argv,
        "exit_code": proc.returncode,
        "stdout": _truncate(proc.stdout or "", output_limit),
        "stderr": _truncate(proc.stderr or "", output_limit),
        "timeout": False,
    }


def execute_task(
    request: TaskRequest,
    config: RouterConfig,
    *,
    on_child_start: Callable[[int], None] | None = None,
    run_id: str | None = None,
) -> RunResult:
    """Run one bounded worker task and return review evidence without integrating."""
    # ``run_id`` lets the queue layer keep ONE identity from submission
    # through execution; callers without a queue keep the legacy fresh id.
    reused = bool(run_id)
    run_id = run_id or _new_run_id()
    result = RunResult(run_id=run_id, status="escalated")
    result.provider = {"endpoint_host": "", "model": ""}

    writer = EvidenceWriter(config.run_records, run_id)
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "router_version": __version__,
        "created_at": utc_timestamp(),
        "worker_started_at": None,
        "worker_finished_at": None,
        "finished_at": None,
        "duration_ms": 0,
        "repository": str(request.repository),
        "repository_realpath": os.path.realpath(request.repository),
        "base_branch": None,
        "base_sha": None,
        "worker_branch": None,
        "worker_commit": None,
        "worktree": None,
        "mode": request.mode.value,
        "provider": {"endpoint_host": None, "model": None, "fingerprint": None},
        "changed_files": [],
        "diff_lines": 0,
        "attempts": 0,
        "final_status": None,
        "escalation_reason": None,
    }

    def record_event(event: str, **fields: Any) -> None:
        try:
            writer.append_event(event, **fields)
        except OSError:
            # A lost timeline line must never abort the run itself.
            pass

    try:
        if reused and (writer.run_dir / "request.json").is_file():
            # Queue-resumed run: the timeline already exists from submit.
            writer.append_event("executor-attached", mode=request.mode.value)
        else:
            writer.create_run(request)
    except OSError as exc:
        _set_escalation(result, "evidence-write-failed", str(exc))
        return result

    disallowed_test_binaries = sorted(
        {
            command.argv[0]
            for command in request.test_commands
            if command.argv[0] not in config.allowed_test_binaries
        }
    )
    if disallowed_test_binaries:
        _set_escalation(
            result,
            "test-binary-not-allowed",
            "test binaries are not in the allowlist: "
            + ", ".join(disallowed_test_binaries),
        )
        return _finish_result(config, run_id, request, result, writer, metadata)

    resolved_worker_command = _resolve_worker_command(config.command)
    if resolved_worker_command is None:
        _set_escalation(
            result,
            "worker-launch-failed",
            "worker command must be a bare executable name or absolute path "
            f"and must be executable: {config.command}",
        )
        return _finish_result(config, run_id, request, result, writer, metadata)
    config = replace(config, command=resolved_worker_command)

    try:
        before_snapshot = read_provider_snapshot(config.claude_settings)
    except ProviderConfigError as exc:
        _set_escalation(result, "provider-config-error", str(exc))
        return _finish_result(config, run_id, request, result, writer, metadata)

    before_fingerprint = fingerprint_provider(before_snapshot)
    result.provider = {
        "endpoint_host": before_snapshot.endpoint_host,
        "model": before_snapshot.model,
    }
    metadata["provider"] = {
        "endpoint_host": before_snapshot.endpoint_host,
        "model": before_snapshot.model,
        "fingerprint": before_fingerprint,
    }

    result.base_branch = _detect_branch(request.repository)
    result.base_sha = _resolve_head(request.repository)
    metadata["base_branch"] = result.base_branch
    metadata["base_sha"] = result.base_sha

    # Tracked symlinks are scanned on the source repository before any
    # isolation or worker work so escape hatches fail closed up front.
    if _is_git_repo(request.repository):
        try:
            validate_symlinks(
                request.repository, request.allowed_paths, request.mode
            )
        except ExternalSymlinkError as exc:
            record_event("symlink-scan-denied", detail=str(exc))
            _set_escalation(result, "external-symlink-denied", str(exc))
            return _finish_result(config, run_id, request, result, writer, metadata)
        record_event("symlink-scan-passed")

    # Policy fold (config floor ← global file ← project file), tightened
    # budgets replace the run cap, and static deny intersections refuse
    # the request before anything expensive happens.
    try:
        resolved = resolve_effective_policy(
            _base_policy_from(config),
            global_path=(
                config.global_policy_path
                if config.global_policy_path is not None
                else default_global_policy_path()
            ),
            repository=request.repository,
        )
    except PolicyRelaxationRejected as exc:
        record_event("policy-rejected", detail=str(exc))
        _set_escalation(result, "policy-relaxation-rejected", str(exc))
        return _finish_result(config, run_id, request, result, writer, metadata)

    # Test-profile resolution happens before any preflight that inspects
    # test binaries; the router owns profile contents so an unknown name is
    # a structured escalation rather than a CLI crash.
    if request.test_profile:
        profile = config.test_profiles.get(request.test_profile)
        if profile is None:
            message = f"test profile {request.test_profile!r} is not configured"
            record_event("test-profile-unknown", detail=message)
            _set_escalation(result, "test-profile-unknown", message)
            return _finish_result(config, run_id, request, result, writer, metadata)
        from dataclasses import replace as _replace

        # Profile commands face the SAME allowlist preflight as inline
        # commands -- before any worker is invoked (design invariant:
        # "binaries outside the allowlist are rejected before the worker
        # is called").
        disallowed_profile_binaries = sorted(
            {
                command.argv[0]
                for command in profile.commands
                if command.argv[0] not in config.allowed_test_binaries
            }
        )
        if disallowed_profile_binaries:
            message = (
                "test binaries are not in the allowlist: "
                + ", ".join(disallowed_profile_binaries)
            )
            record_event(
                "test-binary-not-allowed", source="profile", detail=message
            )
            _set_escalation(result, "test-binary-not-allowed", message)
            return _finish_result(config, run_id, request, result, writer, metadata)

        request = _replace(
            request,
            test_commands=tuple(profile.commands),
            exclusive_tests=request.exclusive_tests or profile.exclusive,
        )

    effective = resolved.effective

    from .platform import macos_sandbox as _sbx

    if effective.sandbox_required and not _sbx.is_sandbox_enforced():
        detail = str(_sbx.SandboxUnavailable())
        record_event("sandbox-unavailable", detail=detail)
        _set_escalation(result, "sandbox-unavailable", detail)
        return _finish_result(config, run_id, request, result, writer, metadata)

    metadata["global_policy_hash"] = resolved.global_fingerprint
    metadata["project_policy_hash"] = resolved.project_fingerprint

    from .policy import RouterPolicy as _RouterPolicy

    enforced_denies_pre = tuple(
        sorted(set(effective.deny_paths) | set(BUILTIN_DENY_PATHS))
    )
    enforced_effective = _RouterPolicy(
        max_turns=effective.max_turns,
        timeout_seconds=effective.timeout_seconds,
        max_changed_files=effective.max_changed_files,
        max_diff_lines=effective.max_diff_lines,
        deny_paths=enforced_denies_pre,
        sandbox_required=effective.sandbox_required,
    )
    metadata["effective_policy_hash"] = enforced_effective.fingerprint()
    effective = enforced_effective

    config = replace(
        config,
        max_turns=effective.max_turns,
        timeout_seconds=effective.timeout_seconds,
        max_changed_files=effective.max_changed_files,
        max_diff_lines=effective.max_diff_lines,
    )
    enforced_denies = tuple(
        sorted(set(effective.deny_paths))
    )
    overlapping = [
        scope
        for scope in request.allowed_paths
        if _scope_hits_denies(scope, enforced_denies)
    ]
    if overlapping:
        message = (
            "request allowed_paths intersect policy denials: "
            + ", ".join(overlapping)
            + " vs "
            + ", ".join(enforced_denies)
        )
        record_event("policy-path-denied", stage="static", detail=message)
        _set_escalation(result, "policy-path-denied", message)
        return _finish_result(config, run_id, request, result, writer, metadata)

    workspace = _prepare_workspace(request, run_id, result)

    # ``_prepare_workspace`` escalates without returning a workspace when the
    # edit target is unsafe: a dirty Git checkout or a non-Git directory. Stop
    # here so the worker loop never runs against an unsafe target.
    if workspace is None and result.escalation_reason is not None:
        return _finish_result(config, run_id, request, result, writer, metadata)

    metadata["worktree"] = result.worktree
    metadata["worker_branch"] = result.branch

    loop_result = _run_worker_loop(
        request,
        config,
        workspace.path if workspace else request.repository,
        record_event,
        metadata,
        on_child_start=on_child_start,
    )
    result.attempts = loop_result.attempts
    result.tests = loop_result.tests
    last_summary = loop_result.summary

    if loop_result.escalation_reason is not None:
        _set_escalation(result, loop_result.escalation_reason, last_summary)
    elif request.mode == RunMode.READ_ONLY:
        _set_status(result, "read-only")
    elif loop_result.tests and all(t["exit_code"] == 0 for t in loop_result.tests):
        _set_status(result, "ready-for-review")
    else:
        _set_escalation(result, "tests-failed-after-correction", last_summary)

    if workspace is not None:
        # Binary changes are checked first: their numstat rows carry ``-``,
        # so file/diff-line budgets would silently under-count them.
        binary_denial = None
        git_measure_error: str | None = None
        try:
            binary_denial = _detect_binary_changes(workspace)
        except (subprocess.CalledProcessError, OSError, RuntimeError) as exc:
            git_measure_error = str(exc)

        if binary_denial is not None:
            _set_escalation(result, "binary-change-denied", binary_denial)
        elif git_measure_error is not None:
            _set_escalation(result, "git-measure-failed", git_measure_error)
        else:
            try:
                measure = workspace.measure_changes(
                    config.max_changed_files,
                    config.max_diff_lines,
                    request.allowed_paths,
                )
                denied_files = [
                    changed
                    for changed in measure.files
                    if _file_under_any(changed, enforced_denies)
                ]
                if denied_files:
                    raise _PolicyPathDenied(denied_files)
                result.changed_files = list(measure.files)
                result.diff_lines = measure.diff_lines
            except _PolicyPathDenied as exc:
                message = (
                    "changed files hit policy denials: "
                    + ", ".join(exc.paths)
                )
                record_event("policy-path-denied", stage="dynamic", detail=message)
                _set_escalation(result, "policy-path-denied", message)
            except PathScopeExceededError as exc:
                _set_escalation(result, "path-scope-exceeded", str(exc))
            except ScopeExceededError as exc:
                _set_escalation(result, "scope-exceeded", str(exc))
            except (subprocess.CalledProcessError, OSError, RuntimeError) as exc:
                _set_escalation(result, "git-measure-failed", str(exc))

        try:
            writer.write_diff(workspace.render_patch())
        except (subprocess.CalledProcessError, OSError) as exc:
            record_event("evidence-diff-unavailable", detail=str(exc))

    metadata["changed_files"] = list(result.changed_files)
    metadata["diff_lines"] = result.diff_lines

    try:
        after_snapshot = read_provider_snapshot(config.claude_settings)
        after_fingerprint = fingerprint_provider(after_snapshot)
    except ProviderConfigError as exc:
        if result.escalation_reason == "path-scope-exceeded":
            _append_summary(result, f"provider configuration check also failed: {exc}")
        else:
            _set_escalation(result, "provider-config-error", str(exc))
        return _finish_result(config, run_id, request, result, writer, metadata)

    if after_fingerprint != before_fingerprint:
        if result.escalation_reason == "path-scope-exceeded":
            _append_summary(result, "provider configuration also changed")
        else:
            _set_escalation(result, "provider-configuration-changed", last_summary)
        return _finish_result(config, run_id, request, result, writer, metadata)

    if (
        workspace is not None
        and result.status == "ready-for-review"
        and result.changed_files
    ):
        try:
            result.commit = workspace.commit_worker_change(
                f"codex-worker: {_short_task(request.task)}"
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            _set_escalation(result, "git-commit-failed", _subprocess_summary(exc))

    return _finish_result(config, run_id, request, result, writer, metadata)


def _detect_binary_changes(workspace: GitWorkspace) -> str | None:
    """Return a denial summary when binary changes exist; ``None`` otherwise."""
    offenders = find_binary_changes(workspace.path)
    if not offenders:
        return None
    return "binary changes are denied by policy: " + ", ".join(offenders)


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
    except (subprocess.CalledProcessError, OSError) as exc:
        _set_escalation(result, "worktree-failed", _subprocess_summary(exc))
        return None
    result.worktree = str(workspace.path)
    result.branch = workspace.branch
    return workspace


def _run_worker_loop(
    request: TaskRequest,
    config: RouterConfig,
    cwd: Path,
    record_event: Any,
    metadata: dict[str, Any],
    on_child_start: Callable[[int], None] | None = None,
) -> _WorkerLoopResult:
    """Invoke the worker up to ``correction_limit + 1`` times; return outcome."""
    attempts = 0
    tests: list[dict[str, Any]] = []
    last_summary = ""
    prompt = _build_prompt(request, config)

    while attempts <= config.correction_limit:
        attempts += 1
        record_event("worker-started", attempt=attempts)
        if metadata["worker_started_at"] is None:
            metadata["worker_started_at"] = utc_timestamp()
        outcome = _invoke_worker(
            config, prompt, cwd, request.mode, on_child_start=on_child_start
        )
        metadata["worker_finished_at"] = utc_timestamp()
        record_event("worker-finished", attempt=attempts, status=outcome.status)
        last_summary = outcome.summary

        if outcome.status != "ok":
            return _WorkerLoopResult(
                attempts=attempts,
                tests=tests,
                summary=last_summary,
                escalation_reason=outcome.status,
            )

        if request.mode == RunMode.READ_ONLY:
            return _WorkerLoopResult(
                attempts=attempts,
                tests=[],
                summary=last_summary,
                escalation_reason=None,
            )

        record_event("tests-started", attempt=attempts)
        tests = _run_all_tests(request.test_commands, cwd, config)
        if any(t["timeout"] for t in tests):
            record_event("tests-failed", reason="test-timeout")
            return _WorkerLoopResult(
                attempts=attempts,
                tests=tests,
                summary="an executor-run test timed out",
                escalation_reason="test-timeout",
            )
        if any(t.get("error") == "test-launch-failed" for t in tests):
            record_event("tests-failed", reason="test-launch-failed")
            return _WorkerLoopResult(
                attempts=attempts,
                tests=tests,
                summary="an executor-run test command could not be launched",
                escalation_reason="test-launch-failed",
            )
        if tests and all(t["exit_code"] == 0 for t in tests):
            record_event("tests-passed")
            return _WorkerLoopResult(
                attempts=attempts,
                tests=tests,
                summary=last_summary,
                escalation_reason=None,
            )

        record_event("tests-failed", reason="tests-failing")

        if attempts > config.correction_limit:
            return _WorkerLoopResult(
                attempts=attempts,
                tests=tests,
                summary=last_summary,
                escalation_reason=None,
            )

        record_event("correction-started", attempt=attempts + 1)
        prompt = _build_correction_prompt(
            request, config, tests, last_summary
        )

    return _WorkerLoopResult(
        attempts=attempts,
        tests=tests,
        summary=last_summary,
        escalation_reason=None,
    )


def _invoke_worker(
    config: RouterConfig,
    prompt: str,
    cwd: Path,
    mode: RunMode,
    on_child_start: Callable[[int], None] | None = None,
) -> _WorkerOutcome:
    """Invoke Claude with the bounded argv contract; never raises.

    The worker runs in its own process session (``start_new_session``) so a
    later ``cancel`` can terminate exactly that process group. External
    behavior (argv, stdin prompt, JSON stdout contract, timeout handling)
    matches the earlier ``subprocess.run`` implementation.
    """
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
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            shell=False,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return _WorkerOutcome(
            status="worker-launch-failed",
            summary=f"worker could not be launched: {exc}",
        )

    if on_child_start is not None:
        try:
            on_child_start(proc.pid)
        except Exception:  # noqa: BLE001 - bookkeeping must not kill the run
            pass

    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=config.timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        return _WorkerOutcome(
            status="worker-timeout",
            summary=f"worker timed out after {config.timeout_seconds} seconds",
        )
    except OSError as exc:
        return _WorkerOutcome(
            status="worker-launch-failed",
            summary=f"worker could not be launched: {exc}",
        )

    if proc.returncode != 0:
        return _WorkerOutcome(
            status=_classify_worker_failure(stdout, stderr),
            summary=_bounded_stderr(stderr),
        )

    summary = _parse_worker_output(stdout)
    if summary is None:
        return _WorkerOutcome(
            status="worker-output-invalid",
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


def _append_summary(result: RunResult, detail: str) -> None:
    result.summary = f"{result.summary}; {detail}" if result.summary else detail


def _finish_result(
    config: RouterConfig,
    run_id: str,
    request: TaskRequest,
    result: RunResult,
    writer: EvidenceWriter,
    metadata: dict[str, Any],
) -> RunResult:
    """Persist the full evidence set, then write the manifest last."""
    finished_at = utc_timestamp()
    metadata.update(
        {
            "finished_at": finished_at,
            "duration_ms": _duration_ms(metadata.get("created_at"), finished_at),
            "attempts": result.attempts,
            "changed_files": list(result.changed_files),
            "diff_lines": result.diff_lines,
            "final_status": result.status,
            "escalation_reason": result.escalation_reason,
        }
    )
    if result.commit:
        metadata["worker_commit"] = result.commit

    try:
        if result.status == "escalated":
            writer.append_event("escalated", reason=result.escalation_reason)
        elif result.status == "ready-for-review":
            writer.append_event("ready-for-review")
        else:
            writer.append_event(result.status)
        writer.write_metadata(metadata)
        writer.write_tests(result.tests)
        writer.write_result(result.to_dict())
        writer.finalize_manifest()
    except (OSError, TypeError, ValueError) as exc:
        _set_escalation(result, "evidence-write-failed", str(exc))
    return result


def _duration_ms(started_at: str | None, finished_at: str) -> int:
    """Whole-millisecond distance between two UTC evidence timestamps."""
    if not started_at:
        return 0
    started = parse_utc_timestamp(started_at)
    finished = parse_utc_timestamp(finished_at)
    return max(0, int((finished - started).total_seconds() * 1000))


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


def _bounded_stderr(stderr: str | None) -> str:
    return _truncate(stderr or "", 200)


def _classify_worker_failure(stdout: str | None, stderr: str | None) -> str:
    diagnostic = f"{stdout or ''}\n{stderr or ''}".lower()
    if "max_turns_reached" in diagnostic or "turn limit" in diagnostic:
        return "worker-turn-limit"
    permission_markers = (
        "requested permissions",
        "permission denied",
        "requires approval",
        "not granted",
        "was blocked",
    )
    if any(marker in diagnostic for marker in permission_markers):
        return "worker-permission-denied"
    provider_markers = (
        "connection refused",
        "could not connect",
        "unable to connect",
        "authentication failed",
        "unauthorized",
        "rate limit",
        "service unavailable",
        "overloaded",
    )
    if any(marker in diagnostic for marker in provider_markers):
        return "provider-unreachable"
    if not (stdout or "").strip() and "claude-code:unrecognized_model" in diagnostic:
        return "worker-output-invalid"
    return "worker-cli-failed"


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _subprocess_summary(exc: subprocess.CalledProcessError | OSError) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = _timeout_text(exc.stderr).strip()
        return stderr or f"command failed with exit code {exc.returncode}"
    return str(exc)


class _PolicyPathDenied(Exception):
    def __init__(self, paths: tuple[str, ...]) -> None:
        super().__init__(", ".join(paths))
        self.paths = paths


def _base_policy_from(config: RouterConfig):
    from .policy import RouterPolicy

    return RouterPolicy(
        max_turns=config.max_turns,
        timeout_seconds=config.timeout_seconds,
        max_changed_files=config.max_changed_files,
        max_diff_lines=config.max_diff_lines,
    )


def _file_under_any(changed: str, denies: tuple[str, ...]) -> bool:
    from pathlib import PurePosixPath

    changed_parts = PurePosixPath(changed).parts
    for deny in denies:
        dparts = PurePosixPath(deny).parts
        if changed_parts[: len(dparts)] == dparts:
            return True
    return False


def _scope_hits_denies(scope: str, denies: tuple[str, ...]) -> bool:
    """True when a task scope sits INSIDE a denied tree.

    A wider scope that merely *contains* a denied subtree stays legal;
    the post-run diff gate catches actual writes into it.
    """
    from pathlib import PurePosixPath

    sparts = PurePosixPath(scope).parts
    for deny in denies:
        dparts = PurePosixPath(deny).parts
        if sparts[: len(dparts)] == dparts:
            return True
    return False


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


def _resolve_head(cwd: Path) -> str | None:
    """Best-effort HEAD SHA lookup; ``None`` outside a Git repository."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
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


def _resolve_worker_command(command: str) -> str | None:
    if "/" in command and not Path(command).is_absolute():
        return None
    resolved = shutil.which(command)
    return str(Path(resolved).resolve()) if resolved else None


def _short_task(task: str) -> str:
    cleaned = " ".join(task.split())
    return cleaned[:60] if cleaned else "task"

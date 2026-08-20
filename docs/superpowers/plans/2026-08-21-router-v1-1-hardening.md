# Claude Worker Router V1.1 Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove avoidable Claude Code permission interruptions and close the read-only, path-scope, and structured-error gaps before merging.

**Architecture:** Keep Codex as the security owner. The Python executor selects a mode-specific Claude tool/permission policy, enforces Git and path boundaries after execution, runs tests itself, and returns precise escalation evidence. Claude Code remains provider-neutral and receives no Bash or provider-selection controls.

**Tech Stack:** Python 3.12 standard library, `unittest`, Git worktrees, Claude Code CLI 2.1.x.

**Spec:** `docs/superpowers/specs/2026-08-21-router-v1-1-hardening-design.md`

## Global Constraints

- CC Switch provider selection remains manual-only; never pass `--model`, `--settings`, or fallback configuration.
- Worker edits occur only in isolated Git worktrees.
- Worker subprocesses and tests use argv arrays with `shell=False`.
- Claude Code receives no Bash tool.
- Every production behavior change follows a witnessed red-green test cycle.
- Do not push or merge automatically.

---

### Task 1: Mode-specific permissions and read-only behavior

**Files:**
- Modify: `tests/fake_claude.py`
- Modify: `tests/test_executor_cli.py`
- Modify: `src/claude_worker_router/executor.py`

**Interfaces:**
- Consumes: `TaskRequest.mode`, `_invoke_worker`, `_run_worker_loop`.
- Produces: `_worker_policy(mode)` and a read-only result that cannot mutate files.

- [ ] Add a fake behavior that attempts a write only when `Edit` or `Write` is exposed.
- [ ] Add tests proving read-only argv contains `--safe-mode`, `dontAsk`, and read tools only; prove the repository remains unchanged and status is `read-only`.
- [ ] Add an edit-mode argv test proving `--safe-mode`, `acceptEdits`, matching `--tools` and `--allowedTools`, and no Bash/model/settings flag.
- [ ] Run the focused tests and confirm they fail because the current executor always uses edit tools and never returns `read-only`.
- [ ] Implement the smallest mode-specific policy and read-only control flow that makes them pass.
- [ ] Run the focused tests and the complete deterministic suite.

### Task 2: Enforced paths and no-change completion

**Files:**
- Modify: `tests/test_config_provider.py`
- Modify: `tests/test_executor_cli.py`
- Modify: `src/claude_worker_router/models.py`
- Modify: `src/claude_worker_router/git_workspace.py`
- Modify: `src/claude_worker_router/executor.py`

**Interfaces:**
- Consumes: `TaskRequest.allowed_paths`, `ChangeMeasure.files`.
- Produces: validated relative path prefixes and `GitWorkspace.measure_changes(..., allowed_paths=...)`.

- [ ] Add request-validation tests rejecting absolute and parent-traversal allowed paths.
- [ ] Add an executor test where the worker changes a file outside the allowed prefix and assert `path-scope-exceeded`, no commit, and unchanged main checkout.
- [ ] Add an executor test where tests pass with no worker changes and assert `ready-for-review` with `commit = null`.
- [ ] Run the focused tests and confirm the expected validation, scope, and empty-commit failures.
- [ ] Implement path normalization, prefix enforcement, and empty-change commit skipping.
- [ ] Run the focused tests and the complete deterministic suite.

### Task 3: Structured failure handling and lean execution

**Files:**
- Modify: `tests/test_executor_cli.py`
- Modify: `tests/test_git_workspace.py`
- Modify: `src/claude_worker_router/executor.py`
- Modify: `src/claude_worker_router/git_workspace.py`

**Interfaces:**
- Consumes: Claude return code/stdout/stderr, `subprocess.TimeoutExpired`, Git subprocess failures.
- Produces: precise escalation reasons and retained JSON evidence for every handled failure.

- [ ] Add fake behaviors for permission denial, turn exhaustion, malformed output, and no change.
- [ ] Add tests for `worker-permission-denied`, `worker-turn-limit`, `worker-output-invalid`, `worker-timeout`, `test-timeout`, and worktree/commit Git failures.
- [ ] Run the focused tests and confirm each currently fails through a wrong reason or uncaught exception.
- [ ] Implement a single failure classifier, timeout-safe test records, and bounded Git exception handling.
- [ ] Make the worker prompt require reading applicable project instructions and remove repeated process prose.
- [ ] Run the focused tests and the complete deterministic suite.

### Task 4: Skill guidance, installed validation, and acceptance

**Files:**
- Modify: `skill/SKILL.md`
- Modify: `VERIFICATION.md`
- Modify: `tests/live/run_smoke_test.sh`

**Interfaces:**
- Consumes: the hardened executor and installed skill symlink.
- Produces: concise user-facing routing guidance and current acceptance evidence.

- [ ] Update the skill to forbid direct implementation calls to `claude -p`, reserve outside-worktree evidence handling for Codex, and describe read-only versus edit routes.
- [ ] Update live verification to check exact run records without asking Claude to inspect paths outside its working directory.
- [ ] Validate both source and installed skill paths.
- [ ] Run the full deterministic suite and `git diff --check`.
- [ ] Run one bounded live read-only call and one bounded one-file edit call with the current manually selected provider; confirm no permission denial, no hook context in safe mode, unchanged main checkout, and redacted evidence.
- [ ] Commit the V1.1 implementation and verification artifacts locally; do not push.
- [ ] Request a read-only Claude Code review with only the exact diff and in-repository requirements available.

### Task 5: Post-review hardening

**Files:**
- Modify: `src/claude_worker_router/models.py`
- Modify: `src/claude_worker_router/git_workspace.py`
- Modify: `src/claude_worker_router/executor.py`
- Modify: `tests/test_config_provider.py`
- Modify: `tests/test_executor_cli.py`
- Modify: `tests/fake_claude.py`

- [ ] Require a non-empty `allowed_paths` list for every edit request and
  reject raw empty or dot path components before normalization.
- [ ] Measure edit scope and re-check the provider fingerprint after failed
  worker or test outcomes, not only after successful runs.
- [ ] Convert Git measurement failures into retained structured evidence.
- [ ] Run approved tests with a minimal non-secret environment allowlist and
  no Python bytecode cache generation.
- [ ] Distinguish known provider failures, invalid third-party-model output,
  and unknown non-zero Claude CLI failures.
- [ ] Re-run the full deterministic suite and live read-only/edit acceptance.

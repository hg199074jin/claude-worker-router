# Claude Worker Router V1.2–V2.x Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按任务逐项实施。每个 Task 都必须独立完成测试、复核和提交；不得一次性实现多个版本。

**Goal:** 在保持 Codex 主导、Claude Code 单 Worker Runtime、人工 Provider 选择和显式 review/integration 原则不变的前提下，把 Claude Worker Router 从当前 V1.1 逐步演进为可靠、可管理、可治理、最终可扩展到多 Worker 和外部 Control Plane 的受控执行层。

**Architecture:** V1.2–V1.3 只强化现有单 Worker 执行内核；V1.4 才引入持久化任务状态；V1.5 才允许同 Provider 下有界并发；V2.0 才引入不可变 Worker Profile；V2.x 才提供 MCP/HTTP Control Plane。任何阶段都不得把 Router 变成第二个 Planner 或 Multi-Agent GUI。

**Tech Stack:** Python 3.12+、标准库、`argparse`、`dataclasses`、`tomllib`、`subprocess`、Git、SQLite（V1.4 起使用标准库 `sqlite3`）、Claude Code CLI、`unittest`。

**Spec:** `docs/superpowers/specs/2026-08-26-router-v1-2-to-v2x-evolution-design.md`

---

# 1. Global Constraints

以下约束适用于全部版本和全部 Task。

- Codex 始终负责规划、架构、风险判断、任务拆解和最终复核。
- V1.x 的唯一 Worker Runtime 为 Claude Code。
- V1.x Provider 始终来自当前 CC Switch 配置。
- Router 不修改 `~/.claude/settings.json`。
- Router 不自动切换 GLM、MiniMax、Claude 或其他 Provider。
- Router 不进行自动 Provider fallback。
- Worker 编辑模式继续禁止 Bash。
- Worker 只读模式不得获得 Edit/Write。
- Git、测试、evidence、integration 由 Router 执行。
- Worker 修改必须进入独立 worktree。
- `allowed_paths` 必须继续作为执行层真实限制。
- integration 必须显式执行。
- escalation 不得自动 integrate。
- Provider fingerprint mismatch 不得自动 integrate。
- V1.2–V1.5 必须兼容现有无子命令 stdin JSON 调用方式。
- 不引入第三方运行依赖，除非某一后续设计明确批准。
- 所有 subprocess 继续使用 argv list、`shell=False`。
- Evidence 中不得记录 API Token、Authorization Header 或 Secret。
- Roadmap 版本 V1.2/V1.3 等不自动等同于 Python package SemVer；`pyproject.toml` 版本号修改需要单独批准。
- 每个 Task 先写失败测试，再写实现。
- 每个 Task 单独 commit。
- 开始下一版本前必须完成上一版本全部验收 Gate。

---

# 2. 总体实施顺序

```text
V1.2 Operational Hardening
        │
        ▼
V1.2 Release Gate
        │
        ▼
V1.3 Policy Hardening
        │
        ▼
V1.3 Release Gate
        │
        ▼
V1.4 Run Management
        │
        ▼
真实使用数据证明存在排队需求
        │
        ▼
V1.5 Bounded Concurrency
        │
        ▼
真实使用证明需要多 Provider 同时运行
        │
        ▼
V2.0 Multi-Worker
        │
        ▼
真实客户端集成需求
        │
        ▼
V2.x Control Plane
```

禁止：

```text
V1.2
 ↓
一次 PR
 ↓
V2.x
```

---

# Part I — V1.2 Operational Hardening

# 3. V1.2 目标

V1.2 不增加新的 Agent 智能。

交付目标：

> Router 可以长期日常运行，环境可诊断、run 可查询、证据完整、Git 生命周期明确、异常 worktree 可管理，并对 symlink 与 binary 风险 fail closed。

---

# Task 1：重构 CLI Dispatch，同时保持 Legacy stdin 模式

**Files:**

- Modify: `src/claude_worker_router/cli.py`
- Create: `tests/test_cli_commands.py`
- Existing regression: `tests/test_executor_cli.py`

**Produces:**

```python
def main(argv: list[str] | None = None) -> int
```

继续保持。

新增内部：

```python
def _run_legacy(config_path: Path) -> int
def _dispatch_command(args: argparse.Namespace) -> int
```

## 实施步骤

- [ ] **Step 1：写 Legacy compatibility 测试**

验证：

```bash
printf '{...}' | claude-worker-router
```

无 subcommand 时仍执行原有路径。

- [ ] **Step 2：写 doctor/list/show 未实现测试**

例如：

```python
self.assertNotEqual(main(["doctor"]), 0)
```

先证明当前 CLI 尚无这些接口。

- [ ] **Step 3：修改 argparse**

目标结构：

```text
claude-worker-router
claude-worker-router doctor
claude-worker-router list
claude-worker-router show RUN_ID
claude-worker-router integrate RUN_ID
claude-worker-router cleanup RUN_ID
```

无 command：

```text
legacy stdin mode
```

有 command：

```text
subcommand mode
```

- [ ] **Step 4：保证 `--config` 同时适用于 legacy 与 subcommand**

例如：

```bash
claude-worker-router --config /x/config.toml doctor
```

- [ ] **Step 5：运行**

```bash
uv run --python 3.12 python -m unittest tests.test_cli_commands -v
uv run --python 3.12 python -m unittest tests.test_executor_cli -v
```

- [ ] **Step 6：提交**

```bash
git add src/claude_worker_router/cli.py tests/test_cli_commands.py
git commit -m "refactor: add backward-compatible CLI command dispatch"
```

---

# Task 2：建立 Evidence Writer

**Files:**

- Create: `src/claude_worker_router/evidence.py`
- Modify: `src/claude_worker_router/executor.py`
- Modify: `src/claude_worker_router/models.py`
- Create: `tests/test_evidence.py`

**Produces:**

```python
class EvidenceWriter
```

至少提供：

```python
create_run(...)
append_event(...)
write_tests(...)
write_diff(...)
write_result(...)
finalize_manifest(...)
```

---

## Evidence 目录

```text
RUN_ID/
├── request.json
├── result.json
├── metadata.json
├── tests.json
├── diff.patch
├── events.jsonl
└── evidence_manifest.json
```

---

## metadata schema

至少：

```python
{
    "schema_version": 1,
    "run_id": "...",
    "router_version": "...",

    "created_at": "...",
    "worker_started_at": "...",
    "worker_finished_at": "...",
    "finished_at": "...",
    "duration_ms": 0,

    "repository": "...",
    "repository_realpath": "...",

    "base_branch": "...",
    "base_sha": "...",

    "worker_branch": "...",
    "worker_commit": "...",

    "worktree": "...",
    "mode": "edit",

    "provider": {
        "endpoint_host": "...",
        "model": "...",
        "fingerprint": "..."
    },

    "changed_files": [],
    "diff_lines": 0,
    "attempts": 0,

    "final_status": "...",
    "escalation_reason": None
}
```

---

## events.jsonl

每行一个 JSON object：

```python
{
    "event": "worker-started",
    "timestamp": "...",
    "attempt": 1
}
```

必须 append-only。

---

## 实施步骤

- [ ] 写 `test_creates_full_run_directory`
- [ ] 写 `test_events_are_append_only_jsonl`
- [ ] 写 `test_metadata_contains_no_provider_credentials`
- [ ] 写 `test_manifest_contains_sha256_for_all_evidence_files`
- [ ] 写 `test_atomic_json_write_survives_replacement`
- [ ] 实现 `EvidenceWriter`
- [ ] 将 executor 原来的 `_write_records()` 迁移到 EvidenceWriter
- [ ] 保留旧 `request.json/result.json`，不得破坏兼容
- [ ] 所有事件使用 UTC ISO-8601 timestamp
- [ ] manifest 最后写入

运行：

```bash
uv run --python 3.12 python -m unittest tests.test_evidence -v
uv run --python 3.12 python -m unittest discover -v
```

提交：

```bash
git commit -am "feat: add structured run evidence"
```

---

# Task 3：Git Base Identity

**Files:**

- Modify: `src/claude_worker_router/git_workspace.py`
- Modify: `src/claude_worker_router/models.py`
- Modify: `src/claude_worker_router/executor.py`
- Modify: `tests/test_git_workspace.py`

**Produces:**

```python
@dataclass
class GitWorkspace:
    repository: Path
    path: Path
    branch: str
    run_id: str
    base_branch: str
    base_sha: str
```

新增：

```python
def current_head(repository: Path) -> str
def current_branch(repository: Path) -> str
```

---

## 实施步骤

- [ ] 测试 worktree 创建时保存准确 `base_sha`
- [ ] 测试 `base_sha` 等于创建前 main HEAD
- [ ] 测试 Worker commit 后 `base_sha` 不改变
- [ ] 将 base SHA 写入 `RunResult` 或 metadata
- [ ] 将 branch identity 写入 evidence
- [ ] 保持原有 DirtyCheckoutError 行为

运行：

```bash
uv run --python 3.12 python -m unittest tests.test_git_workspace -v
```

提交：

```bash
git commit -am "feat: record immutable git base identity"
```

---

# Task 4：Doctor

**Files:**

- Create: `src/claude_worker_router/doctor.py`
- Modify: `src/claude_worker_router/cli.py`
- Create: `tests/test_doctor.py`

**Produces:**

```python
@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str
```

```python
def run_doctor(config: RouterConfig, repository: Path | None = None) -> list[DoctorCheck]
```

---

## 检查项

### Runtime

```text
router config
python
git
claude executable
```

### Provider

```text
settings readable
endpoint host
model
fingerprint
```

### Storage

```text
run_records writable
```

### Tests

对：

```text
allowed_test_binaries
```

执行 `shutil.which()`。

### Repository mode

```bash
claude-worker-router doctor --repo .
```

增加：

```text
git repository
repo root
branch
HEAD SHA
clean status
worktree support
stale router worktree summary
```

---

## Exit code

```text
0 = READY
1 = READY WITH WARNINGS
2 = NOT READY
```

不得输出 Secret。

---

## 测试

- [ ] valid config → READY
- [ ] missing Claude executable → NOT READY
- [ ] malformed provider config → NOT READY
- [ ] missing optional allowed test binary → warning
- [ ] dirty repo → warning，而不是全局 doctor failure
- [ ] JSON output 模式可机器读取：

```bash
claude-worker-router doctor --json
```

提交：

```bash
git commit -am "feat: add router doctor diagnostics"
```

---

# Task 5：Run Repository / list / show

**Files:**

- Create: `src/claude_worker_router/run_store.py`
- Modify: `src/claude_worker_router/cli.py`
- Create: `tests/test_run_store.py`
- Modify: `tests/test_cli_commands.py`

**Produces:**

```python
class RunStore:
    def list_runs(...)
    def load_run(run_id: str)
    def run_exists(run_id: str)
```

---

## list

支持：

```bash
claude-worker-router list
claude-worker-router list --repo /repo
claude-worker-router list --status ready-for-review
claude-worker-router list --limit 20
claude-worker-router list --json
```

排序：

```text
created_at DESC
```

---

## show

```bash
claude-worker-router show RUN_ID
claude-worker-router show RUN_ID --json
```

人类模式显示：

```text
Run
Task
Repository
Base SHA
Provider
Attempts
Changed Files
Tests
Diff
Outcome
Escalation
Integration status
```

---

## 安全规则

`RUN_ID`：

- 不允许 `/`
- 不允许 `..`
- 必须解析在 `run_records` 内部

防止：

```text
show ../../secret
```

---

## 测试

- [ ] 按时间倒序
- [ ] repo filter
- [ ] status filter
- [ ] malformed run ignored with warning
- [ ] traversal run id rejected
- [ ] show missing run 返回 exit 2
- [ ] JSON 输出稳定

提交：

```bash
git commit -am "feat: add run history inspection"
```

---

# Task 6：Symlink Safety

**Files:**

- Create: `src/claude_worker_router/safety.py`
- Modify: `src/claude_worker_router/executor.py`
- Create: `tests/test_safety.py`

**Produces:**

```python
class ExternalSymlinkError(RuntimeError): ...
```

```python
def validate_symlinks(
    repository: Path,
    allowed_paths: tuple[str, ...],
    mode: RunMode,
) -> None
```

---

## 算法

仅检查 Git tracked symlink：

```bash
git ls-files -s
```

识别 mode：

```text
120000
```

对每个 symlink：

1. 读取 link text；
2. 计算 logical target；
3. resolve；
4. 检查是否逃逸 repository/worktree；
5. edit 模式进一步检查 relevant allowed path。

---

## Fail Closed

以下拒绝：

```text
outside repository
broken link
cycle
resolution error
```

返回：

```text
external-symlink-denied
```

---

## 测试

- [ ] internal symlink allowed
- [ ] symlink outside repo denied
- [ ] nested external symlink denied
- [ ] symlink cycle denied
- [ ] read-only run 也不允许外部逃逸
- [ ] Worker 未被调用即可拒绝

提交：

```bash
git commit -am "feat: reject unsafe worker symlinks"
```

---

# Task 7：Binary Safety

**Files:**

- Modify: `src/claude_worker_router/safety.py`
- Modify: `src/claude_worker_router/git_workspace.py`
- Modify: `src/claude_worker_router/executor.py`
- Modify: `src/claude_worker_router/config.py`
- Modify: `src/claude_worker_router/models.py`
- Modify: `config.example.toml`
- Create/Modify: `tests/test_safety.py`

---

## V1.2 Policy

配置：

```toml
binary_edit_policy = "deny"
```

如果字段不存在：

```text
default = deny
```

V1.2 只接受：

```text
deny
```

暂不开放 allow。

这是为 V1.3 Policy 留接口，而不是提前开放。

---

## Detection

比较 base 与 worker 状态：

```bash
git diff --numstat
```

任一 added/deleted 为：

```text
-
```

视为 binary。

对 untracked file：

使用 Git：

```bash
git diff --no-index --numstat /dev/null FILE
```

不得仅通过文件扩展名判断。

---

## Outcome

```text
binary-change-denied
```

必须优先于：

```text
scope-exceeded
```

因为 binary diff line 无法正常计量。

---

## 测试

- [ ] tracked binary 修改拒绝
- [ ] binary 新增拒绝
- [ ] binary 删除拒绝
- [ ] text 正常工作
- [ ] binary 不能因为 diff_lines=0 绕过 budget
- [ ] result/evidence 记录 offending path

提交：

```bash
git commit -am "feat: deny worker binary changes by default"
```

---

# Task 8：Integration Service

**Files:**

- Create: `src/claude_worker_router/integration.py`
- Modify: `src/claude_worker_router/git_workspace.py`
- Modify: `src/claude_worker_router/cli.py`
- Create: `tests/test_integration.py`

**Produces:**

```python
class IntegrationError(RuntimeError):
    reason: str
```

```python
def integrate_run(run_id: str, config: RouterConfig) -> str
```

---

## Preflight

顺序必须固定：

1. load evidence；
2. mode == edit；
3. status == ready-for-review；
4. worker commit 非空；
5. repository identity 一致；
6. main checkout clean；
7. current HEAD == base_sha；
8. worker commit 属于 worker branch；
9. evidence manifest valid；
10. tests passed；
11. scope/binary/symlink escalation 不存在。

---

## Integration

仅：

```bash
git merge --ff-only WORKER_COMMIT
```

不得：

```text
rebase
force
merge --no-ff
auto conflict resolution
```

---

## Base divergence

返回：

```text
integration-base-diverged
```

不修改 main。

---

## Successful integration

记录 event：

```text
integration-started
integration-completed
```

更新 metadata：

```text
integrated_at
integrated_sha
```

---

## 测试

- [ ] valid fast-forward integrates
- [ ] dirty main rejected
- [ ] moved HEAD rejected
- [ ] escalated run rejected
- [ ] failed tests rejected
- [ ] evidence mismatch rejected
- [ ] missing Worker branch rejected
- [ ] successful integrate leaves evidence intact

提交：

```bash
git commit -am "feat: add explicit verified integration"
```

---

# Task 9：Cleanup 与 Stale Worktree

**Files:**

- Create: `src/claude_worker_router/cleanup.py`
- Modify: `src/claude_worker_router/cli.py`
- Create: `tests/test_cleanup.py`

**Produces:**

```python
def cleanup_run(
    run_id: str,
    *,
    discard: bool = False,
) -> None
```

```python
def find_stale_runs(...)
```

---

## 单 run cleanup

```bash
claude-worker-router cleanup RUN_ID
```

默认允许：

```text
integrated
no changes
already discarded
```

若：

```text
ready-for-review
```

则拒绝。

只有：

```bash
claude-worker-router cleanup RUN_ID --discard
```

允许主动放弃。

---

## Stale

```bash
claude-worker-router cleanup --stale
```

默认：

```text
168 hours
```

只自动清：

- integrated；
- empty orphan；
- invalid Git metadata with no unintegrated changes。

仅报告：

- ready-for-review；
- escalated + changes；
- evidence mismatch。

---

## 测试

- [ ] integrated cleanup
- [ ] unintegrated refusal
- [ ] explicit discard
- [ ] evidence 永远保留
- [ ] stale threshold
- [ ] orphan worktree
- [ ] branch already missing
- [ ] repeated cleanup is idempotent

提交：

```bash
git commit -am "feat: manage worker worktree lifecycle"
```

---

# Task 10：V1.2 Skill、README 与 Verification

**Files:**

- Modify: `skill/SKILL.md`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `VERIFICATION.md`
- Modify: `config.example.toml`
- Create: `docs/superpowers/specs/...` if not yet committed

---

## Skill 增加

Codex 在：

```text
ready-for-review
```

之后：

1. 使用 show/evidence；
2. review；
3. 用户或 Codex 明确批准；
4. integrate；
5. cleanup。

不得直接：

```bash
git merge
```

绕过 Router integration。

---

## V1.2 Verification

执行：

```bash
uv run --python 3.12 python -m unittest discover -v
```

然后：

```bash
claude-worker-router doctor
```

然后 bounded live：

### Read-only smoke

检查：

```text
read-only
provider fingerprint stable
full evidence
```

### One-file edit smoke

检查：

```text
isolated worktree
one changed text file
tests pass
ready-for-review
show works
integrate works
cleanup works
```

---

# 11. V1.2 Release Gate

只有以下全部满足才允许进入 V1.3：

- [ ] 完整 deterministic test suite 通过
- [ ] Legacy stdin API 无回归
- [ ] doctor 可用于真实 Mac 环境
- [ ] show/list 可用
- [ ] metadata 完整
- [ ] evidence manifest 可验证
- [ ] base SHA 被记录
- [ ] base drift 阻止 integrate
- [ ] integration 只能显式执行
- [ ] cleanup 不删除 evidence
- [ ] stale worktree 可识别
- [ ] external symlink fail closed
- [ ] binary edit fail closed
- [ ] bounded live edit 通过
- [ ] `VERIFICATION.md` 已更新

---

# Part II — V1.3 Policy Hardening

> **不得与 V1.2 同一实现批次开发。**

# Task 11：Policy Data Model

**Files:**

- Create: `src/claude_worker_router/policy.py`
- Modify: `src/claude_worker_router/models.py`
- Create: `tests/test_policy.py`

定义：

```python
@dataclass(frozen=True)
class RouterPolicy:
    max_turns: int
    timeout_seconds: int
    max_changed_files: int
    max_diff_lines: int
    deny_paths: tuple[str, ...]
    sandbox_required: bool
```

定义：

```python
def merge_policy(
    global_policy: RouterPolicy,
    project_policy: RouterPolicy | None,
    request: TaskRequest,
) -> EffectivePolicy
```

数值：

```text
minimum wins
```

deny：

```text
union wins
```

boolean safety requirement：

```text
true wins
```

---

# Task 12：Global + Project Policy Loader

**Files:**

- Modify: `src/claude_worker_router/policy.py`
- Modify: `src/claude_worker_router/config.py`
- Create: `tests/test_policy_loader.py`

路径：

```text
~/.codex/model-router/policy.toml
<repo>/.claude-worker-router/policy.toml
```

要求：

```text
Project 只能收紧
```

如果 project 尝试放宽：

```text
policy-relaxation-rejected
```

不要静默 clamp。

---

# Task 13：deny_paths Enforcement

**Files:**

- Modify: `src/claude_worker_router/safety.py`
- Modify: `src/claude_worker_router/executor.py`
- Modify: `tests/test_safety.py`

内置 deny 至少：

```text
.git
.claude-worker-router
```

Effective deny：

```text
built-in ∪ global ∪ project
```

Request `allowed_paths` 与 deny 相交：

执行前直接拒绝。

---

# Task 14：Test Profiles

**Files:**

- Create: `src/claude_worker_router/test_profiles.py`
- Modify: `src/claude_worker_router/models.py`
- Modify: `src/claude_worker_router/executor.py`
- Create: `tests/test_test_profiles.py`

配置例：

```toml
[test_profiles.python-unit]
commands = [
  ["uv", "run", "--python", "3.12", "python", "-m", "unittest", "discover", "-v"]
]
exclusive = false
```

Task 可：

```json
{
  "test_profile": "python-unit"
}
```

V1.3 同时支持旧：

```text
test_commands
```

但：

```text
test_profile 与 test_commands 不得同时存在
```

---

# Task 15：Policy Fingerprint

Evidence 增加：

```text
global_policy_hash
project_policy_hash
effective_policy_hash
```

使用：

```text
SHA-256 canonical JSON
```

不得把路径之外的 Secret 写入 hash source。

---

# Task 16：macOS Sandbox Feasibility Spike

**本 Task 不直接改变默认生产执行路径。**

**Files:**

- Create: `docs/superpowers/research/2026-xx-xx-macos-sandbox-feasibility.md`
- Optional experimental module:
  `src/claude_worker_router/platform/macos_sandbox.py`
- Tests:
  `tests/test_macos_sandbox.py`

必须验证：

1. Claude Code 能在限制环境中启动；
2. worktree 可写；
3. repository 外部敏感路径不可写；
4. Provider 网络正常；
5. Claude configuration 仍能读到必要内容；
6. credentials 不被复制到临时文件。

研究结果只能是：

```text
SUPPORTED
```

或：

```text
NOT READY
```

如果 NOT READY：

V1.3 发布时：

```text
sandbox_required = false
```

可继续。

但 global policy 设置：

```text
sandbox_required = true
```

时必须 fail closed：

```text
sandbox-unavailable
```

---

# 12. V1.3 Release Gate

- [ ] Global policy 可以加载
- [ ] Project policy 可以加载
- [ ] Project 无法放宽 Global
- [ ] deny path 有效
- [ ] test profile 有效
- [ ] old test_commands 仍兼容
- [ ] policy fingerprint 完整
- [ ] Worker 无法编辑 policy directory
- [ ] sandbox research 已形成正式结论
- [ ] V1.2 全部 regression tests 通过

---

# Part III — V1.4 Run Management

## 进入 Gate

只有实际开始出现多个等待任务，才实施本阶段。

---

# Task 17：Lifecycle Model

**Files:**

- Modify: `src/claude_worker_router/models.py`
- Create: `tests/test_run_state.py`

新增：

```python
class RunLifecycle(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    READY_FOR_REVIEW = "ready-for-review"
    INTEGRATED = "integrated"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
```

Execution outcome 与 lifecycle 分离。

---

# Task 18：SQLite State Store

**Files:**

- Create: `src/claude_worker_router/state_store.py`
- Create: `tests/test_state_store.py`

使用标准库：

```python
sqlite3
```

数据库：

```text
~/.codex/model-router/state.db
```

首版 schema：

```sql
runs(
    run_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    outcome TEXT,
    priority INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    parent_run_id TEXT,
    evidence_path TEXT NOT NULL
)
```

Migration：

使用：

```text
PRAGMA user_version
```

不要引入 Alembic。

---

# Task 19：submit / queue / drain

CLI：

```bash
claude-worker-router submit
claude-worker-router queue
claude-worker-router drain
```

`submit`：

stdin JSON → validation → pending → return run_id。

`drain`：

```text
priority DESC
created_at ASC
```

单 Worker 顺序执行。

不得并发。

---

# Task 20：Crash Recovery

启动/doctor/drain 时检查：

```text
lifecycle=running
```

但本进程并无对应 active child：

转换：

```text
blocked
```

outcome：

```text
runner-interrupted
```

绝不自动重新执行。

---

# Task 21：Cancel

```bash
claude-worker-router cancel RUN_ID
```

### pending

直接 cancelled。

### running

终止 Claude child process。

因此 executor 在 V1.4 必须从单纯：

```python
subprocess.run()
```

逐步改为受控：

```python
subprocess.Popen()
```

但外部行为必须保持一致。

### ready-for-review

cancel 表示 discard intent。

worktree 不自动删。

---

# V1.4 Release Gate

- [ ] state DB restart 后存在
- [ ] pending queue 不丢
- [ ] 单 Worker drain 正常
- [ ] running crash → blocked
- [ ] 无 silent retry
- [ ] cancel 完整可追踪
- [ ] evidence 与 SQLite 可互相校验

---

# Part IV — V1.5 Bounded Concurrency

## 进入 Gate

统计最近至少：

```text
50
```

个 queued run。

只有：

```text
>= 20%
```

存在实际可并行任务并形成明显排队影响，才实施。

---

# Task 22：Path Conflict Engine

**Files:**

- Create: `src/claude_worker_router/scheduler.py`
- Create: `tests/test_scheduler.py`

接口：

```python
def paths_conflict(
    left: tuple[str, ...],
    right: tuple[str, ...],
) -> bool
```

规则：

```text
same path → conflict
ancestor → conflict
descendant → conflict
otherwise disjoint
```

---

# Task 23：Provider Epoch

Scheduler 启动 batch：

```text
provider_epoch = current fingerprint
```

新 run dispatch 前：

重新读取 fingerprint。

若改变：

```text
停止 dispatch
```

已运行的不杀掉。

结束时仍按现有 fingerprint check 判定其结果。

---

# Task 24：Concurrency = 2

固定：

```text
MAX_CONCURRENCY = 2
```

本版本不要支持：

```text
--concurrency 20
```

可以配置：

```text
1 or 2
```

超过 2：

config validation fail。

---

# Task 25：Exclusive Tests

Test Profile 增加：

```toml
exclusive = true
```

Scheduler 遇到：

```text
exclusive test
```

必须等待：

```text
active_runs == 0
```

才启动。

运行期间不得启动另一个 run。

---

# Task 26：Repository Integration Lock

Worker execution 可并发。

但：

```text
integrate
```

按 repository 使用 exclusive lock。

建议 lock 文件：

```text
~/.codex/model-router/locks/<repo-hash>.lock
```

使用平台可用的文件锁实现。

不得同时修改同一 main checkout。

---

# V1.5 Release Gate

- [ ] concurrency 永远 <= 2
- [ ] overlap 自动串行
- [ ] disjoint path 可以并行
- [ ] Provider change 停止新 dispatch
- [ ] exclusive tests 有效
- [ ] integration 串行
- [ ] 两个 worktree 不污染
- [ ] crash recovery 仍正确
- [ ] 不自动切 Provider

---

# Part V — V2.0 Multi-Worker

## 进入 Gate

必须存在明确真实需求：

```text
需要 GLM + MiniMax + Claude 在同一时间段分别工作
```

且人工 CC Switch 已经成为瓶颈。

---

# Task 27：Worker Profile Schema

**Files:**

- Create: `src/claude_worker_router/profiles.py`
- Create: `tests/test_profiles.py`

模型：

```python
@dataclass(frozen=True)
class WorkerProfile:
    profile_id: str
    runtime: str
    command: str
    provider_config: Path
    model_identity: str
    credential_reference: str
    fingerprint: str
```

Credential reference 是：

> credential 的位置或 key identifier。

不是 credential value。

---

# Task 28：Immutable Profile Loader

配置：

```text
~/.codex/model-router/workers/*.toml
```

加载后 canonical fingerprint。

Run 开始后：

```text
profile fingerprint
```

不得变化。

变化：

```text
worker-profile-changed
```

---

# Task 29：Claude Code Profile Isolation

目标：

```text
glm
minimax
claude
```

各拥有独立 Claude configuration context。

Router 不修改：

```text
~/.claude/settings.json
```

每个 profile 必须能够独立启动 Claude Code。

这是 V2.0 的核心验收点。

---

# Task 30：Legacy CC Switch Profile

内置：

```text
cc-switch-current
```

继续工作。

但只允许：

```text
single provider mode
```

不得和另一不同 Provider profile 并行。

---

# Task 31：Explicit Profile Selection

TaskRequest V2：

```json
{
  "worker_profile": "minimax"
}
```

如果不写：

可以使用配置的默认 profile。

但 Router 不根据：

```text
price
failure
quality
rate limit
```

动态挑选。

---

# Task 32：Multi-Profile Scheduler

沿用 V1.5：

```text
path conflict
repo lock
policy
evidence
```

新增：

```text
profile identity
credential isolation
provider isolation
```

---

# Task 33：是否建立 WorkerBackend

此 Task 有明确判定条件：

如果 V2.0 全部 profile 仍然都是：

```text
Claude Code
```

则：

> **DO NOT implement WorkerBackend abstraction.**

只有已批准第二 Runtime，例如：

```text
Codex CLI
```

才创建：

```python
class WorkerBackend(Protocol):
    def preflight(...)
    def invoke(...)
    def fingerprint(...)
    def capabilities(...)
    def normalize_outcome(...)
    def terminate(...)
```

这不是待办项，而是明确 YAGNI Gate。

---

# V2.0 Release Gate

同时启动：

```text
GLM
MiniMax
Claude
```

至少验证：

- [ ] settings 独立
- [ ] credentials 独立
- [ ] fingerprints 独立
- [ ] evidence 身份正确
- [ ] 一个失败不影响另外两个
- [ ] 无全局 settings mutation
- [ ] 无自动 fallback
- [ ] path conflict 继续工作

---

# Part VI — V2.x Control Plane

## 进入 Gate

必须已有外部真实消费者，例如：

```text
AionUi
Orbit
手机
远程 PC
其他 Agent
```

---

# Task 34：Application Service

**Files:**

- Create: `src/claude_worker_router/service.py`
- Create: `tests/test_service.py`

必须先建立：

```python
class RouterService
```

统一：

```python
submit()
status()
cancel()
evidence()
approve()
```

CLI 先迁移成调用 `RouterService`。

然后 MCP / HTTP 才能调用同一 service。

不得：

```text
CLI 一套逻辑
HTTP 一套逻辑
MCP 一套逻辑
```

---

# Task 35：Idempotent Submit

Service：

```python
submit(request, idempotency_key=None)
```

state DB 增加唯一约束。

同一 key 重试：

返回：

```text
original run_id
```

不得重复运行 Worker。

---

# Task 36：HTTP Adapter

**Files:**

- Create: `src/claude_worker_router/http_server.py`
- Create: `tests/test_http_server.py`

默认：

```text
127.0.0.1
```

首版 API：

```text
POST /runs
GET  /runs/{id}
POST /runs/{id}/cancel
GET  /runs/{id}/evidence
POST /runs/{id}/approve
```

不得默认 bind：

```text
0.0.0.0
```

---

# Task 37：Remote Authentication

若启用非 loopback：

必须：

```text
access key
key rotation
audit log
rate limit
```

未配置认证：

Router 拒绝绑定非 loopback。

---

# Task 38：MCP Adapter

MCP tools：

```text
router_submit
router_status
router_cancel
router_evidence
router_approve
```

MCP：

不得获得：

```text
change_provider
change_policy
read_secret
arbitrary_shell
force_integrate
```

---

# Task 39：Approve Contract

所有入口：

```text
CLI
HTTP
MCP
```

最终必须调用同一个：

```python
RouterService.approve()
```

内部再调用：

```text
V1.2 integration safety checks
```

因此即使手机点击“Approve”：

```text
base diverged
```

仍然失败。

---

# V2.x Release Gate

- [ ] CLI 和 API 使用同一 service
- [ ] HTTP 默认 loopback
- [ ] remote 必须认证
- [ ] submit idempotent
- [ ] MCP 无 bypass
- [ ] approve 无 bypass
- [ ] Policy 始终生效
- [ ] Evidence 不泄漏 credential
- [ ] GUI 保持外部实现

---

# 13. 推荐目录最终形态

不要求 V1.2 一次重构成该目录；随功能自然演进。

```text
src/claude_worker_router/
│
├── __init__.py
├── cli.py
│
├── models.py
├── config.py
├── provider.py
│
├── executor.py
├── git_workspace.py
│
├── evidence.py
├── run_store.py
├── doctor.py
├── safety.py
├── integration.py
├── cleanup.py
│
├── policy.py
├── test_profiles.py
│
├── state_store.py
├── scheduler.py
│
├── profiles.py
│
├── service.py
├── http_server.py
└── mcp_server.py
```

核心原则：

> 文件按职责拆，不为了“架构漂亮”提前建立空目录和空抽象。

---

# 14. Test Suite 最终形态

```text
tests/
├── helpers.py
├── fake_claude.py
│
├── test_config_provider.py
├── test_executor_cli.py
├── test_git_workspace.py
│
├── test_cli_commands.py
├── test_evidence.py
├── test_doctor.py
├── test_run_store.py
├── test_safety.py
├── test_integration.py
├── test_cleanup.py
│
├── test_policy.py
├── test_policy_loader.py
├── test_test_profiles.py
├── test_macos_sandbox.py
│
├── test_run_state.py
├── test_state_store.py
├── test_scheduler.py
│
├── test_profiles.py
├── test_service.py
├── test_http_server.py
│
└── live/
```

---

# 15. 每个 Task 的固定完成流程

所有 Agent 必须使用：

```text
1. 阅读 Spec
2. 阅读相关现有代码
3. 写失败测试
4. 运行并确认失败原因正确
5. 写最小实现
6. 运行目标测试
7. 运行相关 regression tests
8. 自查 diff
9. commit
10. 进入下一个 Task
```

禁止：

```text
写完 10 个 Task
↓
最后统一跑测试
```

---

# 16. Commit Strategy

建议：

```text
refactor: add backward-compatible CLI command dispatch
feat: add structured run evidence
feat: record immutable git base identity
feat: add router doctor diagnostics
feat: add run history inspection
feat: reject unsafe worker symlinks
feat: deny worker binary changes by default
feat: add explicit verified integration
feat: manage worker worktree lifecycle
docs: document v1.2 operational workflow
```

每一 commit 应：

- 单一职责；
- 测试通过；
- 可单独 review；
- 可以 revert。

---

# 17. Agent 执行边界

本计划交给 Codex/Claude Code Agent 执行时：

## 可委派 Worker

例如：

```text
新增 DoctorCheck dataclass
增加具体 unittest
实现 JSON serializer
实现 RunStore filter
增加 path conflict function
```

## Codex 自己负责

以下不得交给廉价 Worker 独立决策：

```text
Policy precedence 设计
integration security boundary
sandbox security policy
credential isolation
Worker Profile architecture
HTTP authentication
MCP approval boundary
```

Worker 可以写受控实现。

架构决策仍由 Codex review。

---

# 18. 不允许 Agent 擅自增加的功能

实施过程中不得“顺便”增加：

- Web GUI；
- Electron；
- Tauri；
- React；
- Browser Automation；
- Office；
- Cron；
- 自动模型选择；
- 自动 Provider fallback；
- LLM 评分 Router；
- Vector DB；
- Project Memory；
- Redis；
- Postgres；
- Docker daemon；
- Celery；
- Kubernetes；
- 消息队列中间件；
- 复杂 Workflow DSL；
- DAG Planner；
- 第三方 Agent Framework。

任何上述需求必须重新写 Design Spec。

---

# 19. 最推荐的实际执行方式

当前只实施：

```text
V1.2
Tasks 1–10
```

完成后停止。

生成：

```text
V1.2 Verification Report
```

然后真实使用一段时间。

只有验证：

```text
Global/Project Policy
```

确实成为下一个主要问题，再启动 V1.3。

因此整个路线的正确执行方式不是：

```text
Implementation Plan
 ↓
全部执行
```

而是：

```text
Implementation Plan
 ↓
V1.2
 ↓
真实使用
 ↓
重新评估
 ↓
V1.3
 ↓
真实使用
 ↓
重新评估
 ↓
...
```

---

# 20. Definition of Done

Claude Worker Router 的一个版本只有同时满足下面三项才叫完成。

## Code Done

```text
功能实现
测试通过
无未处理异常
```

## Safety Done

```text
旧安全边界没有被削弱
没有新增隐式权限
没有静默 fallback
没有自动 integration
```

## Operational Done

```text
真实 bounded live test 成功
Evidence 可复核
失败场景可理解
README / Skill / Verification 已同步
```

只做到 Code Done：

> 不算 release ready。

---

# 21. 最终实施原则

整个演进过程始终遵循：

```text
Reliability
    ↓
Policy
    ↓
Management
    ↓
Concurrency
    ↓
Multi-Worker
    ↓
Control Plane
```

而不是：

```text
更多 Agent
    ↓
更多模型
    ↓
更多并发
    ↓
最后再补安全
```

最终 Router 应保持：

> **小而可靠的受控执行内核。**

Codex 负责思考。

Worker 负责执行。

Router 负责确保：

```text
任务边界正确
执行身份明确
修改范围受控
验证过程可信
结果可以复核
集成必须批准
失败能够接管
全过程有证据
```

这就是 V1.2 到 V2.x 的实施主线。
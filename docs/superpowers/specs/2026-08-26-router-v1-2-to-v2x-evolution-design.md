# Claude Worker Router V1.2–V2.x Evolution Design

**Status:** Proposed  
**Date:** 2026-08-26  
**Scope:** V1.2 Operational Hardening → V1.3 Policy Hardening → V1.4 Run Management → V1.5 Bounded Concurrency → V2.0 Multi-Worker → V2.x Control Plane

---

## 1. 文档目的

本文定义 Claude Worker Router 从当前 V1.1 向 V2.x 演进的总体架构、版本边界、安全原则和进入条件。

本路线的核心目标不是把 Router 建设成新的 Multi-Agent 平台，也不是复制 AionUi、Orbit、OpenAgentd 或 Orbital，而是继续强化 Router 当前最有价值的职责：

> **在 Codex 控制下，为 Claude Code Worker 提供边界明确、可回滚、可测试、可复核、可追踪的执行环境。**

整个演进过程遵循一个原则：

> **先解决可靠性，再解决规模；先解决执行治理，再解决多 Worker；只有真实需求出现以后才增加并发、Provider 隔离和控制面。**

---

# 2. 产品定位

Claude Worker Router 的长期定位为：

> **A governed worker execution layer for Codex.**

即：

> **面向 Codex 的受控 Worker 执行、验证与审计层。**

其职责不是替代 Codex，而是把 Codex 已经界定好的执行任务安全地下放给 Worker。

长期架构关系：

```text
User
  │
  ▼
Codex
Planner / Architect / Reviewer
  │
  │ structured task
  ▼
Claude Worker Router
  │
  ├─ Policy
  ├─ Workspace Isolation
  ├─ Scope Enforcement
  ├─ Verification
  ├─ Evidence
  ├─ Run Management
  └─ Scheduling
  │
  ▼
Worker Runtime
  │
  ▼
Claude Code
  │
  ▼
Provider / Model
```

在 V1.x 中：

```text
Worker Runtime = Claude Code
Provider = CC Switch 当前人工选择的 Provider
```

直到 V2.0 才允许改变这一前提。

---

# 3. 不变的核心原则

以下原则贯穿 V1.2 至 V2.x，除非未来单独形成新设计并明确推翻，否则不得改变。

## 3.1 Codex 始终是责任主体

Codex负责：

- 需求理解；
- 架构设计；
- 任务拆解；
- 风险识别；
- 是否委派；
- acceptance criteria；
- allowed paths；
- 测试选择；
- Worker 输出复核；
- 最终集成决策。

Worker负责：

- 范围受控的实现；
- 范围受控的分析；
- 根据测试失败结果进行有限 correction。

Router 不承担第二套完整设计职责。

---

## 3.2 Worker 不获得无限执行权限

V1.x 默认继续禁止 Worker 使用 Bash。

Worker 编辑模式原则上仅获得：

```text
Read
Glob
Grep
Edit
Write
```

只读模式仅获得：

```text
Read
Glob
Grep
```

Git、测试、版本检测、证据写入继续由 Router 执行。

---

## 3.3 Provider 不静默切换

V1.x：

```text
Provider Selection = Manual Only
```

Router：

- 不自动修改 CC Switch；
- 不自动 fallback；
- 不自动从 GLM 切 MiniMax；
- 不自动从 MiniMax 切 Claude；
- 不根据失败类型静默重试另一个模型。

Provider 失败后返回 Codex。

---

## 3.4 Worktree 不是安全沙箱

Git worktree 仅解决：

> Git 修改与主工作区隔离。

它不代表：

> 操作系统级安全隔离。

因此后续即使增加更多 Git 防护，也不得把 worktree 描述成 OS sandbox。

---

## 3.5 不自动集成

Worker 完成任务后：

```text
Worker
  ↓
Tests
  ↓
ready-for-review
  ↓
Codex review
  ↓
explicit approval
  ↓
integrate
```

任何 Provider mismatch、scope violation、test failure 或其他 escalation 均不得自动集成。

---

## 3.6 Evidence 永远优先于“智能”

Router 的执行必须能够回答：

```text
谁执行的？
执行什么？
从哪个 commit 开始？
允许修改什么？
实际修改了什么？
用了哪个 Provider？
跑了什么测试？
测试结果如何？
发生过哪些 correction？
为什么成功？
为什么失败？
最后是否集成？
```

如果某项“智能化能力”会削弱这些问题的可回答性，则不应加入。

---

# 4. 明确的长期 Non-goals

以下能力不属于当前路线目标：

- 自研大型桌面 GUI；
- 替代 AionUi、Orbit 等前端；
- Router 自己进行完整架构设计；
- Router 自己生成复杂项目计划；
- Worker 自动修改长期项目记忆；
- Worker 自动决定 Provider；
- 自动 fallback；
- 自动生产部署；
- 自动基础设施变更；
- 自动处理密钥；
- 无审批自动 merge；
- “全自动无人监管 Agent”；
- V1.x 支持十几个不同 Agent Runtime；
- 为了 Multi-Agent 而 Multi-Agent。

---

# 5. 总体版本路线

| 版本 | 核心目标 | 复杂度变化 |
|---|---|---|
| V1.2 | 每天使用稳定可靠 | 运维强化 |
| V1.3 | Policy 可配置且不可越权 | 治理强化 |
| V1.4 | 多任务可管理 | 状态与队列 |
| V1.5 | 有界并发 | 调度强化 |
| V2.0 | 多个独立 Worker Profile | Provider 隔离 |
| V2.x | 外部客户端控制 | Control Plane |

路线必须按顺序演进。

后一个版本不得成为前一个版本未完成问题的替代品。

---

# 6. V1.2 — Operational Hardening

## 6.1 目标

V1.2 不增加新的 Agent 能力。

目标只有一个：

> **让 Router 从“可以运行”变成“每天使用都足够稳定”。**

主要解决：

- 环境问题难排查；
- run 难查询；
- evidence 不够完整；
- worktree 生命周期不完整；
- Worker commit 与主分支变化缺乏明确关系；
- integration 缺少标准流程；
- stale worktree 缺少管理；
- symlink 可能突破目录边界；
- binary 文件无法被 diff line budget 有效衡量。

---

# 7. V1.2 CLI 设计

新增：

```text
claude-worker-router doctor
claude-worker-router list
claude-worker-router show RUN_ID
claude-worker-router integrate RUN_ID
claude-worker-router cleanup RUN_ID
claude-worker-router cleanup --stale
```

原有 stdin JSON 执行方式继续兼容。

不得因为 V1.2 引入 CLI 子命令而破坏已有 Codex Skill 调用方式。

---

# 8. doctor

## 8.1 目标

在 Worker 真正运行前，快速判断当前环境是否 READY。

示例：

```text
$ claude-worker-router doctor

Claude Worker Router Doctor

✓ Python runtime
✓ Router configuration
✓ git executable
✓ Claude Code executable
✓ Claude settings readable
✓ Provider fingerprint
✓ run-record directory writable
✓ Git worktree support
✓ configured test binaries

Provider:
  endpoint: api.example.com
  model: example-model

READY
```

---

## 8.2 doctor 必须检查

### Router

- config 可以解析；
- 必填配置存在；
- run records 目录可创建、可写；
- Router 版本可读取。

### Git

- `git` 可执行；
- Git 版本可获得；
- worktree 能力可用。

### Claude Code

- command 可解析成绝对 executable；
- executable 可运行；
- Claude 配置文件可读取。

### Provider

只读取非敏感信息：

```text
endpoint_host
model
default model mapping
fingerprint
```

不得输出：

- API Token；
- Authorization Header；
- 完整 Secret；
- 环境变量中的凭据。

### Tests

确认配置中的：

```text
allowed_test_binaries
```

是否可以解析。

不存在的 binary 应显示 Warning 或 Error。

---

## 8.3 Repository Doctor

允许：

```text
claude-worker-router doctor --repo /path/to/repo
```

增加：

- 是否为 Git repo；
- repository root；
- 当前 branch；
- HEAD SHA；
- checkout 是否 clean；
- Router worktree 目录是否存在异常残留。

V1.3 以后再增加 Project Policy 检查。

---

# 9. V1.2 Evidence Model

当前：

```text
run_id/
├── request.json
└── result.json
```

升级为：

```text
run_id/
├── request.json
├── result.json
├── metadata.json
├── tests.json
├── diff.patch
├── events.jsonl
└── evidence_manifest.json
```

---

# 10. metadata.json

至少记录：

```text
schema_version
run_id
router_version

created_at
worker_started_at
worker_finished_at
finished_at
duration_ms

repository
repository_realpath

base_branch
base_sha

worker_branch
worker_commit

worktree

mode

provider.endpoint_host
provider.model
provider.fingerprint

changed_files
diff_lines

attempts

final_status
escalation_reason
```

不得记录 credential。

---

# 11. events.jsonl

采用 append-only JSON Lines。

示例：

```json
{"event":"run-created","timestamp":"..."}
{"event":"preflight-started","timestamp":"..."}
{"event":"provider-verified","timestamp":"..."}
{"event":"worktree-created","timestamp":"..."}
{"event":"worker-started","timestamp":"...","attempt":1}
{"event":"worker-finished","timestamp":"...","attempt":1}
{"event":"tests-started","timestamp":"..."}
{"event":"tests-failed","timestamp":"..."}
{"event":"correction-started","timestamp":"...","attempt":2}
{"event":"worker-finished","timestamp":"...","attempt":2}
{"event":"tests-passed","timestamp":"..."}
{"event":"ready-for-review","timestamp":"..."}
```

V1.2 不要求记录 Claude 的完整内部思考过程。

Router 只记录：

> observable execution facts。

---

# 12. tests.json

保存每个 Router 执行的测试：

```text
argv
started_at
finished_at
duration_ms
exit_code
timeout
stdout
stderr
output_truncated
```

仍然受：

```text
test_output_limit_bytes
```

限制。

---

# 13. diff.patch

在 Worker 执行结束后保存最终 patch。

其作用：

- Codex review；
- 用户复核；
- cleanup 后仍能查看；
- stale worktree 删除后仍有修改证据。

对于 binary 文件不得仅依赖 diff.patch。

---

# 14. evidence_manifest.json

Router 完成 run 时计算 Evidence 文件 SHA-256。

例如：

```json
{
  "request.json": "...",
  "result.json": "...",
  "metadata.json": "...",
  "tests.json": "...",
  "diff.patch": "...",
  "events.jsonl": "..."
}
```

该机制主要防止：

- 意外损坏；
- 文件不一致；
- evidence 文件被部分覆盖。

它不宣称提供不可抵赖性，也不替代数字签名。

---

# 15. Base SHA

创建 worktree 时必须记录：

```text
base_branch
base_sha
```

例如：

```text
main
12ab34...
```

Worker 的所有修改均明确基于该 SHA。

---

# 16. integrate

新增：

```text
claude-worker-router integrate RUN_ID
```

## 16.1 integrate 前置条件

必须全部满足：

1. run 是 edit mode；
2. final result 为 `ready-for-review`；
3. Worker commit 存在；
4. 主 checkout clean；
5. 当前 repository identity 与 run 一致；
6. 当前 HEAD 等于 `base_sha`；
7. Worker branch/commit 与 evidence 一致；
8. Provider mismatch 不存在；
9. path scope violation 不存在；
10. tests 全部通过。

---

## 16.2 Base divergence

若：

```text
base_sha = A

Worker:
A → W

Main:
A → B → C
```

则：

```text
current HEAD != base_sha
```

Router 必须拒绝：

```text
integration-base-diverged
```

V1.2 不自动：

- rebase；
- cherry-pick；
- conflict resolution；
- merge strategy decision。

这些交给 Codex。

---

## 16.3 正常 integration

若 main 未发生变化：

```text
A
│
└── Worker commit W
```

使用：

```text
git merge --ff-only
```

形成：

```text
A → W
```

不制造不必要的 merge commit。

---

# 17. cleanup

新增：

```text
claude-worker-router cleanup RUN_ID
```

其职责仅为：

- 删除 Router worktree；
- 删除临时 Worker branch；
- 保留 Evidence。

Evidence 默认永久保留，除非未来单独设计 retention policy。

---

## 17.1 cleanup 安全条件

以下情况默认可 cleanup：

- 已完成 integration；
- 没有 Worker change；
- 明确被 discard 的 run。

以下情况不得默认 cleanup：

```text
ready-for-review
但尚未 integrate
```

必须显式：

```text
--discard
```

才允许删除未集成 worktree。

---

# 18. stale worktree

增加：

```text
claude-worker-router cleanup --stale
```

默认 stale threshold：

```text
168 hours
```

即 7 天。

但 stale 不等于自动删除。

---

## 18.1 默认处理

可以自动删除：

- 已 integrated 的 Router worktree；
- 已不存在有效 branch 的空残留；
- Git worktree metadata 中已失效的 Router 记录。

不得自动删除：

- ready-for-review 且未 integrate；
- blocked/escalated 但仍存在修改；
- evidence 与 worktree 不一致的 run。

这些仅报告。

显式：

```text
--discard-unintegrated
```

后才允许清理。

---

# 19. Symlink 防护

## 19.1 风险

worktree 是目录隔离，但：

```text
repo/path/link
→ /Users/user/private
```

可能通过 symlink 形成文件系统逃逸。

---

## 19.2 V1.2 默认规则

Worker 调用前扫描 Git 跟踪的 symlink。

### 全部模式

若 symlink 最终目标：

```text
outside repository
```

则拒绝 Worker run：

```text
external-symlink-denied
```

### Edit 模式

如果 symlink 位于：

```text
allowed_paths
```

其最终 target 还必须落在：

```text
allowed_paths
```

否则拒绝。

---

## 19.3 Symlink cycle

循环 symlink：

```text
A → B
B → A
```

必须 fail closed。

不得尝试“猜测”其安全性。

---

# 20. Binary 防护

当前 diff-line budget 无法可靠衡量二进制文件。

例如 binary numstat 可能表示：

```text
-    -
```

因此：

```text
diff_lines = 0
```

不能代表修改规模很小。

---

## 20.1 默认策略

V1.2：

```text
binary_edit_policy = deny
```

Worker 不允许：

- 修改 tracked binary；
- 新增 binary；
- 删除 binary。

检测后返回：

```text
binary-change-denied
```

binary 策略后续可由 V1.3 Policy 控制，但项目 Policy 不得突破 global policy。

---

# 21. V1.2 list / show

## list

```text
claude-worker-router list
```

显示：

```text
RUN ID
TIME
REPOSITORY
MODE
PROVIDER
STATUS
FILES
DIFF
```

支持：

```text
--repo
--status
--limit
--json
```

---

## show

```text
claude-worker-router show RUN_ID
```

显示：

- task；
- acceptance criteria；
- repository；
- base SHA；
- Worker commit；
- Provider；
- attempts；
- tests；
- changed files；
- diff；
- escalation；
- integration 信息。

支持：

```text
--json
```

---

# 22. V1.2 验收条件

V1.2 完成必须满足：

- `doctor` 可独立验证本地环境；
- 所有 run 有完整 metadata；
- 所有 edit run 有 base SHA；
- tests 单独留存；
- diff 单独留存；
- events 有完整生命周期；
- evidence 有 hash manifest；
- `list` 可查历史；
- `show` 可复核单次 run；
- integrate 只能显式执行；
- base drift 会阻止 integrate；
- cleanup 保留 evidence；
- stale worktree 可识别；
- external symlink 被拒绝；
- binary edit 默认被拒绝；
- 所有 V1.1 deterministic tests 继续通过；
- V1.1 Skill 调用方式兼容。

---

# 23. V1.3 — Policy Hardening

## 23.1 目标

V1.3解决：

> 不同项目需要不同限制，但任何项目都不能削弱用户定义的全局安全边界。

Policy 层级：

```text
Built-in Invariants
       ↓
Global Policy
       ↓
Project Policy
       ↓
Task Request
```

越向下：

> 只能变严格，不能变宽松。

---

# 24. Policy 文件

继续保留运行配置：

```text
~/.codex/model-router/config.toml
```

新增全局 Policy：

```text
~/.codex/model-router/policy.toml
```

项目 Policy：

```text
<repo>/.claude-worker-router/policy.toml
```

项目 policy 可提交 Git。

Worker 默认禁止修改：

```text
.claude-worker-router/
```

---

# 25. Policy 合并原则

## 数值上限

如：

```text
max_changed_files
max_diff_lines
max_turns
timeout_seconds
```

Effective value：

```text
min(global, project, request)
```

项目不能增大 global limit。

---

## deny_paths

合并方式：

```text
Built-in deny
∪ Global deny
∪ Project deny
```

只能增加。

不能删除。

---

## allowed_paths

Request 声明的 `allowed_paths` 最终必须：

- 不触碰 deny path；
- 满足 global policy；
- 满足 project policy。

Request 不能以空数组表示 unrestricted。

---

# 26. deny_paths

采用 repository-relative path prefix。

示例：

```toml
[paths]
deny = [
  ".git",
  ".claude-worker-router",
  "secrets",
  "deployment/prod"
]
```

Router 自身 metadata、Git internals 和明确敏感目录始终不可 Worker 修改。

---

# 27. Project Budget

不同项目可以设置不同预算。

例如：

```toml
[limits]
max_changed_files = 3
max_diff_lines = 250
max_turns = 8
timeout_seconds = 600
```

如果 global 为：

```text
max_diff_lines = 500
```

project 可设：

```text
250
```

但不能设：

```text
1000
```

如果设置更宽松值：

Router 应：

```text
policy-relaxation-rejected
```

而不是静默忽略。

---

# 28. Test Profile

V1.3 引入命名测试方案。

例如：

```text
python-unit
python-pytest
npm-test
frontend-check
```

TaskRequest 可使用：

```text
test_profile = "python-unit"
```

代替每次重复写 argv。

---

## 28.1 Profile 要求

Test Profile 仍然必须：

- 使用 argv array；
- 禁止 shell string；
- 禁止 shell=True；
- binary 必须在 global allowlist；
- 不允许自定义 secret env；
- 由 Router 执行；
- Worker 无权修改执行中的 Profile。

---

## 28.2 Policy fingerprint

Router 每次 run 应记录：

```text
global_policy_hash
project_policy_hash
effective_policy_hash
```

确保未来可以回答：

> “这个 Worker 当时是在什么 Policy 下执行的？”

---

# 29. macOS Sandbox Research

V1.3 只引入：

> **可选 OS sandbox 能力研究与实验实现。**

不将其作为必选依赖。

目标：

```text
Worker Process
    │
    ├─ allowed read: repository/worktree
    ├─ allowed write: worktree + approved temp
    ├─ denied write: home sensitive paths
    ├─ denied write: ~/.ssh
    ├─ denied write: ~/.codex
    ├─ denied write: ~/.claude
    └─ network: provider access retained
```

---

## 29.1 Sandbox 原则

必须满足：

- 不破坏 Claude Code 正常非交互运行；
- 不暴露 API credential；
- 不修改全局系统配置；
- fail closed；
- 明确标记 experimental；
- worktree isolation 与 OS sandbox 分开描述。

如果无法可靠满足以上条件：

> V1.3 可以发布而不启用 sandbox。

---

## 29.2 Policy

可设计：

```text
sandbox_required = false
```

若 global 设置：

```text
true
```

Project Policy 不得关闭。

---

# 30. V1.3 验收条件

必须验证：

- project policy 无法突破 global policy；
- request 无法突破 project policy；
- deny paths 始终优先；
- budget 使用最严格值；
- test profiles 不通过 shell；
- project policy 无法修改 provider；
- project policy 无法指定 credential；
- policy hashes 写入 evidence；
- Worker 无权修改运行中的 policy；
- sandbox 不可用时，required mode 必须阻止执行。

---

# 31. V1.4 — Run Management

## 31.1 进入条件

只有实际使用开始出现：

- 多个 Worker 请求等待；
- 很难记住哪个任务完成；
- 多个 run 同时需要 review；
- 需要取消尚未开始的任务；

才进入 V1.4。

---

# 32. 生命周期与执行结果分离

V1.4 开始区分：

### Lifecycle State

```text
pending
running
ready-for-review
integrated
blocked
cancelled
```

### Execution Outcome

例如：

```text
read-only
tests-passed
worker-timeout
provider-unreachable
path-scope-exceeded
```

这样：

一个成功的 read-only run：

```text
lifecycle = ready-for-review
outcome = read-only
```

不再需要把所有概念塞进一个 `status` 字段。

---

# 33. 状态机

```text
pending
  │
  ├──────────────→ cancelled
  │
  ▼
running
  │
  ├──────────────→ blocked
  │
  ├──────────────→ cancelled
  │
  ▼
ready-for-review
  │
  ├──────────────→ cancelled
  │
  ▼
integrated
```

`blocked`、`cancelled`、`integrated` 为 terminal state。

Blocked run 不自动 retry。

重新执行必须产生：

```text
new run_id
```

可记录：

```text
parent_run_id
```

---

# 34. Queue

新增：

```text
claude-worker-router submit
claude-worker-router queue
claude-worker-router cancel RUN_ID
claude-worker-router drain
```

---

## 34.1 submit

任务进入：

```text
pending
```

立即返回：

```text
run_id
```

---

## 34.2 drain

V1.4：

```text
单 Worker
顺序执行
```

不并发。

按：

```text
priority
created_at
```

领取任务。

---

# 35. 状态持久化

V1.4 引入 SQLite：

```text
~/.codex/model-router/state.db
```

SQLite 仅负责：

- lifecycle state；
- queue；
- timestamps；
- run index；
- repository identity；
- parent/child relationship；
- evidence directory reference。

详细证据仍保存在：

```text
run_records/RUN_ID/
```

---

# 36. Crash Recovery

如果 Router 在：

```text
running
```

期间异常退出：

下一次启动不得自动重新执行 Worker。

该 run 转：

```text
blocked
```

reason：

```text
runner-interrupted
```

保留：

- worktree；
- partial diff；
- events；
- evidence。

重新执行必须由 Codex/用户显式创建新 run。

---

# 37. Cancellation

### pending

可以立即：

```text
pending → cancelled
```

### running

Router 尝试终止 Worker process。

然后：

```text
running → cancelled
```

保留 worktree 和证据。

### ready-for-review

取消等同：

> 显式放弃该结果。

不得偷偷 cleanup。

---

# 38. V1.4 不做的事情

V1.4 不增加：

- 并发；
- DAG；
- 自动依赖推理；
- 自动 retry；
- lease worker farm；
- 多 Provider；
- background cloud service。

这是一个：

> **可管理的单 Worker 队列。**

---

# 39. V1.4 验收条件

- pending 状态可持久化；
- Router 重启 queue 不丢；
- running crash 不静默 retry；
- cancel 有完整 evidence；
- ready-for-review 可以跨重启保留；
- integrate 后状态准确；
- list/show 使用统一 state DB；
- SQLite 与 run evidence 可以交叉验证。

---

# 40. V1.5 — Bounded Concurrency

## 40.1 进入条件

只有真实 run 数据证明顺序执行产生明显等待时才开发。

建议最低证据：

> 最近 50 个 queued tasks 中，至少 20% 存在本可以同时执行的独立任务，并且顺序等待时间明显影响实际使用。

没有这个证据：

> 保持 V1.4 单 Worker。

---

# 41. 并发上限

V1.5：

```text
max_concurrency = 2
```

固定最大 2。

不提供无限配置。

---

# 42. Same Provider Fingerprint

并发任务必须使用：

```text
同一个 Provider fingerprint
```

调度器启动并发批次时记录：

```text
provider_epoch
```

每次领取新任务前重新检查。

若：

```text
current fingerprint != provider_epoch
```

则：

- 不再启动新任务；
- 已运行任务允许结束；
- 运行结束时仍执行 fingerprint verification；
- mismatch 的 run 进入 blocked/escalated；
- 不自动切 Provider。

---

# 43. Path Conflict

同一 repository 的两个 edit task：

只有：

```text
allowed_paths disjoint
```

才允许并发。

---

## 43.1 冲突算法

以下视为 conflict：

```text
A == B
```

或者：

```text
A is ancestor of B
```

或者：

```text
B is ancestor of A
```

例如：

```text
src/core
src/core/models
```

冲突。

而：

```text
src/backend
web/frontend
```

不冲突。

---

# 44. 不同 repository

不同 Git repository：

默认不存在文件 path conflict。

但仍：

- 共享 concurrency=2；
- 共享 Provider fingerprint 约束；
- 遵守各自 Project Policy。

---

# 45. Exclusive Tests

V1.3 Test Profile 可增加：

```text
exclusive = true
```

用于：

- 固定端口；
- 独占数据库；
- 全局 test fixture；
- 不适合并发的测试。

任何使用 exclusive profile 的 run：

> 不与其他 Worker 同时执行。

---

# 46. Integration 仍串行

即使 Worker execution 并发：

```text
integrate
```

仍按 repository 串行。

不得同时修改同一 main checkout。

---

# 47. V1.5 不做

- 不自动修改 CC Switch；
- 不允许 GLM + MiniMax 同时；
- 不允许 Claude + GLM 同时；
- 不支持 3+ Worker；
- 不自动 resolve conflict；
- 不并发 integrate；
- 不自动 fallback。

---

# 48. V1.5 验收条件

必须测试：

- 两个 disjoint path 可并发；
- overlap path 自动串行；
- ancestor path 正确识别冲突；
- Provider fingerprint 改变停止新 dispatch；
- running run 检测 provider mismatch；
- exclusive test 会强制串行；
- crash 后状态一致；
- 两个 worktree 不互相污染。

---

# 49. V2.0 — Multi-Worker

## 49.1 进入条件

V2.0 不是按时间自动推进的版本。

必须满足：

> 用户确实需要 GLM、MiniMax、Claude 等不同 Worker 在同一个时间段独立工作，并且人工 CC Switch 已经成为执行瓶颈。

否则继续使用 V1.5。

---

# 50. 关键架构变化

V1.x：

```text
Claude Code
   ↓
~/.claude/settings.json
   ↓
CC Switch Current Provider
```

V2.0：

```text
Router
   │
   ├─ Worker Profile: glm
   │      └─ isolated immutable configuration
   │
   ├─ Worker Profile: minimax
   │      └─ isolated immutable configuration
   │
   └─ Worker Profile: claude
          └─ isolated immutable configuration
```

Router 不再通过修改：

```text
~/.claude/settings.json
```

切 Provider。

---

# 51. Immutable Worker Profile

每个 Profile：

```text
profile_id
runtime
command
provider configuration reference
model identity
credential reference
capabilities
policy overrides
configuration fingerprint
```

Profile：

- 创建后执行期间不可修改；
- Router 不写 Provider 配置；
- credential 不进入 evidence；
- Profile fingerprint 写入 run metadata。

---

# 52. Legacy CC Switch Mode

保留：

```text
cc-switch-current
```

用于兼容。

但：

> legacy CC Switch profile 不允许参与不同 Provider 的并发执行。

一旦 run 使用 legacy mode：

该 run 继续遵守 V1.x manual-only 原则。

---

# 53. Profile Selection

即使 V2.0：

Router 仍然不自动决定：

```text
“哪个模型最便宜”
“哪个模型更聪明”
“失败后换哪个”
```

Profile 由：

- Codex；
- 用户；
- 上层显式 Policy；

明确指定。

例如：

```text
worker_profile = "minimax-worker"
```

Provider failure 后：

不得自动：

```text
minimax → glm
```

如果 Codex 决定换 Worker：

创建新的 run，并记录：

```text
parent_run_id
```

---

# 54. WorkerBackend Abstraction

V2.0 初期即使存在多个 Worker Profile：

如果全部仍然是 Claude Code：

> 不强制引入 WorkerBackend 抽象。

只有真正引入第二种 runtime：

例如：

```text
Claude Code
Codex CLI
Gemini CLI
```

才建立：

```text
WorkerBackend
```

接口至少包括：

```text
preflight()
invoke()
capabilities()
fingerprint()
normalize_outcome()
terminate()
```

避免提前抽象尚不存在的兼容需求。

---

# 55. V2.0 Security

每个 Worker Profile 必须做到：

- 配置隔离；
- credential 隔离；
- fingerprint 独立；
- evidence 独立；
- worktree 独立；
- failure 独立；
- 不修改其他 Profile；
- 不修改 global provider state。

---

# 56. V2.0 验收条件

必须证明：

```text
GLM run
MiniMax run
Claude run
```

同时执行时：

- 不修改同一个 settings；
- Provider identity 不串线；
- Credential 不串线；
- evidence 可确认实际执行者；
- 一个 Worker 失败不改变其他 Worker；
- 不存在自动 fallback；
- path conflict 规则继续有效。

---

# 57. V2.x — Control Plane

## 57.1 进入条件

只有出现以下真实需求之一才开发：

- AionUi 需要调用 Router；
- Orbit 需要调用 Router；
- 手机需要查询 Router；
- 另一台电脑需要提交任务；
- Codex 以外的客户端需要访问 Router。

否则：

> CLI 已经足够。

---

# 58. Control Plane 定位

Control Plane 不是新的 Agent。

它只是：

> Router 核心能力的外部访问层。

架构：

```text
              ┌─ CLI
              │
Client ───────┼─ MCP
              │
              └─ HTTP
                    │
                    ▼
             Application Service
                    │
                    ▼
                 Router Core
```

CLI、MCP、HTTP 必须调用同一套核心逻辑。

不得存在：

> HTTP 可以绕过 CLI Policy。

---

# 59. 最小 API

首版仅提供：

```text
submit
status
cancel
evidence
approve
```

---

## 59.1 submit

输入：

- repository；
- task；
- acceptance criteria；
- allowed paths；
- mode；
- test profile；
- optional worker profile。

返回：

```text
run_id
```

必须支持 idempotency key，避免网络重试创建重复任务。

---

## 59.2 status

返回：

```text
lifecycle
outcome
timestamps
provider/profile
changed files
tests summary
attention required
```

---

## 59.3 cancel

调用 V1.4 同一 cancellation 逻辑。

不得实现第二套取消机制。

---

## 59.4 evidence

提供：

- metadata；
- tests；
- diff；
- event timeline；
- evidence manifest。

不得返回 credential。

---

## 59.5 approve

Approve 是：

> 显式授权操作。

对 `ready-for-review` run：

执行和 CLI integrate 完全相同的安全检查。

若：

```text
base diverged
```

仍必须失败。

不得因为来自 GUI 或手机就降低集成检查。

---

# 60. Control Plane 网络安全

默认：

```text
bind = 127.0.0.1
```

不得默认：

```text
0.0.0.0
```

远程访问必须显式启用认证。

至少支持：

- access key；
- key rotation；
- request audit；
- rate limiting。

推荐通过可信私有网络访问。

Router 不直接默认暴露公网。

---

# 61. MCP

MCP 仅作为 adapter。

示例工具：

```text
router_submit
router_status
router_cancel
router_evidence
router_approve
```

MCP Server 不拥有：

- Provider 配置修改权限；
- Policy 修改权限；
- arbitrary shell 权限；
- credential 输出权限。

---

# 62. GUI 原则

Router 项目：

> 永远不建设完整 GUI。

可使用：

- AionUi；
- Orbit；
- 其他 MCP Client；
- Web 前端；
- 手机客户端。

Router 只提供稳定 Control Plane。

这样：

```text
Frontend 可替换
Router Core 不变
```

---

# 63. 参考项目的借鉴边界

## OpenAgentd

借鉴：

> Everything observable.

用于：

- events；
- timing；
- diff；
- test result；
- agent/run status。

不借：

- 完整桌面 UI；
- 自有 Agent Runtime；
- 大型 Provider 管理系统。

---

## TNJ Orbit

借鉴：

- lifecycle；
- queue；
- durable state；
- bounded scheduling；
- clear completion / blocking。

不借：

- Router 自己重新 design；
- Router 自己 decompose；
- V1.x 多 Runner；
- 自动 retry workflow；
- 复杂 DAG。

因为这些职责已经由 Codex承担。

---

## xinnaider Orbit

借鉴：

- create/status/cancel 类控制接口；
- Agent/run inspection 思路。

不借：

- Desktop UI；
- split pane；
-完整 MCP orchestration platform。

---

## Orbital

借鉴：

> Project，而不是临时 session，是 run 的归属主体。

因此每个 run 必须明确记录：

```text
repository
base commit
project policy
history
```

不借：

```text
PROJECT_STATE.md
DECISIONS.md
LESSONS.md
self-improving memory
```

Router 不建设第二套长期语义记忆。

---

## AionUi

定位为：

> Router 未来可能使用的外部 GUI。

不复制：

- Office；
- Team Mode；
- GUI；
- Browser；
- Cron；
-模型管理。

---

# 64. 数据权威关系

最终必须保持：

```text
Git repository
= 代码事实来源

Policy
= 执行权限事实来源

Run Evidence
= Worker 执行事实来源

state.db
= 生命周期事实来源

Codex / 项目文档 / Memory
= 语义与决策事实来源
```

不得出现两个系统同时宣称：

> 自己才是项目决策的真相来源。

---

# 65. Audit Trail

一次成熟 run 最终应形成：

```text
RUN
│
├── Request
│
├── Effective Policy
│
├── Base SHA
│
├── Provider/Profile Fingerprint
│
├── Worktree
│
├── Worker Attempts
│
├── Tests
│
├── Changed Files
│
├── Diff
│
├── Events
│
├── Outcome
│
├── Review
│
└── Integration
```

任何关键阶段失败：

仍然保留前面已经产生的 evidence。

---

# 66. Error Philosophy

Router 应继续使用：

> structured failure，而不是 catch-all failure。

例如：

```text
provider-unreachable
worker-timeout
worker-turn-limit
worker-permission-denied
worker-output-invalid

path-scope-exceeded
binary-change-denied
external-symlink-denied

test-launch-failed
test-timeout
tests-failed-after-correction

integration-base-diverged
integration-dirty-checkout

policy-relaxation-rejected
sandbox-unavailable

runner-interrupted
```

每个 error 都应该回答：

1. 谁应该处理；
2. 是否允许 retry；
3. 是否需要人工介入；
4. worktree 是否保留；
5. evidence 是否完整。

---

# 67. Retry Philosophy

允许：

> 同一 Worker 对测试失败进行现有 correction loop。

不允许：

- Provider failure 自动 retry 其他 Provider；
- crash 后静默 retry；
- policy violation 自动 retry；
- path violation 自动 retry；
- sandbox failure 自动降级；
- base divergence 自动 rebase。

当需要重新执行时：

```text
创建新 run_id
```

并使用：

```text
parent_run_id
```

形成可追溯关系。

---

# 68. Backward Compatibility

V1.2–V1.5：

必须继续兼容：

```text
Claude Code
CC Switch Current Provider
```

以及原有 Skill JSON contract。

新增字段优先：

- optional；
- schema versioned；
- 有明确 default。

重大破坏性配置改变原则上推迟至 V2.0。

---

# 69. Version Gate

每个版本进入下一个版本前必须证明上一版本真实解决了对应问题。

```text
V1.2
可靠运行
   ↓
V1.3
可靠治理
   ↓
V1.4
可靠管理
   ↓
V1.5
可靠并发
   ↓
V2.0
可靠多 Worker
   ↓
V2.x
可靠外部控制
```

不得：

```text
V1.2 不稳定
   ↓
直接增加 Multi-Agent
```

---

# 70. 最终目标

如果本路线正确执行，成熟 Router 不应该给人的感觉是：

> “这里有很多 Agent，好复杂。”

而应该是：

> “Codex 需要 Worker 时，它自然把任务交出去；Worker 被限制在正确范围；测试自动完成；所有证据都留下；需要我决定的时候才叫我。”

最终体验：

```text
User
  │
  ▼
Codex
  │
  │ bounded execution
  ▼
Router
  │
  ├─ verifies environment
  ├─ verifies policy
  ├─ isolates worktree
  ├─ invokes worker
  ├─ verifies provider
  ├─ verifies scope
  ├─ runs tests
  ├─ records evidence
  └─ returns review package
  │
  ▼
Codex Review
  │
  ▼
Explicit Approval
  │
  ▼
Integrate
```

Router 的成熟标准不是：

> 能调多少模型。

而是：

> **在用户不需要操心执行细节的情况下，仍然保证每一步可控、可验证、可追溯。**

这应当成为 Claude Worker Router 从 V1.2 到 V2.x 的长期设计原则。
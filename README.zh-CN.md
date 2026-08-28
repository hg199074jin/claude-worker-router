# Claude Worker Router

[![测试](https://github.com/hg199074jin/claude-worker-router/actions/workflows/tests.yml/badge.svg)](https://github.com/hg199074jin/claude-worker-router/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)

[English](README.md)

Claude Worker Router 让 Codex 把边界清晰的实现任务交给 Claude Code，
但仍由 Codex 负责协调、复核和最终决定。它调用你在 Claude Code / CC Switch
中`手动选择`的提供商，例如 MiniMax、GLM 或其他兼容提供商；它不会保存提供商
档案、指定模型，也不会自动切换或 fallback。

```text
你在 CC Switch 中手动选择提供商
                    │
Codex ── 结构化 JSON ──> Router ──> Claude Code
  │                              │             │
  └── 审查与批准                  └── 编辑任务使用隔离 Git worktree
```

这是一个刻意保守的工具：它只适用于小范围、可回滚、可测试的修改；不适用于
架构决策、凭据、生产操作、破坏性任务或大范围重构。

## 任务该交给谁

| 任务类型 | 建议负责人 | 原因 |
| --- | --- | --- |
| 一个有本地测试的定点修复 | Claude Code worker | diff 有边界，验收标准可观察 |
| 狭窄的代码审查或排查 | `read-only` worker | 不会暴露文件编辑工具 |
| 鉴权、密钥、支付、生产或基础设施 | Codex | 属于安全或远端写入边界 |
| 跨模块设计或无边界重构 | Codex | 需要架构判断 |
| worker 升级、超时或权限失败 | Codex | 不会偷偷重试或换模型 |

## 能保证什么，不能保证什么

Router 会：

- 从标准输入读取一份结构化 JSON，避免把任务文本放进命令行参数；
- 使用固定工具集，worker 没有 Bash 权限；
- 为编辑任务创建独立 Git worktree；只读任务无法编辑文件；
- 强制检查 `allowed_paths`、变更预算和已批准的测试 argv；
- 由执行器以最小、无敏感信息的环境运行测试；
- 记录不可变 base SHA 与脱敏后的提供商指纹；
- 默认拒绝不安全的已跟踪符号链接和二进制改动；
- 保存 request、result、metadata、测试输出、完整补丁、事件时间线和
  SHA-256 清单，作为可复核证据；
- 返回结构化升级原因，而非悄悄换模型或换提供商重试。

Router 不会选择或修改 CC Switch / Claude Code 的提供商设置，不会自动合并
worker 输出，不会把 worktree 伪装成操作系统级沙箱，也不会让安全敏感或生产
任务自动变得可委派。

## 快速开始

### 1. 安装前置条件

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Git
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview)
- CC Switch，或其他`手动`配置 Claude Code 提供商的方式

```sh
git clone git@github.com:hg199074jin/claude-worker-router.git
cd claude-worker-router
uv sync
```

### 2. 配置 Router，但不要复制凭据

```sh
mkdir -p ~/.codex/model-router
cp config.example.toml ~/.codex/model-router/config.toml
```

在复制后的文件中填写 `run_records` 和 `claude_settings` 的占位路径。凭据仍留在
Claude Code / CC Switch 中；Router 配置文件不包含 API Key 或 Token。

### 3. 检查环境

```sh
uv run claude-worker-router doctor --json
```

`doctor` 在可用时返回 `0`，有警告时返回 `1`，无法安全运行时返回 `2`。

### 4. 安装 Codex 路由技能

```sh
ln -s "$(pwd)/skill" ~/.codex/skills/claude-worker-router
```

随附技能会告诉 Codex 何时可委派、何时必须由 Codex 自己负责。

## 任务 JSON 契约

每次执行都是通过标准输入传入的一份 JSON。编辑任务必须给出仓库相对的
`allowed_paths`，并且至少给出一个已批准的项目内测试命令。

```json
{
  "repository": "/absolute/path/to/your-project",
  "task": "修正 src/pricing/discount.py 中的折扣计算。",
  "acceptance_criteria": [
    "200 元商品打 75 折后返回 150。",
    "聚焦的单元测试退出码为 0。"
  ],
  "mode": "edit",
  "allowed_paths": ["src/pricing/discount.py"],
  "test_commands": [["uv", "run", "python", "-m", "unittest", "-v"]]
}
```

```sh
printf '%s' '<任务 JSON>' | uv run claude-worker-router
```

`read-only` 任务不能带测试命令，也不会得到编辑工具：

```json
{
  "repository": "/absolute/path/to/your-project",
  "task": "找出 checkout 测试失败的可能原因。",
  "acceptance_criteria": ["报告结论与涉及文件。"],
  "mode": "read-only",
  "allowed_paths": []
}
```

| 可选字段 | 含义 |
| --- | --- |
| `exclusive_tests` | 为需要独占资源的测试预留一个独占队列批次 |
| `test_profile` | 使用配置中的命名测试方案，替代内联 `test_commands` |

`test_profile` 与 `test_commands` 互斥。任何试图指定 model、settings 或
provider 覆盖项的请求都会被拒绝。

## 结果、证据与集成

编辑任务的成功状态是 `ready-for-review`，它`不会`自动合并。只读任务的成功
状态是 `read-only`。`escalated` 结果会给出精确的 `escalation_reason`。

```text
pending → running → ready-for-review → integrated
                   └───────────────→ blocked / cancelled
```

请按下列流程交接，而不要直接合并 worker 分支：

1. 使用 `show RUN_ID` 检查证据。
2. 审查补丁、改动路径、测试输出、base SHA 和脱敏提供商信息。
3. 人工明确批准集成。
4. 执行 `integrate RUN_ID`；它只会 fast-forward。
5. 不再需要 worker worktree 时执行 `cleanup RUN_ID`。

```sh
claude-worker-router doctor [--repo PATH] [--json]
claude-worker-router list [--repo ...] [--status ...] [--limit N] [--json]
claude-worker-router show RUN_ID [--json]
claude-worker-router integrate RUN_ID
claude-worker-router cleanup RUN_ID [--discard]
claude-worker-router cleanup --stale [--stale-hours 168]
```

集成会拒绝脏工作区、基点漂移（`integration-base-diverged`）、失败测试、缺失
worker 分支和证据 SHA-256 清单不一致。它不会 rebase、强推、自动解决冲突，
也不会创建 merge commit。`cleanup` 只删除 Router 创建的隔离产物，证据会保留；
使用 `--discard` 显式放弃尚未集成的改动。

## 队列、取消与恢复

```sh
printf '%s' '<任务 JSON>' | claude-worker-router submit
claude-worker-router queue --state pending --json
claude-worker-router drain [--once]
claude-worker-router cancel RUN_ID
```

`submit` 使用相同的任务 JSON，另加可选 `priority`（数值越大越优先）和
`parent_run_id`；队列字段只存于 SQLite，不写入任务证据。状态保存在运行记录旁
的 `state.db` 中。

若 drainer 在运行中中断，`doctor` 会显示 `queue-health`，下一次 drain 会将旧
任务标记为 `blocked` / `runner-interrupted`。它绝不会被隐式重新执行；只有你
决定重试时才提交一个新 run。取消运行中的任务只会终止 worker 的专属进程组，
不会影响你的 shell，并保留其 worktree 与证据。

## 有界并发

设置 `max_concurrency = 2` 后，最多同时运行两个 worker。默认是 `1`，
任何大于 `2` 的值都会被拒绝。

- 同仓库编辑任务仅在 `allowed_paths` 范围不相交时才能同批；不同仓库不会冲突。
- 每个批次固定一个提供商指纹。若期间 CC Switch 发生变化，派发以退出码 `5`
  停止，待执行任务仍留在队列，不会自动选择其他提供商。
- `exclusive_tests: true` 会让任务独占一个批次。
- 集成仍通过建议性锁按仓库串行化。

两个并发位是当前版本刻意设置的上限，而不是 worker 农场。

## 策略与安全边界

可选的全局和项目策略文件只能在 `config.toml` 基础上继续收紧限制：

```toml
# ~/.codex/model-router/policy.toml
sandbox_required = false
[limits]
max_turns = 6
max_diff_lines = 400
[paths]
deny = ["secrets", "deployment/prod"]
```

```toml
# <repo>/.claude-worker-router/policy.toml
[limits]
max_turns = 4
[paths]
deny = ["infra"]
```

数值限制取更小值，deny 路径取并集，安全布尔值只能从关闭变为开启。项目策略
尝试放宽全局规则会被拒绝为 `policy-relaxation-rejected`；每次运行都会记录
策略指纹。

`.git` 和 `.claude-worker-router` 始终禁止 worker 编辑。设置
`sandbox_required = true` 目前会 fail closed 为 `sandbox-unavailable`：
worktree 隔离`不是`操作系统级沙箱。详见
[macOS 可行性研究](docs/superpowers/research/2026-08-27-macos-sandbox-feasibility.md)。

在审查前，应把所有 worker 输出当作不可信改动。密钥、依赖凭据的测试、基础
设施变更、破坏性操作、生产任务和重大设计工作都应留给 Codex。

## 配置参考

从 [config.example.toml](config.example.toml) 开始。

| 字段 | 用途 |
| --- | --- |
| `command` | 如 `claude` 的裸可执行文件名，或绝对可执行路径 |
| `provider` | 必须为 `cc-switch-current`，仅表示手动提供商路由 |
| `max_turns`、`timeout_seconds`、`correction_limit` | 单次运行及修正循环的硬限制 |
| `max_changed_files`、`max_diff_lines` | 编辑 diff 预算 |
| `allowed_test_binaries` | 测试 argv 中允许使用的可执行文件 |
| `run_records` | 持久证据目录；默认 `~/.codex/model-router/runs` |
| `claude_settings` | Claude Code 设置；默认 `~/.claude/settings.json` |
| `max_concurrency` | 默认 `1`，最大 `2` |
| `binary_edit_policy` | 目前只支持 `"deny"` |

包含 `/` 的相对可执行路径、字符串形式的 shell 测试命令、不安全路径、编辑任务
缺失测试命令或二进制改动，都会在 worker 改动前被拒绝。

## 排错

| 现象 | 含义 | 下一步 |
| --- | --- | --- |
| `doctor` 返回 2 | 必需的本地依赖或配置不可用 | 运行 `doctor --json`，处理具名失败检查 |
| `worker-permission-denied` | 任务需要固定工具集之外的能力 | 收窄或重设计任务；不要授予宽泛 shell 权限 |
| `worker-timeout` / `worker-turn-limit` | 任务超过有界预算 | 交回 Codex；不要自动切换提供商 |
| `external-symlink-denied` | 已跟踪符号链接外逃、损坏或成环 | 修复链接后再委派 |
| `policy-path-denied` / `binary-change-denied` | 请求或实际改动触及受保护边界 | 由 Codex 处理，或有意识地调整策略 |
| `drain` 返回 5 | 批次期间提供商发生变化 | 待执行任务仍在队列；稳定手动选择后再 drain |
| `integration-base-diverged` | worker 开始后目标分支已前进 | 审查新基点并创建新的 run |

## 开发与验证

```sh
PYTHONWARNINGS=error uv run --python 3.12 python -m unittest discover -v
uv build
```

真实冒烟测试会创建新的临时 Git 项目，执行一次只读和一次隔离编辑，然后检查
测试证据和主工作区哈希。它会调用 Claude Code 当前选定的提供商，可能消耗额度：

```sh
tests/live/run_smoke_test.sh
```

历史验证记录见 [VERIFICATION.md](VERIFICATION.md)，其中不包含 API Token
或 Token 片段。

## 范围与贡献状态

这是一个个人使用的、提供商中立的 Codex + Claude Code 工作流工具。它选择
显式升级、人工审查、可复现证据和手动模型选择，而不是追求自治吞吐量。当前尚未
选择许可证或贡献政策。

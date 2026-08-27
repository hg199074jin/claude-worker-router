# Claude Worker Router

[![测试](https://github.com/hg199074jin/claude-worker-router/actions/workflows/tests.yml/badge.svg)](https://github.com/hg199074jin/claude-worker-router/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)

[English](README.md)

一个与提供商无关的执行器：它让 Codex 将边界清晰的编码任务交给
Claude Code 中当前选定的模型，并保留可审查的执行证据。对于编辑任务，
它会在隔离的 Git worktree 中工作。

路由器**不会**选择、切换或在模型之间自动 fallback。请在 CC Switch 中
手动选择提供商；路由器只会通过 Claude Code 调用当前配置。

> 这是一个刻意保守的工具。架构决策、密钥、安全边界、生产环境变更和
> 远程写入仍由 Codex 负责；worker 只适合小范围、可回滚、可测试的任务。

## 功能

- 通过 Claude Code 执行单个结构化 JSON 任务，不将任务文本放到命令行。
- 使用 `--safe-mode` 与固定工具集，worker 没有 Bash 权限。
- 编辑任务创建隔离 worktree；只读任务无法编辑文件。
- 强制执行仓库相对路径的 `allowed_paths`、变更预算和已批准的测试命令。
- 由执行器以最小、非敏感的环境运行测试。
- 记录 worker 修改所基于的不可变 base SHA。
- 在调用 worker 之前对 Git 跟踪的符号链接 fail closed
  （`external-symlink-denied`）；默认拒绝任何二进制文件改动
  （`binary-change-denied`）。
- 保存脱敏的提供商指纹和完整的每次运行证据目录：request、result、
  metadata、tests、完整 diff 补丁、append-only 事件时间线，以及 SHA-256
  完整性清单。
- 返回结构化的升级原因，而不是静默地换一个提供商重试。

## Run 管理命令

除 stdin 执行器外，V1.2 提供五个共用同一核心的子命令：

```sh
claude-worker-router doctor [--repo PATH] [--json]  # 环境诊断（0=就绪 1=有警告 2=不可用）
claude-worker-router list [--repo ...] [--status ...] [--limit N] [--json]
claude-worker-router show RUN_ID [--json]           # 复核单次 run 的证据摘要
claude-worker-router integrate RUN_ID               # 通过校验后 fast-forward 集成
claude-worker-router cleanup RUN_ID [--discard]     # 清理隔离产物；证据永久保留
claude-worker-router cleanup --stale                # 报告超过 168 小时的陈旧 run
```

编辑任务的标准生命周期为：`ready-for-review` → `show` → 人工审查 →
显式批准 → `integrate` → `cleanup`。integrate 的前置检查会拒绝脏的主工作区
（`integration-dirty-checkout`）、基点漂移（`integration-base-diverged`）、
未通过的测试以及证据哈希不一致的情况。它只做 fast-forward 合并——没有
rebase、没有强推、也不会制造 merge commit；如果主分支已前进，由 Codex
决定下一步。证据目录是永久记录，cleanup 永远不会删除它们。

## 队列与取消（V1.4）

任务密集时可以把提交与执行解耦：

```sh
printf '%s' '{...任务 JSON...}' | claude-worker-router submit   # 立即返回 pending
claude-worker-router queue [--state ...] [--json]
claude-worker-router drain [--once]     # 单 worker，严格顺序执行
claude-worker-router cancel RUN_ID      # 支持 pending / running / ready-for-review
```

`submit` 复用与 stdin 相同的 JSON 契约，另加可选的 `priority`（越大越先
执行）与 `parent_run_id`；两者仅存于状态库，不进入证据契约。生命周期
（`pending → running → ready-for-review / integrated / blocked /
cancelled`）记录在运行记录目录旁的 SQLite `state.db` 中，重启不丢。
上次 drain 异常中断会在下次启动时转为 blocked（`runner-interrupted`），
并由 `doctor --json` 的 queue-health 检查暴露——重新执行必须使用新的
run id。取消 running 任务只会终止 worker 自己的进程组（绝不波及你的
shell），worktree 与证据完整保留。

## 有界并发（V1.5）

当配置写入 `max_concurrency = 2` 时，`drain` 最多同时运行 **两个** worker
（默认 `1`；大于 2 直接判定配置非法）。调度规则：

- 同一仓库内，两个编辑任务只有在 `allowed_paths` 范围互不相交时才能同批；
  不同仓库的任务永不冲突。
- 每个批次运行在同一个 provider 指纹（epoch）之下。若期间 CC Switch 发生
  变化，派发立即停止（退出码 5）：待执行任务保持 pending，运行中的任务按
  各自的收尾指纹校验结束——绝不自动切换。
- 任务可用 `"exclusive_tests": true` 退出共批：排他任务独占一个批次。
- `integrate` 通过建议性文件锁按仓库串行化，避免两个入口同时改动同一主
  工作区。

这是刻意收窄的能力：两个并发位是为了消除真实等待，而不是做 worker 农场。
V1.5 设计要求以真实使用数据（近期队列中 ≥20% 可并行任务）作为进一步放宽
的前提。

## 策略层（V1.3）

`config.toml` 里的限额是运营者的运行上限；策略在此基础上按机器和项目进一步收紧：

```sh
# ~/.codex/model-router/policy.toml            （全局）
sandbox_required = false
[limits]
max_turns = 6
max_diff_lines = 400
[paths]
deny = ["secrets", "deployment/prod"]
```

```toml
# <repo>/.claude-worker-router/policy.toml     （项目，随 Git 提交！）
[limits]
max_turns = 4
[paths]
deny = ["infra"]
```

合并规则：数值只许变小（min）、deny 只许增加（并集）、布尔安全要求只会开启。
项目文件试图放宽已解析的全局值会立即失败（`policy-relaxation-rejected`），
绝不静默钳制。每次运行的证据都会记录各层及最终生效规则集的 SHA-256 指纹，
回答“这个 worker 当时是在什么策略下执行的”。

V1.3 新增的硬边界：`.git` 与 `.claude-worker-router` 永远禁写；改动命中
deny 前缀以 `policy-path-denied` 升级；任务可用 `"test_profile"` 引用配置中
的命名测试方案（与内联 `test_commands` 互斥），`exclusive = true` 的方案直接
接入 V1.5 批次排他；未知名称升级为 `test-profile-unknown`。设置
`sandbox_required = true` 目前会 fail closed 返回 `sandbox-unavailable`；
可行性研究见
`docs/superpowers/research/2026-08-27-macos-sandbox-feasibility.md`。

## 安全模型

Claude Code 当前使用的提供商只能手动选择。请求中不要带 `model`、
`settings` 或 `provider_profile`，也不要期待自动切换或 fallback。

worktree 隔离可以将修改与主工作区分开，但它**不是**操作系统级别的安全
沙箱。请把 worker 输出当作不可信改动处理：审查 diff、查看执行证据，确认
后再集成。不要把安全敏感任务、密钥、依赖凭据的测试、基础设施变更、
破坏性操作或大型架构工作交给 worker。

## 环境要求

- Python 3.12 或更高版本
- [uv](https://docs.astral.sh/uv/)
- Git
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview)
- CC Switch（或其他能够手动提供 Claude Code 配置的方式）

## 安装

克隆仓库，并在隔离环境中安装：

```sh
git clone git@github.com:hg199074jin/claude-worker-router.git
cd claude-worker-router
uv sync
```

将 `skill/` 目录链接或复制到 Codex 技能目录。macOS 示例：

```sh
ln -s "$(pwd)/skill" ~/.codex/skills/claude-worker-router
```

复制示例配置，并将两个绝对路径占位符替换为本机路径：

```sh
mkdir -p ~/.codex/model-router
cp config.example.toml ~/.codex/model-router/config.toml
```

配置文件不包含 API Token。Claude Code 从 CC Switch 当前选择的提供商配置中
读取凭据。

## 在 Codex 中使用

已安装的技能会告诉 Codex 哪些任务可以委派。对于范围受控的编辑，Codex
通过封装器传入一个 JSON 请求，而不是直接调用 `claude -p`：

```sh
printf '%s' '{
  "repository": "/absolute/path/to/your-project",
  "task": "修复折扣计算。",
  "acceptance_criteria": ["200 元商品打 75 折后金额正确。"],
  "mode": "edit",
  "allowed_paths": ["src/pricing"],
  "test_commands": [["uv", "run", "python", "-m", "unittest", "-v"]]
}' | uv run claude-worker-router
```

仅分析时，使用 `read-only` 模式，并省略测试命令：

```json
{
  "repository": "/absolute/path/to/your-project",
  "task": "找出 checkout 测试失败的可能原因。",
  "acceptance_criteria": ["报告结论和涉及文件。"],
  "mode": "read-only",
  "allowed_paths": []
}
```

命令会输出 `RunResult` JSON。正常编辑成功的状态是 `ready-for-review`；
它不会自动将 worker 的提交合并进主工作区。只读成功的状态是 `read-only`。
任何 `escalated` 结果都会包含 `escalation_reason`，由 Codex 接管处理。

## 配置

`config.example.toml` 展示了可设置的限制，其中最重要的是：

- `command`：如 `claude` 的裸可执行文件名，或绝对路径。
- `provider`：当前为 `cc-switch-current`，表示你手动选定的提供商，
  而不是保存的提供商档案。
- `max_turns` 和 `timeout_seconds`：单次 worker 运行的硬上限。
- `max_changed_files` 和 `max_diff_lines`：编辑预算限制。
- `allowed_test_binaries`：路由器允许用于测试命令的可执行文件白名单。
- `binary_edit_policy`：V1.2 仅接受 `"deny"`——一旦 worker 增改删任何
  二进制文件，运行将直接升级，而不是带着无法度量的 diff 进入审查。

建议保持较小的限制和狭窄的路径范围。若一个任务无法以受控 diff 和项目内
测试命令验证，就应继续由 Codex 处理。

## 开发

运行完整的确定性测试：

```sh
uv run --python 3.12 python -m unittest discover -v
```

仓库还包含真实冒烟测试，会调用 Claude Code 当前配置的提供商，并可能消耗
提供商额度：

```sh
tests/live/run_smoke_test.sh
```

最新的提交验证证据见 [VERIFICATION.md](VERIFICATION.md)。其中不包含 API
Token 或 Token 片段。

## 项目状态

这是一个早期且刻意聚焦的工具，为个人的 Codex + Claude Code 工作流设计。
它优先选择清晰的升级与人工审查，而非静默自治。待仓库所有者确定许可证和
贡献规范后，欢迎提交 issue 和贡献。

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
- 保存脱敏的提供商指纹和每次运行的证据记录。
- 返回结构化的升级原因，而不是静默地换一个提供商重试。

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

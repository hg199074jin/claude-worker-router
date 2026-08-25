# Claude Worker Router

[![Tests](https://github.com/hg199074jin/claude-worker-router/actions/workflows/tests.yml/badge.svg)](https://github.com/hg199074jin/claude-worker-router/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)

[简体中文](README.zh-CN.md)

A small, provider-neutral executor that lets Codex delegate bounded coding
tasks to the model currently selected in Claude Code through CC Switch. It
creates reviewable evidence and keeps the worker inside an isolated Git
worktree for edit tasks.

The router does **not** select, switch, or fall back between models. Choose a
provider in CC Switch yourself; the router calls the Claude Code command using
that current configuration.

> The project is intentionally conservative. Codex keeps responsibility for
> architecture, secrets, security boundaries, production changes, and remote
> writes. The worker is for small, reversible, testable work.

## What it does

- Runs one structured JSON task through Claude Code without putting task text
  on the command line.
- Uses `--safe-mode` and a fixed tool set: no Bash access for the worker.
- Creates an isolated worktree for edit tasks; read-only tasks cannot edit.
- Enforces repository-relative `allowed_paths`, change budgets, and approved
  test commands.
- Runs approved tests itself with a minimal, non-secret environment.
- Captures a redacted provider fingerprint and a per-run evidence record.
- Returns structured escalation reasons instead of silently retrying with a
  different provider.

## Safety model

The selected Claude Code provider is manual-only. Do not include `model`,
`settings`, or `provider_profile` in a request, and do not expect automatic
provider fallback.

Worktree isolation is useful for keeping edits separate, but it is **not** an
OS-level security sandbox. Treat worker output as untrusted: review the diff,
inspect the evidence, and integrate only after you are satisfied. Do not send
security-sensitive tasks, secrets, credential-dependent tests, infrastructure
changes, destructive operations, or broad architectural work to the worker.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Git
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview)
- CC Switch (or another manual configuration that provides the Claude Code
  provider settings)

## Installation

Clone the repository and install it into an isolated environment:

```sh
git clone git@github.com:hg199074jin/claude-worker-router.git
cd claude-worker-router
uv sync
```

Install the included Codex skill by linking or copying the `skill/` directory
to your Codex skills location. On macOS, for example:

```sh
ln -s "$(pwd)/skill" ~/.codex/skills/claude-worker-router
```

Copy the example configuration and replace the two absolute-path placeholders:

```sh
mkdir -p ~/.codex/model-router
cp config.example.toml ~/.codex/model-router/config.toml
```

The configuration contains no API token. Claude Code reads the credentials for
the provider currently selected in CC Switch.

## Use from Codex

The installed skill tells Codex when a task can be delegated. For a bounded
edit, Codex sends a single JSON request to the wrapper rather than calling
`claude -p` directly.

```sh
printf '%s' '{
  "repository": "/absolute/path/to/your-project",
  "task": "Fix the discount calculation.",
  "acceptance_criteria": ["A 25 percent discount is subtracted correctly."],
  "mode": "edit",
  "allowed_paths": ["src/pricing"],
  "test_commands": [["uv", "run", "python", "-m", "unittest", "-v"]]
}' | uv run claude-worker-router
```

For analysis only, use `read-only` mode and omit test commands:

```json
{
  "repository": "/absolute/path/to/your-project",
  "task": "Identify the likely cause of the failing checkout test.",
  "acceptance_criteria": ["Report findings and affected files."],
  "mode": "read-only",
  "allowed_paths": []
}
```

The command prints a `RunResult` JSON document. The normal edit success state
is `ready-for-review`; it does not automatically merge the worker commit into
your main checkout. Read-only success uses `read-only`. Any `escalated` result
includes an `escalation_reason` for Codex to take over.

## Configuration

`config.example.toml` contains the available limits. The important values are:

- `command`: a bare executable name such as `claude`, or an absolute path.
- `provider`: currently `cc-switch-current`; it represents your manually
  selected provider, not a stored provider profile.
- `max_turns` and `timeout_seconds`: hard limits for one worker run.
- `max_changed_files` and `max_diff_lines`: edit budget limits.
- `allowed_test_binaries`: the only executables the router will use for test
  commands.

Use small limits and narrow path scopes. If a task cannot be verified with a
bounded diff and a project-local test command, keep it with Codex.

## Development

Run the complete deterministic suite:

```sh
uv run --python 3.12 python -m unittest discover -v
```

The repository includes a live smoke test, which calls the provider currently
configured in Claude Code and may consume provider quota:

```sh
tests/live/run_smoke_test.sh
```

The latest committed verification evidence is in [VERIFICATION.md](VERIFICATION.md).
It contains no API tokens or token fragments.

## Project status

This is an early, deliberately narrow tool. It is designed for a personal
Codex + Claude Code workflow and favors clear escalation over silent autonomy.
Contributions and issue reports are welcome once a license and contribution
policy are chosen by the repository owner.

# Claude Worker Router

[![Tests](https://github.com/hg199074jin/claude-worker-router/actions/workflows/tests.yml/badge.svg)](https://github.com/hg199074jin/claude-worker-router/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)

[简体中文](README.zh-CN.md)

Claude Worker Router lets Codex use Claude Code as a bounded implementation
worker while Codex remains the coordinator and reviewer. The router calls the
provider you have **manually selected** in Claude Code / CC Switch—MiniMax,
GLM, or another compatible provider. It never stores provider profiles,
selects a model, or switches/falls back automatically.

```text
You manually select a provider in CC Switch
                    │
Codex ── structured JSON ──> Router ──> Claude Code
  │                              │             │
  └── review and approval         └── isolated Git worktree for edits
```

This is deliberately conservative: it is for small, reversible, testable
changes—not architecture decisions, credentials, production operations,
destructive work, or broad refactors.

## Choose the right route

| Task | Recommended owner | Why |
| --- | --- | --- |
| One focused bug fix with a local test | Claude Code worker | Bounded diff and observable acceptance checks |
| Narrow code review or investigation | Worker in `read-only` mode | No file-edit tool is exposed |
| Authentication, secrets, payment, production, infrastructure | Codex | Security or remote-write boundary |
| Cross-cutting design or unbounded refactor | Codex | Requires architectural judgement |
| Worker escalation, timeout, or permission failure | Codex | No hidden retry or provider switch occurs |

## Guarantees and non-goals

The router:

- reads one structured JSON task from standard input, keeping task text off
  command-line arguments;
- uses a fixed worker tool set without Bash access;
- creates a dedicated Git worktree for edit tasks; read-only tasks cannot
  edit files;
- requires `allowed_paths`, change budgets, and approved test argv arrays;
- runs approved tests itself with a minimal non-secret environment;
- records the immutable base SHA and redacted provider fingerprint;
- rejects unsafe tracked symlinks and binary changes by default;
- writes request/result/metadata/test output/full patch/event timeline and a
  SHA-256 manifest as durable run evidence;
- returns a structured escalation reason instead of silently changing model
  or retrying through another provider.

It does **not** choose or modify CC Switch / Claude Code provider settings,
auto-merge worker output, turn a worktree into an OS-level sandbox, or make
security-sensitive and production work safe to delegate.

## Quick start

### 1. Install prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Git
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview)
- CC Switch, or another *manual* Claude Code provider configuration

```sh
git clone git@github.com:hg199074jin/claude-worker-router.git
cd claude-worker-router
uv sync
```

### 2. Configure without copying credentials

```sh
mkdir -p ~/.codex/model-router
cp config.example.toml ~/.codex/model-router/config.toml
```

Set the `run_records` and `claude_settings` placeholders in the copied file.
Credentials remain in Claude Code / CC Switch; this router configuration has
no API key or token.

### 3. Verify the environment

```sh
uv run claude-worker-router doctor --json
```

`doctor` exits `0` when ready, `1` for warnings, and `2` when the router
cannot safely run. Resolve an error before starting a worker task.

### 4. Install the Codex routing skill

The bundled skill tells Codex when it may delegate and when it must keep a
task under Codex control:

```sh
ln -s "$(pwd)/skill" ~/.codex/skills/claude-worker-router
```

## The task contract

Every execution is one JSON object supplied through standard input. An edit
task needs repository-relative `allowed_paths` and at least one approved
project-local test command.

```json
{
  "repository": "/absolute/path/to/your-project",
  "task": "Correct the discount calculation in src/pricing/discount.py.",
  "acceptance_criteria": [
    "A 25 percent discount on 200 returns 150.",
    "The focused unit test exits 0."
  ],
  "mode": "edit",
  "allowed_paths": ["src/pricing/discount.py"],
  "test_commands": [["uv", "run", "python", "-m", "unittest", "-v"]]
}
```

```sh
printf '%s' '<task JSON>' | uv run claude-worker-router
```

`read-only` tasks cannot include test commands and receive no edit tools:

```json
{
  "repository": "/absolute/path/to/your-project",
  "task": "Identify the likely cause of the failing checkout test.",
  "acceptance_criteria": ["Report findings and affected files."],
  "mode": "read-only",
  "allowed_paths": []
}
```

Optional fields:

| Field | Meaning |
| --- | --- |
| `exclusive_tests` | When `true`, reserve a solo queue batch |
| `test_profile` | Use a named configuration profile instead of inline `test_commands` |

`test_profile` and `test_commands` are mutually exclusive. Requests that
supply model, settings, or provider overrides are rejected.

## Result, evidence, and integration

An edit task succeeds as `ready-for-review`; it does **not** merge anything.
Read-only success is `read-only`. An `escalated` result includes a precise
`escalation_reason`.

```text
pending → running → ready-for-review → integrated
                   └───────────────→ blocked / cancelled
```

Use this handoff instead of merging a worker branch directly:

1. Inspect the evidence with `show RUN_ID`.
2. Review the recorded patch, changed paths, test output, base SHA, and
   redacted provider metadata.
3. Give explicit approval to integrate.
4. Run `integrate RUN_ID`; it fast-forwards only.
5. Run `cleanup RUN_ID` when the isolated worktree is no longer needed.

```sh
claude-worker-router doctor [--repo PATH] [--json]
claude-worker-router list [--repo ...] [--status ...] [--limit N] [--json]
claude-worker-router show RUN_ID [--json]
claude-worker-router integrate RUN_ID
claude-worker-router cleanup RUN_ID [--discard]
claude-worker-router cleanup --stale [--stale-hours 168]
```

Integration rejects a dirty target checkout, base drift
(`integration-base-diverged`), failed tests, a missing worker branch, and an
evidence SHA-256 manifest mismatch. It never rebases, force-pushes, resolves
conflicts automatically, or creates a merge commit. Cleanup removes only
router-created isolation artifacts; evidence remains retained. Use
`--discard` to explicitly abandon an unintegrated change.

## Queue, cancellation, and recovery

```sh
printf '%s' '<task JSON>' | claude-worker-router submit
claude-worker-router queue --state pending --json
claude-worker-router drain [--once]
claude-worker-router cancel RUN_ID
```

`submit` accepts the same task JSON plus optional `priority` (higher drains
first) and `parent_run_id`; those queue fields live in SQLite only and never
enter task evidence. State is stored in `state.db` next to run records.

If a drainer dies mid-run, `doctor` surfaces `queue-health` and the next
drain marks the old task `blocked` with `runner-interrupted`. It is never
re-executed implicitly: submit a new run when you decide to retry. Cancelling
a running task targets the worker's dedicated process group—not your shell—and
preserves its worktree and evidence for review.

## Bounded concurrency

Set `max_concurrency = 2` to run at most two workers at once. The default is
`1`, and any value above `2` is rejected.

- Same-repository edit tasks share a batch only when their `allowed_paths`
  scopes are disjoint; different repositories never conflict.
- Each batch pins one provider fingerprint. If CC Switch changes during it,
  dispatch stops with exit code `5`, pending tasks remain queued, and no
  provider is selected automatically.
- `exclusive_tests: true` gives a task a batch of its own.
- Integration remains serialized per repository through an advisory lock.

Two slots are a deliberate ceiling for this version, not a worker farm.

## Policies and safety boundaries

Optional global and per-project policy files can only tighten the limits in
`config.toml`:

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

Numeric limits take the minimum, deny paths are unioned, and safety booleans
can only turn on. A project attempt to relax a global rule is rejected as
`policy-relaxation-rejected`; every run records policy fingerprints.

`.git` and `.claude-worker-router` are always denied for worker edits.
`sandbox_required = true` currently fails closed as `sandbox-unavailable`:
worktree isolation is **not** an OS-level sandbox. See
[the macOS feasibility research](docs/superpowers/research/2026-08-27-macos-sandbox-feasibility.md)
for the measured decision.

Treat every worker result as untrusted until reviewed. Keep secrets,
credential-dependent tests, infrastructure changes, destructive operations,
production work, and major design work with Codex.

## Configuration reference

Start with [config.example.toml](config.example.toml).

| Field | Purpose |
| --- | --- |
| `command` | Bare executable such as `claude`, or an absolute executable path |
| `provider` | Must be `cc-switch-current`; describes manual provider routing only |
| `max_turns`, `timeout_seconds`, `correction_limit` | Hard run and correction-loop limits |
| `max_changed_files`, `max_diff_lines` | Edit-diff budget |
| `allowed_test_binaries` | Executables allowed in test argv arrays |
| `run_records` | Durable evidence directory; default `~/.codex/model-router/runs` |
| `claude_settings` | Claude Code settings; default `~/.claude/settings.json` |
| `max_concurrency` | `1` by default, maximum `2` |
| `binary_edit_policy` | Only `"deny"` is supported |

Relative executable paths containing `/`, shell-string test commands, unsafe
paths, missing edit-test commands, and binary changes are rejected before the
worker can make a change.

## Troubleshooting

| Symptom | Meaning | Next action |
| --- | --- | --- |
| `doctor` exits 2 | A required local dependency or setting is unusable | Run `doctor --json` and resolve the named failing check |
| `worker-permission-denied` | The task needs a capability outside the fixed tool set | Narrow/redesign the task; do not grant broad shell access |
| `worker-timeout` / `worker-turn-limit` | Work exceeded its bounded budget | Let Codex take over; do not switch providers automatically |
| `external-symlink-denied` | A tracked symlink escapes, is broken, or cycles | Correct the repository link before delegation |
| `policy-path-denied` / `binary-change-denied` | A protected boundary was requested or changed | Keep work with Codex or deliberately adjust policy |
| `drain` exits 5 | Provider changed during a batch | Pending tasks stay queued; stabilize manual selection and drain later |
| `integration-base-diverged` | Target branch advanced since worker start | Review the new base and create a fresh run |

## Development and verification

```sh
PYTHONWARNINGS=error uv run --python 3.12 python -m unittest discover -v
uv build
```

The live smoke test creates a fresh temporary Git project, performs a
read-only run and an isolated edit, then checks test evidence and the main
worktree hash. It calls the provider currently selected in Claude Code and
may consume provider quota:

```sh
tests/live/run_smoke_test.sh
```

See [VERIFICATION.md](VERIFICATION.md) for retained verification records. They
contain no API tokens or token fragments.

## Scope and contribution status

This is a personal, provider-neutral Codex + Claude Code workflow tool. Its
design favors explicit escalation, human review, reproducible evidence, and
manual provider selection over autonomous throughput. No license or
contribution policy has been selected yet.

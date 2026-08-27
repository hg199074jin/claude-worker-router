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
- Records the immutable base SHA a worker change was built on.
- Fails closed on unsafe tracked symlinks (`external-symlink-denied`) before
  the worker is invoked, and denies binary changes by default
  (`binary-change-denied`).
- Captures redacted provider fingerprints plus a per-run evidence directory:
  request, result, metadata, tests, the full diff patch, an append-only
  event timeline, and a SHA-256 integrity manifest.
- Returns structured escalation reasons instead of silently retrying with a
  different provider.

## Run management commands

Beyond the stdin executor, V1.2 ships five subcommands that share one core:

```sh
claude-worker-router doctor [--repo PATH] [--json]  # diagnose the environment (0=READY 1=warnings 2=NOT READY)
claude-worker-router list [--repo ...] [--status ...] [--limit N] [--json]
claude-worker-router show RUN_ID [--json]           # review one run's evidence summary
claude-worker-router integrate RUN_ID               # verified fast-forward of an approved run
claude-worker-router cleanup RUN_ID [--discard]     # drop isolation artifacts; evidence is kept
claude-worker-router cleanup --stale                # report runs older than 168 hours
```

The normal lifecycle for an edit task is:
`ready-for-review` → `show` → human review → explicit approval →
`integrate` → `cleanup`. Integration refuses dirty main checkouts,
moved bases (`integration-base-diverged`), failing tests, or evidence whose
SHA-256 manifest no longer matches. There is no rebase, force push, or merge
commit — if the base moved, Codex decides what to do. Evidence directories
are permanent records; cleanup never deletes them.

## Queue and cancellation (V1.4)

For bursty work you can decouple submission from execution:

```sh
printf '%s' '{...task json...}' | claude-worker-router submit   # -> {"run_id","lifecycle":"pending"}
claude-worker-router queue [--state pending|running|...|all] [--json]
claude-worker-router drain [--once]     # single worker, strictly sequential
claude-worker-router cancel RUN_ID      # pending / running / ready-for-review
```

`submit` accepts the same JSON as stdin mode plus optional `priority`
(higher drains first) and `parent_run_id`; both live only in the state
database and never enter evidence. Lifecycle (`pending → running →
ready-for-review/integrated/blocked/cancelled`) is tracked in SQLite at
`state.db` next to your run records; restarts keep every state. A drainer
that died mid-run is surfaced by `doctor` as `queue-health` warning and
moved to blocked `runner-interrupted` on the next drain — re-execution
always requires a new run id. Cancelling a running task terminates the
worker's own process group (never your shell) while keeping its worktree
and evidence intact.

## Bounded concurrency (V1.5)

`drain` can run at most **two** workers at once when the configuration sets
`max_concurrency = 2` (default `1`; any value above 2 fails config
validation). Scheduling rules:

- Two edit tasks may share a batch only when their `allowed_paths` scopes
  are disjoint *within the same repository*; tasks on different repositories
  never conflict.
- Every batch runs under one provider fingerprint (an "epoch"). If CC Switch
  changes underneath it, dispatch stops immediately (exit code 5), pending
  tasks stay pending, and running ones finish under their own end-of-run
  verification — never an automatic switch.
- A task can opt out of batching with `"exclusive_tests": true`; exclusive
  runs get a batch of their own and nothing else joins it.
- `integrate` serializes per repository through an advisory file lock, so a
  concurrent drainer cannot mutate one main checkout from two places.

This is deliberately narrow: two slots exist to remove real waiting, not to
build a worker farm. The V1.5 design requires usage evidence (recent queues
showing ≥20% parallelizable work) before raising limits.

## Policy layer (V1.3)

Limits from `config.toml` are the operator's run cap; policies tighten
them further per machine and per project:

```sh
# ~/.codex/model-router/policy.toml            (global)
sandbox_required = false
[limits]
max_turns = 6
max_diff_lines = 400
[paths]
deny = ["secrets", "deployment/prod"]
```

```toml
# <repo>/.claude-worker-router/policy.toml     (project, commit it!)
[limits]
max_turns = 4
[paths]
deny = ["infra"]
```

Rules: numbers may only shrink (`min`), deny lists only grow (union), and
boolean safety requirements only turn on. A project file that tries to
relax a resolved global value fails immediately with
`policy-relaxation-rejected` — never silently clamped. Every run's
evidence records SHA-256 fingerprints of each loaded layer plus the
effective rule set, so any review can answer "under what policy did this
worker run?".

Additional hard edges added in V1.3: `.git` and `.claude-worker-router`
are always denied; changed files hitting a deny prefix escalate with
`policy-path-denied`; tasks may reference named `[test_profiles.*]`
entries from config via `"test_profile"` (mutually exclusive with inline
`test_commands`) — an `exclusive = true` profile feeds V1.5 batch
exclusivity; unknown names escalate as `test-profile-unknown`. Setting
`sandbox_required = true` currently fails closed with
`sandbox-unavailable`; the feasibility spike lives at
`docs/superpowers/research/2026-08-27-macos-sandbox-feasibility.md`.

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
- `binary_edit_policy`: only `"deny"` is accepted in V1.2 — worker runs that
  touch any binary file escalate instead of passing review with an
  unmeasurable diff.

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

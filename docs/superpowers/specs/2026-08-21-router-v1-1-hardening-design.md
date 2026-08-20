# Claude Worker Router V1.1 Hardening Design

## Goal

Make non-interactive Claude Code worker runs predictable and low-friction
without weakening worktree isolation, manual CC Switch provider selection, or
Codex review gates.

## Observed causes

The successful live worker run used `acceptEdits` and only `Read` plus `Edit`;
it completed without a permission denial. Most development-time interruptions
came from direct `claude -p` calls that started in Manual mode, Bash calls,
access outside the worker directory, oversized multi-stage tasks, and turn
limits. The user-level memory hook also injected unrelated context into the
worker session.

## Invocation policy

All delegated runs go through `scripts/run_worker.py`; Codex does not call
`claude -p` directly for implementation. The executor starts Claude Code with
`--safe-mode` so hooks, plugins, skills, and automatic memory do not add
unrelated context. Provider authentication and model selection continue to
come from the current CC Switch configuration; the executor never passes
`--model` or `--settings`.

Edit runs use `acceptEdits` and expose plus pre-approve only
`Read,Glob,Grep,Edit,Write`. Read-only runs use `dontAsk` and expose plus
pre-approve only `Read,Glob,Grep`. Neither mode exposes Bash. Because safe mode
does not auto-load project instruction files, each prompt requires the worker
to locate and read applicable `AGENTS.md` and `CLAUDE.md` files before acting.

## Read-only contract

Read-only mode runs in the requested directory without creating a worktree,
cannot receive file-edit tools, does not run project tests, does not create a
commit, and returns status `read-only` after a valid worker response. A provider
fingerprint change still escalates the result.

## Edit scope and integration

Edit mode continues to require a clean Git repository and an isolated sibling
worktree. `allowed_paths` is an enforced set of repository-relative path
prefixes, not prompt-only guidance. Absolute paths, empty components, and `..`
segments are rejected during request validation. After the worker and tests
finish, any changed path outside the declared scope escalates as
`path-scope-exceeded` and is never committed.

An edit with passing tests and no changed files returns `ready-for-review` with
`commit = null`; it does not attempt an empty Git commit. The worktree and run
evidence remain available for Codex review.

## Failure classification

The executor returns structured reasons instead of mapping every failure to a
provider outage:

- `worker-permission-denied` for denied or approval-required tool calls;
- `worker-turn-limit` for exhausted Claude Code turns;
- `worker-timeout` for the Claude subprocess timeout;
- `worker-output-invalid` for malformed Claude JSON output;
- `worker-launch-failed` when the command cannot start;
- `test-timeout` for timed-out executor-run tests;
- `provider-unreachable` for remaining non-zero provider/CLI failures.

Worktree creation and Git commit failures also become structured escalations
with retained evidence rather than uncaught CLI crashes.

## Prompt and task shape

Prompts state each rule once and contain only the task, acceptance criteria,
allowed paths, budgets, project-instruction requirement, and stop conditions.
Claude Code never runs tests, Git, version discovery, or run-record inspection;
the Python executor and Codex own those operations. Increasing `max_turns` is
not the primary remedy: a worker task must remain bounded enough to fit the
existing 12-turn default.

## Verification

Deterministic tests cover read-only tool isolation, permission flags, safe
mode, allowed-path enforcement, no-change commits, worker error
classification, test timeouts, and Git failures. Final validation runs the
complete unit suite, source and installed skill validators, and a bounded live
read-only plus one-file edit acceptance run using the provider currently
selected by the user in CC Switch. No provider switch or fallback is allowed.

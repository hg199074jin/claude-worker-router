---
name: claude-worker-router
description: Use before coding implementation, debugging, testing, refactoring, or code review in a Git project when bounded, reversible, testable work may be delegated to a provider-neutral Claude Code worker.
---

# Claude Code Worker Router

Use this skill as the policy Codex applies when deciding whether a coding
request should stay in Codex or be delegated to the Claude Code worker.

## Routing intent

- Perform shallow triage only. The worker is for execution, not for a second
  full design pass. Do not duplicate the deep solution Codex already worked
  through.
- Apply the hard Codex gates from the approved design. If any hard gate
  applies, Codex owns the work; do not delegate.
- When no hard gate applies and Codex is uncertain whether the task is
  worthwhile for the worker, prefer the worker. Reversible, bounded, and
  testable tasks are good worker candidates.
- "Claude Code worker" means the provider currently selected in CC Switch.
  Do not assume a specific provider unless the user names one explicitly.

## Hard Codex gates

A task must stay under Codex control, and may not be delegated to the worker,
when any of the following applies:

- authentication, authorization, secrets, cryptography, or security boundaries
- payment, billing, financial transfers, or regulated-data handling
- destructive data operations or irreversible migrations
- production deployment, infrastructure mutation, or remote write operations
- major architecture or public-interface decisions spanning multiple subsystems
- changes that cannot be checked with a bounded diff and observable tests
- an explicit user instruction to use only Codex

In any of these cases the worker may provide only a read-only second opinion;
Codex remains the owner of the work.

## Provider selection discipline

- Provider selection and switching are manual-only. Never load alternate
  provider settings and never attempt automatic fallback between providers.
- After a provider failure, return control to Codex. Any model change
  requires the user to switch CC Switch manually and confirm before another
  run starts.
- Codex does not introduce profile files, model overrides, or automatic
  fallback rules. The wrapper and skill stay neutral across providers.

## Announcing the route

- State the chosen route briefly before invoking the worker, so the user
  can interject if they disagree. Do not silently route or silently keep
  the work in Codex.

## Worker execution rules

- Require clean Git for any worker edits. Uncommitted or untracked changes
  block delegation.
- Every edit request must declare at least one repository-relative
  `allowed_paths` prefix. Never use an empty list as an unrestricted scope.
- Every edit request must include at least one approved project-local test
  command. Read-only requests include no test commands.
- After this skill is installed, never bypass the executor with a direct
  implementation call to `claude -p`. All delegated implementation and
  analysis requests go through `scripts/run_worker.py` so the permission,
  provider, evidence, and isolation checks are always applied.
- Send a single structured JSON request over stdin to
  `scripts/run_worker.py`. Do not pass the task as free-form prose.
- For edit mode, give the worker only repository reading and file-editing
  tools inside its isolated worktree. For read-only mode, expose only Read,
  Glob, and Grep; never expose Edit or Write.
- Supply test commands as argv arrays, and only after Codex has verified
  those commands are project-local tests. Do not pass commands Codex has
  not confirmed; binaries outside the configured allowlist are rejected before
  the worker is called.
- The worker does not run tests, Git commands, version discovery, or inspect
  run records outside its working directory. The Python executor runs tests
  and Git; Codex inspects global configuration and retained evidence.
- The executor checks the Claude command and provider configuration before
  creating an edit worktree, so local setup failures do not leave unused
  branches behind. Configure the command as a bare executable name such as
  `claude` or as an absolute path, never as a relative path containing `/`.
- Before the first worker call of a session (or after any environment
  change), prefer `claude-worker-router doctor` (optionally with `--repo`)
  to confirm the machine is READY; it exits 0 when healthy, 1 with
  warnings, 2 when unusable.
- Tracked symlinks are scanned before any worker invocation: a symlink that
  resolves outside the repository, is broken, or forms a cycle fails closed
  (`external-symlink-denied`) without invoking the worker. In edit mode a
  link inside `allowed_paths` must also resolve inside them.
- Binary changes are denied by default. If the worker adds, modifies, or
  deletes a binary file the run escalates with `binary-change-denied`,
  which takes precedence over scope verdicts because diff-line budgets are
  meaningless for binaries.
- Executor-run tests receive only a small non-secret environment allowlist.
  Tasks whose tests require credentials or sensitive host access stay with
  Codex under the hard security gate.
- Review the diff and test evidence Codex receives before integrating any
  worker change.
- Never auto-integrate on escalation or on a provider-fingerprint
  mismatch. Both conditions return control to Codex and require explicit
  user direction.
- Treat `worker-permission-denied`, `worker-turn-limit`, `worker-timeout`,
  `worker-output-invalid`, `worker-cli-failed`, `git-measure-failed`,
  `test-launch-failed`, `test-timeout`, `evidence-write-failed`,
  `binary-change-denied`, `external-symlink-denied`, and path-scope
  failures as distinct takeover reasons. Do not retry them as generic provider
  failures.

## Post-review lifecycle (V1.2)

A `ready-for-review` result is *not* finished work. Codex must close the
loop explicitly:

1. Inspect the run: `claude-worker-router list [--repo ...]` and
   `claude-worker-router show RUN_ID [--json]`. Evidence lives under
   `run_records/RUN_ID/` with metadata, tests, the full diff patch,
   an append-only event timeline, and a SHA-256 manifest.
2. Review the recorded diff and test evidence.
3. Get explicit user approval before touching main.
4. Integrate only through `claude-worker-router integrate RUN_ID`. Its
   preflight refuses dirty checkouts (`integration-dirty-checkout`),
   moved bases (`integration-base-diverged`), failed tests, evidence
   tampering, and missing worker branches. Integration is exclusively a
   fast-forward merge; there is no rebase, no force push, and no conflict
   auto-resolution.
5. Clean up with `claude-worker-router cleanup RUN_ID`. It removes only the
   isolation artifacts and never touches evidence. An unintegrated change
   requires an explicit `--discard`; `cleanup --stale` reports runs older
   than 168 hours instead of deleting anything silently.

Never bypass these steps with a direct `git merge` of the worker branch --
doing so skips exactly the checks the router exists to provide.

## Per-task overrides

- Honor explicit per-task user instructions:
  - "只用 Codex" — Codex handles the task end-to-end; do not delegate.
  - "优先交给 Claude Code" — prefer the worker unless a hard gate blocks.
  - "worker 只分析" — worker may analyze but not edit; Codex owns any
    follow-up implementation.

## Isolation is not a security boundary

- State clearly that worktree isolation is not an OS-level security
  sandbox. Treat worker output the same way as any other untrusted code
  review: diff review, test evidence, and explicit integration approval
  are still required.

## When not to use this skill

- Non-coding requests, pure chat, and tasks with no Git repository
  context. Route those normally without consulting this skill.

---
name: claude-worker-router
description: Route coding implementation, debugging, testing, refactoring, and code-review work between Codex and a provider-neutral Claude Code worker. Use before implementation when a Git project may benefit from delegating bounded, reversible, testable work; keep high-risk or architectural work under Codex control.
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
- Send a single structured JSON request over stdin to
  `scripts/run_worker.py`. Do not pass the task as free-form prose.
- Supply test commands as argv arrays, and only after Codex has verified
  those commands are project-local tests. Do not pass commands Codex has
  not confirmed.
- Review the diff and test evidence Codex receives before integrating any
  worker change.
- Never auto-integrate on escalation or on a provider-fingerprint
  mismatch. Both conditions return control to Codex and require explicit
  user direction.

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
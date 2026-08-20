# Verified V1.1 — Claude Code Worker Router

This document records the end-to-end evidence for the hardened V1.1 router.
It covers deterministic tests, source and installed-skill validation, a
read-only live call, an isolated edit live call, and the non-secret provider
fingerprint retained in the run records.

No authentication token value, full environment dump, or provider secret
is included anywhere in this document.

## Verification Metadata

- Verification date: 2026-08-21
- Worktree path: `/Volumes/ORICO/Projects/claude-worker-router/.worktrees/router-implementation`
- Branch: `codex-worker-router/implementation`

## Tool Versions

| Tool          | Version                                                 |
| ------------- | ------------------------------------------------------- |
| Python        | 3.12.14                                                 |
| uv            | 0.12.5 (Homebrew 2026-08-14, aarch64-apple-darwin)      |
| Git           | 2.55.0                                                  |
| Claude Code   | 2.1.234                                                  |

## Current Provider (CC Switch) — Non-secret Fingerprint Only

| Field        | Value                  |
| ------------ | ---------------------- |
| Endpoint host | `api.minimaxi.com`    |
| Model         | `MiniMax-M3[1M]`      |

The full CC Switch provider mapping and any authentication token are
intentionally not reproduced here. The router configuration contains only
non-secret routing settings.

## Deterministic Suite

Command:

```
/opt/homebrew/bin/uv run --python 3.12 python -m unittest discover -s tests -v
```

Result: **48 tests, all PASS**.

Modules exercised: `test_config_provider`, `test_executor_cli`,
`test_git_workspace`.

## Source and Installed Skill Validation

Command:

```
/opt/homebrew/bin/uv run --no-project --python 3.12 --with pyyaml python \
  /Users/sandro/.codex/skills/.system/skill-creator/scripts/quick_validate.py skill
```

Result: `Skill is valid!`

The same validator was run against the installed symlink at
`/Users/sandro/.codex/skills/claude-worker-router`; result: `Skill is valid!`.

The suite includes regressions for edit/read-only tool separation, path-scope
enforcement before and after failed runs, worker and executor failure
classification, post-failure provider fingerprint checks, test timeouts, empty
commits, Python cache suppression, minimal test environments, and structured
Git measurement failures. It also verifies that a third-party model
compatibility warning without a valid result is classified as
`worker-output-invalid`, not as a provider outage. Direct in-process request
construction cannot bypass path normalization, and path-scope violations stay
the primary audit reason when a provider change is detected in the same run.
Preflight tests also prove that missing worker commands and invalid provider
configuration stop before worktree creation; missing test executables and
unwritable run-record locations return structured results instead of crashing.
Edit requests without tests, read-only requests containing ignored tests, and
test binaries outside the allowlist are rejected before consuming a worker
call.

## Live Read-Only Run

| Field              | Value                                                                |
| ------------------ | -------------------------------------------------------------------- |
| Run identifier      | `529b56c0c572479b92f59011be633bc5`                                  |
| Status              | `read-only`                                                          |
| Attempts            | 1                                                                    |
| Commit              | none                                                                 |
| Executor-run tests  | none                                                                 |
| Run record          | `/Users/sandro/.codex/model-router/runs/529b56c0c572479b92f59011be633bc5` |

The task repository hash was identical before and after the call. The Claude
session used only `Glob` and `Read`; no `Edit`, `Write`, or `Bash` call was
made.

## Live Isolated Edit Run

| Field              | Value                                                                |
| ------------------ | -------------------------------------------------------------------- |
| Run identifier      | `bd03173129f948b3b85e2c2ec77ba7a2`                                  |
| Status              | `ready-for-review`                                                  |
| Attempts            | 1                                                                   |
| Changed files       | `discount.py`                                                       |
| Diff lines          | 2                                                                   |
| Worker commit       | `baa39147791d30fad7103f5a6197093e139434a6`                         |
| Smoke root          | `/var/folders/cj/byj4hb2j0c1d9jydpcvtd9t40000gn/T/claude-worker-smoke.XXXXXX.DdWXwRwd6Z` |
| Run record          | `/Users/sandro/.codex/model-router/runs/bd03173129f948b3b85e2c2ec77ba7a2` |

## Main-Checkout Isolation Proof

- Main-checkout fixture hash (`tests/live/fixture/discount.py`) was
  recorded before and after the worker invocation.
- The hash before the run equals the hash after the run.
- Only the worker's isolated worktree (`smoke root` plus the
  `.codex-worktrees/discount/<run-id>` directory) was modified.
- The worker's commit diff for `discount.py` swaps the buggy
  `return price + price * (percent / 100.0)` for
  `return price - price * (percent / 100.0)` — i.e. it changes `+` to `-`
  in `compute_price`.

## Test Execution Evidence

- Test argv:
  `['uv', 'run', '--python', '3.12', 'python', '-m', 'unittest', '-v']`
- Test exit code: `0`
- Test stdout: `Ran 1 test in 0.000s — OK`
  (`test_twenty_five_percent_discount_on_two_hundred` passed)
- Test stderr: includes the standard unittest `... ok` line for
  `DiscountTests.test_twenty_five_percent_discount_on_two_hundred`.
- The executor invoked these tests with `shell=False`; no shell expansion
  of the argv occurred.
- The child test process ran with `PYTHONDONTWRITEBYTECODE=1`, so the test did
  not create an out-of-scope `__pycache__` directory.
- The child test environment is built from a small non-secret allowlist rather
  than copied from the parent. Regression tests verify that Anthropic, AWS,
  GitHub, and database credentials are absent.

## Permission and Hook Evidence

- Both successful smoke calls used `--safe-mode`, so user settings, MCP
  servers, skills, and hooks were not loaded for the worker session.
- The two successful smoke session logs contain zero
  `hook_additional_context` records.
- The final edit session used `Glob`, `Read`, and `Edit` only. It made no Bash
  call and produced no approval prompt or user interruption.
- The executor, not the model, owned the approved test and Git operations.

A subsequent broad read-only review consumed its bounded turns without
producing a valid result. It made no edits and exposed the compatibility-warning
classification edge case covered by the new deterministic regression. It was
not retried and no provider was switched automatically.

## Compatibility Warning

No `unrecognized_model` compatibility warning was observed in the run
record for this run. (The current provider model is fingerprinted and
accepted.)

## Token Handling Statement

No API token value, token hash, or token prefix is written to router run
records, the smoke artifacts, or this verification document. The provider
fingerprint includes only the non-secret `endpoint_host` and `model` fields,
and provider credentials are withheld from executor-run tests.

## Files Used by This Verification

- `tests/live/fixture/discount.py`
- `tests/live/fixture/test_discount.py`
- `tests/live/run_smoke_test.sh`
- `VERIFICATION.md`

The live smoke directory, both router run records, and the isolated worker
worktree are intentionally retained for review. They are outside the project
worktree and are not integrated automatically.

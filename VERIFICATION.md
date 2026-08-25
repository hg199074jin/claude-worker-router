# Verified V1.1 — Claude Code Worker Router

This document records the end-to-end evidence for the hardened V1.1 router.
It covers deterministic tests, source and installed-skill validation, a
read-only live call, an isolated edit live call, and the non-secret provider
fingerprint retained in the run records.

No authentication token value, full environment dump, or provider secret
is included anywhere in this document.

## Verification Metadata

- Verification date: 2026-08-21
- Verified commit: `4e6d9c5a82a17afea3cf4b32cc92fd3c3681d638`

## Tool Versions

| Tool          | Version                                                 |
| ------------- | ------------------------------------------------------- |
| Python        | 3.12.14                                                 |
| uv            | 0.12.5 (Homebrew 2026-08-14, aarch64-apple-darwin)      |
| Git           | 2.55.0                                                  |
| Claude Code   | 2.1.234                                                  |

## Provider Used During Verification — Non-secret Fingerprint Only

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

Result: **51 tests, all PASS**.

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
Relative worker command paths are rejected, and the executable resolved during
preflight is the same absolute file used for the worker invocation.
Direct `TestCommand` construction applies the same non-empty argv validation as
JSON request parsing, so internal callers cannot bypass the preflight contract.

## Live Read-Only Run

| Field              | Value                                                                |
| ------------------ | -------------------------------------------------------------------- |
| Run identifier      | `2d6fa2a1577f46989baadbf6befe7a0b`                                  |
| Status              | `read-only`                                                          |
| Attempts            | 1                                                                    |
| Commit              | none                                                                 |
| Executor-run tests  | none                                                                 |
| Run record          | `~/.codex/model-router/runs/2d6fa2a1577f46989baadbf6befe7a0b` |

The task repository hash was identical before and after the call. The Claude
session used only `Glob` and `Read`; no `Edit`, `Write`, or `Bash` call was
made.

## Live Isolated Edit Run

| Field              | Value                                                                |
| ------------------ | -------------------------------------------------------------------- |
| Run identifier      | `e34455b939ee462cbd5b4d8f7b5150e0`                                  |
| Status              | `ready-for-review`                                                  |
| Attempts            | 1                                                                   |
| Changed files       | `discount.py`                                                       |
| Diff lines          | 2                                                                   |
| Worker commit       | `c9b75759f173118d15c6794ba21a051a6d6bd0ca`                         |
| Smoke root          | a temporary directory outside the repository |
| Run record          | `~/.codex/model-router/runs/e34455b939ee462cbd5b4d8f7b5150e0` |

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
worktree were retained for review. They were outside the project worktree and
were not integrated automatically.

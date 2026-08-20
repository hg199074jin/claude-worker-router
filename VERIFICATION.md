# Verified Baseline — Claude Code Worker Router

This document records the end-to-end evidence for Task 6 of the
Claude Code Worker Router implementation. It is the single human-facing
summary of the deterministic suite, the bounded live CC Switch provider
call, and the provider fingerprint retained from the run record.

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

Result: **17 tests, all PASS** (3.735s).

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

## Live Smoke Run (Bounded Single Call)

| Field              | Value                                                                |
| ------------------ | -------------------------------------------------------------------- |
| Run identifier      | `5ddc3aaee7c548368348a72e3d69378a`                                  |
| Status              | `ready-for-review`                                                  |
| Attempts            | 1                                                                   |
| Worker commit       | `db348162c7cca879f71b378ca9fb1f07617f4426`                         |
| Smoke root          | `/var/folders/cj/byj4hb2j0c1d9jydpcvtd9t40000gn/T/claude-worker-smoke.XXXXXX.07D8b4PFBZ` |
| Run record          | `/Users/sandro/.codex/model-router/runs/5ddc3aaee7c548368348a72e3d69378a` |

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

## Compatibility Warning

No `unrecognized_model` compatibility warning was observed in the run
record for this run. (The current provider model is fingerprinted and
accepted.)

## Token Handling Statement

No API token value, token hash, or token prefix was recorded, logged,
inspected, stored, or otherwise retained by the worker router, the
smoke driver, or this verification document. The provider fingerprint
includes only the non-secret `endpoint_host` and `model` fields.

## Files Added by This Verification

- `tests/live/fixture/discount.py`
- `tests/live/fixture/test_discount.py`
- `tests/live/run_smoke_test.sh`
- `VERIFICATION.md`

Transient artefacts (`tests/live/fixture/__pycache__/` and
`tests/live/smoke_stdout.log`) are intentionally not committed:
- `__pycache__/` is excluded by the project `.gitignore`.
- `smoke_stdout.log` is a transient run output that the brief instructs
  not to commit.

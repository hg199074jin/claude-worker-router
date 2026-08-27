# macOS Sandbox Feasibility Spike — Claude Worker Router

Date: 2026-08-27 · Version target: V1.3 (Task 16) · Verdict: **NOT READY**

## Scope of the spike

Assess whether V1.3 can wrap the Claude Code worker in an OS-level sandbox
on macOS (`/usr/bin/sandbox-exec`, the "seatbelt" mechanism) such that all
six feasibility gates from the design hold simultaneously:

1. Claude Code starts and completes a non-interactive run;
2. the isolated worktree stays writable;
3. paths outside the repository (notably `~/.ssh`, `~/.codex`,
   `~/.claude`) are not writable;
4. provider network access still works for the CC-Switch-selected endpoint;
5. Claude configuration remains readable;
6. no credential material is copied into temp/world-readable files.

## What was measured on this machine

| Probe                                   | Result                                   |
| --------------------------------------- | ---------------------------------------- |
| `/usr/bin/sandbox-exec` exists          | yes (macOS 26.5.1, arm64)                |
| trivial `(allow default)` profile runs  | yes — child printed output, exit 0       |
| Apple documentation for profile syntax  | none; SBPF is undocumented               |
| deprecation status                      | Apple marks `sandbox-exec` deprecated    |

So *raw capability* is present on this host. That is the only green cell.

## Why the verdict is NOT READY for V1.3 gating

- **Undocumented, moving language.** SBPF has no stable spec; filters
  change across macOS releases and Claude Code versions. A profile that
  passes today may deny a required syscall after either update.
- **Invariant proof cost is open-ended.** Gate 3+6 together require
  enumerating every write/read path Claude Code touches (provider caches,
  extension hosts, shell snapshots) per release. Falsifying one missing
  path only surfaces at run time as a worker failure.
- **Fail-closed direction must not degrade availability silently.** A
  wrongly strict profile converts healthy runs into mysterious escalations,
  which contradicts the Evidence-over-intelligence principle: unexplained
  failures destroy auditability.
- **Worktree isolation already bounds blast radius** for text-only edits,
  while binary/symlink/deny-path layers added in V1.2/V1.3 cover the main
  escape hatches without kernel coupling.

## Shipped behavior in V1.3

* `policy.toml` accepts `sandbox_required`; global/project layers fold with
  true-wins semantics.
* When any layer requires the sandbox, `execute_task` fails closed with
  structured reason ``sandbox-unavailable`` before invoking the worker.
  The gate consults `platform/macos_sandbox.is_sandbox_enforced()`, which
  this version hardwires to False (`EXPERIMENTAL_VERDICT = "NOT READY"`).
* The probe `is_sandbox_available()` reports raw host capability for
  tooling/diagnostics only.

## Path to SUPPORTED (future task)

1. Build the minimal SBPF template in this module; run the six-gate
   checklist against three consecutive Claude Code releases.
2. Add an integration harness that fails CI when any gate regresses.
3. Re-issue this research doc with measured evidence per gate; only then
   flip `is_sandbox_enforced()` behind a config knob.

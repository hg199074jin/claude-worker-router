#!/usr/bin/env python3
"""Deterministic fake Claude executable for executor tests.

Reads the worker prompt from standard input, performs a fixture edit based on
the ``FAKE_CLAUDE_BEHAVIOR`` environment variable, and emits deterministic JSON
output. The fake never reads credentials, never shells out, and is invoked by
``executor._invoke_worker`` exactly the way the real Claude binary would be
called (argv list, ``shell=False``).

Environment variables consumed:

* ``FAKE_CLAUDE_BEHAVIOR`` -- one of ``fix``, ``fail-then-fix``, ``always-fail``,
  ``provider-change``, ``provider-error``.
* ``FAKE_CLAUDE_WORKTREE`` -- absolute path of the worktree to edit (defaults to
  the current working directory).
* ``FAKE_CLAUDE_SETTINGS_PATH`` -- absolute path of the provider settings file
  that ``provider-change`` appends whitespace to.
* ``FAKE_CLAUDE_INVOCATION_COUNTER`` -- file whose integer contents are
  incremented after each invocation, exposing the count via
  ``FixtureRun.claude_invocation_count``.
* ``FAKE_CLAUDE_ARGV_LOG`` -- file where the fake writes its raw argv JSON so
  tests can assert against the executor's command-line contract.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


_PROMPT_TARGET = "example.txt"
_OK_RESULT = {"type": "result", "result": "ok", "is_error": False}


def _log_argv(path_str: str) -> None:
    if not path_str:
        return
    Path(path_str).write_text(json.dumps(sys.argv), encoding="utf-8")


def _bump_counter(path_str: str) -> int:
    if not path_str:
        return 0
    counter = Path(path_str)
    count = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
    count += 1
    counter.write_text(str(count), encoding="utf-8")
    return count


def _resolve_worktree() -> Path:
    worktree = os.environ.get("FAKE_CLAUDE_WORKTREE")
    return Path(worktree) if worktree else Path(os.getcwd())


def _maybe_modify_settings(behavior: str) -> None:
    settings_path = os.environ.get("FAKE_CLAUDE_SETTINGS_PATH")
    if not settings_path:
        return
    path = Path(settings_path)
    if not path.exists():
        return
    if behavior == "provider-change":
        # Mutate a parsed field so ``fingerprint_provider`` detects the change.
        # The fingerprint is computed from the parsed ``ProviderSnapshot`` dict,
        # not from raw bytes, so editing ANTHROPIC_MODEL is what matters.
        raw = path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        env = data.get("env")
        if not isinstance(env, dict):
            return
        env["ANTHROPIC_MODEL"] = f"{env.get('ANTHROPIC_MODEL', 'Test-Model')}-changed"
        path.write_text(json.dumps(data), encoding="utf-8")


def _maybe_apply_edit(behavior: str, count: int, worktree: Path) -> None:
    target = worktree / _PROMPT_TARGET
    if behavior == "always-fail":
        return
    if behavior == "fix":
        target.write_text("worker\n", encoding="utf-8")
        return
    if behavior == "fail-then-fix":
        if count >= 2:
            target.write_text("worker\n", encoding="utf-8")
        return
    if behavior == "provider-change":
        # Provider-change deliberately leaves the worktree untouched so the
        # test fails the smoke check and the executor escalates on
        # provider-configuration-changed before considering commit.
        return


def main() -> int:
    # Drain stdin so the pipe does not block when the test omits a prompt.
    sys.stdin.read()

    _log_argv(os.environ.get("FAKE_CLAUDE_ARGV_LOG", ""))
    count = _bump_counter(os.environ.get("FAKE_CLAUDE_INVOCATION_COUNTER", ""))

    behavior = os.environ.get("FAKE_CLAUDE_BEHAVIOR", "")
    worktree = _resolve_worktree()

    if behavior == "provider-error":
        sys.stderr.write("connection refused\n")
        return 7

    _maybe_modify_settings(behavior)
    _maybe_apply_edit(behavior, count, worktree)

    sys.stdout.write(json.dumps(_OK_RESULT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
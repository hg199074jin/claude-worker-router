"""Policy loader tests (V1.3 Task 12).

Global policies live at ``~/.codex/model-router/policy.toml``; project
policies at ``<repo>/.claude-worker-router/policy.toml``. Files are
optional; unknown keys are rejected (typos must never silently drop a
constraint); and the resolved effective policy is the fold
base(config) ← global ← project under tighten-only semantics.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claude_worker_router.policy import (
    PolicyRelaxationRejected,
    RouterPolicy,
    default_global_policy_path,
    load_policy_file,
    resolve_effective_policy,
)


def _write(tmp: Path, relative: str, body: str) -> Path:
    path = tmp / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.strip(), encoding="utf-8")
    return path


# TOML quirk: a bare key belongs to the LAST opened table, so the root-level
# ``sandbox_required`` must sit above any [table] header.
FULL_BODY = """
sandbox_required = false

[limits]
max_turns = 6
timeout_seconds = 900
max_changed_files = 3
max_diff_lines = 200

[paths]
deny = ["secrets", "deployment/prod"]
""".strip()


class LoadPolicyFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="policy-loader-"))
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, True)

    def test_full_document_parses(self) -> None:
        path = _write(self.tmp, "policy.toml", FULL_BODY)
        policy = load_policy_file(path)
        self.assertEqual(policy.max_turns, 6)
        self.assertEqual(policy.timeout_seconds, 900)
        self.assertEqual(policy.max_changed_files, 3)
        self.assertEqual(policy.max_diff_lines, 200)
        self.assertEqual(policy.deny_paths, ("deployment/prod", "secrets"))
        self.assertFalse(policy.sandbox_required)

    def test_partial_document_keeps_defaults_for_missing_axes(self) -> None:
        path = _write(
            self.tmp,
            "partial.toml",
            "[limits]\nmax_diff_lines = 100\n[paths]\ndeny = [\"secrets\"]\n",
        )
        policy = load_policy_file(
            path,
            defaults=RouterPolicy(
                max_turns=10,
                timeout_seconds=600,
                max_changed_files=4,
                max_diff_lines=400,
            ),
        )
        # Missing axes inherit the defaults instead of failing resolution.
        self.assertEqual(policy.max_turns, 10)
        self.assertEqual(policy.max_diff_lines, 100)

    def test_unknown_keys_are_rejected(self) -> None:
        path = _write(
            self.tmp,
            "typo.toml",
            '[limits]\nmax_diff_linez = 100\n',
        )
        with self.assertRaisesRegex(ValueError, "unknown"):
            load_policy_file(path)

    def test_project_scope_entries_must_be_safe(self) -> None:
        path = _write(
            self.tmp,
            "unsafe.toml",
            '[paths]\ndeny = ["../outside"]\n',
        )
        with self.assertRaises(ValueError):
            load_policy_file(path)


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="policy-resolve-"))
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, True)

    BASE = RouterPolicy(
        max_turns=12,
        timeout_seconds=1200,
        max_changed_files=5,
        max_diff_lines=500,
    )

    def test_no_files_yields_none_layers(self) -> None:
        loaded = resolve_effective_policy(self.BASE, self.tmp / "nope.toml", None)
        self.assertIsNone(loaded.global_policy)
        self.assertIsNone(loaded.project_policy)

    def test_fold_base_global_then_project(self) -> None:
        global_path = _write(self.tmp, "global.toml", FULL_BODY)
        project_path = _write(
            self.tmp,
            "repo/.claude-worker-router/policy.toml",
            '[paths]\ndeny = ["infra"]\n[limits]\nmax_turns = 4\n',
        )
        result = resolve_effective_policy(self.BASE, global_path, project_path.parent.parent)

        self.assertIsNotNone(result.global_policy)
        self.assertEqual(result.effective.max_turns, 4)
        self.assertEqual(result.effective.max_diff_lines, 200)
        self.assertEqual(
            result.effective.deny_paths,
            ("deployment/prod", "infra", "secrets"),
        )

    def test_project_relaxation_of_resolved_global_is_rejected(self) -> None:
        global_path = _write(self.tmp, "global.toml", FULL_BODY)
        project_path = _write(
            self.tmp,
            "repo/.claude-worker-router/policy.toml",
            "[limits]\nmax_turns = 99\n",
        )
        with self.assertRaises(PolicyRelaxationRejected):
            resolve_effective_policy(
                self.BASE, global_path, project_path.parent.parent
            )

    def test_default_global_path_location(self) -> None:
        self.assertTrue(
            str(default_global_policy_path()).endswith(
                ".codex/model-router/policy.toml"
            )
        )


if __name__ == "__main__":
    unittest.main()

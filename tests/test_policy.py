"""Policy data model tests (V1.3 Task 11).

The policy hierarchy is

    Built-in invariants > global policy > project policy > task request

Numbers may only shrink (minimum wins), deny lists only grow (union wins),
and a boolean safety requirement can only turn on (true wins). Project
policies that try to RELAX the global layer are refused loudly rather than
silently clamped.
"""

from __future__ import annotations

import unittest

from claude_worker_router.policy import (
    PolicyRelaxationRejected,
    RouterPolicy,
    merge_policy,
)


def _policy(**overrides) -> RouterPolicy:
    base = dict(
        max_turns=12,
        timeout_seconds=1200,
        max_changed_files=5,
        max_diff_lines=500,
        deny_paths=("secrets",),
        sandbox_required=False,
    )
    base.update(overrides)
    return RouterPolicy(**base)


class MergePolicyTests(unittest.TestCase):
    def test_merge_with_missing_project_takes_global(self) -> None:
        effective = merge_policy(_policy(), None)
        self.assertEqual(effective.max_turns, 12)
        self.assertEqual(effective.deny_paths, ("secrets",))
        self.assertFalse(effective.sandbox_required)

    def test_numeric_fields_take_the_minimum(self) -> None:
        effective = merge_policy(
            _policy(max_turns=12, timeout_seconds=1200),
            _policy(max_turns=8, timeout_seconds=900),
        )
        self.assertEqual(effective.max_turns, 8)
        self.assertEqual(effective.timeout_seconds, 900)

    def test_deny_paths_union_wins_and_is_sorted_unique(self) -> None:
        effective = merge_policy(
            _policy(deny_paths=("secrets", "infra")),
            _policy(deny_paths=("secrets", "deployment/prod")),
        )
        self.assertEqual(
            effective.deny_paths,
            ("deployment/prod", "infra", "secrets"),
        )

    def test_boolean_true_wins(self) -> None:
        self.assertTrue(
            merge_policy(
                _policy(sandbox_required=False),
                _policy(sandbox_required=True),
            ).sandbox_required
        )
        self.assertTrue(
            merge_policy(
                _policy(sandbox_required=True),
                _policy(sandbox_required=False),
            ).sandbox_required
        )

    def test_project_relaxing_a_number_is_rejected_loudly(self) -> None:
        with self.assertRaisesRegex(PolicyRelaxationRejected, "max_turns"):
            merge_policy(
                _policy(max_turns=8),
                _policy(max_turns=20),
            )

    def test_path_normalization_rejects_traversal_entries(self) -> None:
        for unsafe in ("/abs", "../up", "a//b", "./x", ""):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(ValueError):
                    _policy(deny_paths=(unsafe,))


if __name__ == "__main__":
    unittest.main()

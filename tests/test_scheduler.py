"""Bounded-concurrency scheduling tests (V1.5 Tasks 22–26).

Pure conflict logic lives in :mod:`claude_worker_router.scheduler`; the
drain-side integration tests live further down this module once the
concurrent runner lands (Tasks 23–25).
"""

from __future__ import annotations

import unittest

from claude_worker_router.scheduler import paths_conflict


class PathConflictEngineTests(unittest.TestCase):
    def test_identical_paths_conflict(self) -> None:
        self.assertTrue(paths_conflict(("src/core",), ("src/core",)))

    def test_ancestor_conflicts_in_both_directions(self) -> None:
        self.assertTrue(paths_conflict(("src/core",), ("src/core/models",)))
        self.assertTrue(paths_conflict(("src/core/models",), ("src/core",)))

    def test_disjoint_paths_do_not_conflict(self) -> None:
        self.assertFalse(paths_conflict(("src/backend",), ("web/frontend",)))
        self.assertFalse(
            paths_conflict(
                ("src/core/a",), ("src/cored/b",)
            )  # prefix must respect component boundary
        )

    def test_multi_entry_sets_detect_any_overlap(self) -> None:
        self.assertTrue(paths_conflict(("a", "b"), ("b", "c")))
        self.assertFalse(paths_conflict(("a", "b"), ("c", "d")))

    def test_component_boundary_is_respected(self) -> None:
        # "src" is NOT a prefix of "srcx/file".
        self.assertFalse(paths_conflict(("src",), ("srcx/file.py",)))

    def test_empty_scope_means_read_only_never_conflicts(self) -> None:
        self.assertFalse(paths_conflict((), ("src",)))
        self.assertFalse(paths_conflict(("src",), ()))


if __name__ == "__main__":
    unittest.main()

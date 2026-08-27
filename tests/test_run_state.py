"""Run lifecycle model tests (V1.4 Task 17).

V1.4 separates *lifecycle* (where a run stands in its management pipeline)
from *execution outcome* (what happened when it ran). The legacy stdin API
is untouched: ``RunResult.status`` keeps its exact V1.x values; lifecycle
mapping happens one layer up.
"""

from __future__ import annotations

import unittest

from claude_worker_router.models import (
    TERMINAL_LIFECYCLES,
    RunLifecycle,
    assert_lifecycle_transition,
    lifecycle_from_outcome,
)


class RunLifecycleTests(unittest.TestCase):
    def test_lifecycle_values_are_exact_lowercase_strings(self) -> None:
        self.assertEqual(RunLifecycle.PENDING.value, "pending")
        self.assertEqual(RunLifecycle.RUNNING.value, "running")
        self.assertEqual(
            RunLifecycle.READY_FOR_REVIEW.value, "ready-for-review"
        )
        self.assertEqual(RunLifecycle.INTEGRATED.value, "integrated")
        self.assertEqual(RunLifecycle.BLOCKED.value, "blocked")
        self.assertEqual(RunLifecycle.CANCELLED.value, "cancelled")

    def test_terminal_states_are_blocked_cancelled_integrated(self) -> None:
        self.assertEqual(
            frozenset(TERMINAL_LIFECYCLES),
            {
                RunLifecycle.BLOCKED,
                RunLifecycle.CANCELLED,
                RunLifecycle.INTEGRATED,
            },
        )

    def test_legal_transitions_are_accepted(self) -> None:
        legal = [
            (RunLifecycle.PENDING, RunLifecycle.RUNNING),
            (RunLifecycle.PENDING, RunLifecycle.CANCELLED),
            (RunLifecycle.RUNNING, RunLifecycle.READY_FOR_REVIEW),
            (RunLifecycle.RUNNING, RunLifecycle.BLOCKED),
            (RunLifecycle.RUNNING, RunLifecycle.CANCELLED),
            (RunLifecycle.READY_FOR_REVIEW, RunLifecycle.INTEGRATED),
            (RunLifecycle.READY_FOR_REVIEW, RunLifecycle.CANCELLED),
        ]
        for current, target in legal:
            with self.subTest(current=current.value, target=target.value):
                assert_lifecycle_transition(current, target)

    def test_illegal_transitions_are_rejected(self) -> None:
        illegal = [
            (RunLifecycle.PENDING, RunLifecycle.INTEGRATED),
            (RunLifecycle.PENDING, RunLifecycle.READY_FOR_REVIEW),
            (RunLifecycle.CANCELLED, RunLifecycle.RUNNING),
            (RunLifecycle.CANCELLED, RunLifecycle.PENDING),
            (RunLifecycle.BLOCKED, RunLifecycle.RUNNING),
            (RunLifecycle.INTEGRATED, RunLifecycle.CANCELLED),
            (RunLifecycle.RUNNING, RunLifecycle.INTEGRATED),
        ]
        for current, target in illegal:
            with self.subTest(current=current.value, target=target.value):
                with self.assertRaises(ValueError):
                    assert_lifecycle_transition(current, target)

    def test_success_result_maps_to_ready_for_review_with_distinct_outcome(self) -> None:
        # The design example: a successful read-only run keeps lifecycle
        # ready-for-review while outcome records what actually happened.
        self.assertEqual(
            lifecycle_from_outcome("read-only"), RunLifecycle.READY_FOR_REVIEW
        )
        self.assertEqual(
            lifecycle_from_outcome("ready-for-review"),
            RunLifecycle.READY_FOR_REVIEW,
        )

    def test_escalated_result_maps_to_blocked(self) -> None:
        self.assertEqual(lifecycle_from_outcome("escalated"), RunLifecycle.BLOCKED)

    def test_unknown_outcome_is_rejected_not_guessed(self) -> None:
        with self.assertRaises(ValueError):
            lifecycle_from_outcome("totally-bogus")


if __name__ == "__main__":
    unittest.main()

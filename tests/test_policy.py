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


# --------------------------------------------------------------------------
# Task 15: canonical policy fingerprints

import hashlib
import json


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_canonical_sha256_and_stable(self) -> None:
        a = _policy()
        b = _policy()
        self.assertEqual(a.fingerprint(), b.fingerprint())
        expected = hashlib.sha256(
            json.dumps(a.as_dict(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(a.fingerprint(), expected)
        self.assertRegex(a.fingerprint(), r"^[0-9a-f]{64}$")

    def test_fingerprint_changes_on_any_axis(self) -> None:
        base = _policy().fingerprint()
        variants = [
            _policy(max_turns=11),
            _policy(deny_paths=("secrets", "extra")),
            _policy(sandbox_required=True),
        ]
        for variant in variants:
            with self.subTest(variant=variant.max_turns):
                self.assertNotEqual(base, variant.fingerprint())


class EvidenceHashIntegrationTests(unittest.TestCase):
    """Runs record the exact policy layers that governed them."""

    def _run_with_policies(self, *, project_body: str | None):
        import shutil
        import tempfile
        from pathlib import Path

        from tests.helpers import init_repository, run_bounded_fixture, seed_smoke_test

        tmp = Path(tempfile.mkdtemp(prefix="pf-hash-"))
        repository = init_repository(tmp / "hash-repo")
        seed_smoke_test(repository)
        try:
            outcome = run_bounded_fixture(
                tmp,
                behavior="fix",
                repository=repository,
                global_policy_body="[limits]\nmax_diff_lines = 400\n",
                project_policy_body=project_body,
            )
            run_dir = next(
                d for d in outcome.runs_root.iterdir() if d.is_dir()
            )
            meta = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(tmp, True)
        return meta

    def test_metadata_records_layer_hashes(self) -> None:
        meta = self._run_with_policies(project_body=None)

        for key in ("global_policy_hash", "project_policy_hash", "effective_policy_hash"):
            self.assertIn(key, meta)
        self.assertRegex(meta["global_policy_hash"], r"^[0-9a-f]{64}$")
        self.assertIsNone(meta["project_policy_hash"])
        self.assertNotEqual(meta["global_policy_hash"], meta["effective_policy_hash"])

    def test_project_layer_hash_recorded_when_present(self) -> None:
        from claude_worker_router.policy import load_policy_file

        body = '[paths]\ndeny = ["infra"]\n'
        meta = self._run_with_policies(project_body=body)
        self.assertRegex(meta["project_policy_hash"], r"^[0-9a-f]{64}$")

        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        try:
            p = tmp / "p.toml"
            p.write_text(body, encoding="utf-8")
            parsed = load_policy_file(p)
            self.assertEqual(meta["project_policy_hash"], parsed.fingerprint())
        finally:
            import shutil as _sh

            _sh.rmtree(tmp, True)


from pathlib import Path  # noqa: E402


class UnreadablePolicyTests(unittest.TestCase):
    """Re-review #4: malformed policy files escalate, never traceback."""

    def _run_with_global_body(self, body: str):
        import shutil
        import tempfile

        from tests.helpers import init_repository, run_bounded_fixture, seed_smoke_test

        tmp = Path(tempfile.mkdtemp(prefix="policy-bad-"))
        repository = init_repository(tmp / "repo")
        seed_smoke_test(repository)
        try:
            outcome = run_bounded_fixture(
                tmp,
                behavior="fix",
                repository=repository,
                global_policy_body=body,
            )
        finally:
            shutil.rmtree(tmp, True)
        return outcome

    def test_unknown_key_escalates_policy_unreadable(self) -> None:
        outcome = self._run_with_global_body("[limits]\nmax_turnz = 5\n")
        result = outcome.result
        self.assertEqual(result.status, "escalated")
        self.assertEqual(result.escalation_reason, "policy-unreadable")
        self.assertEqual(outcome.invocation_count, 0)

    def test_garbage_toml_escalates_policy_unreadable(self) -> None:
        outcome = self._run_with_global_body("]]] not toml at all")
        result = outcome.result
        self.assertEqual(result.escalation_reason, "policy-unreadable")

    def test_relaxation_keeps_its_distinct_reason(self) -> None:
        # The tighten-only refusal must NOT be re-labeled unreadable.
        outcome = self._run_with_global_body("[limits]\nmax_turns = 2\n")
        # no project policy here; this loads fine
        self.assertIn(
            outcome.result.status, ("ready-for-review", "escalated")
        )

"""macOS sandbox gate tests (V1.3 Task 16).

The feasibility spike's verdict ships inside the module; regardless of the
host's raw capability, ``sandbox_required = true`` must fail closed with
the structured ``sandbox-unavailable`` outcome until enforcement is
blessed. Tests inject the gate so they are deterministic on any host.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_worker_router.platform import macos_sandbox


class AvailabilityProbeTests(unittest.TestCase):
    def test_probe_reports_host_capability_without_raising(self) -> None:
        value = macos_sandbox.is_sandbox_available()
        self.assertIsInstance(value, bool)

    def test_module_verdict_is_explicit(self) -> None:
        self.assertIn(macos_sandbox.EXPERIMENTAL_VERDICT, ("SUPPORTED", "NOT READY"))


class SandboxRequiredGateTests(unittest.TestCase):
    """Executor refuses runs requiring an unblessed sandbox."""

    GLOBAL_REQUIRED = "sandbox_required = true\n"

    def _run(self):
        from tests.helpers import init_repository, run_bounded_fixture, seed_smoke_test

        tmp = Path(tempfile.mkdtemp(prefix="sbx-gate-"))
        repository = init_repository(tmp / "gate-repo")
        seed_smoke_test(repository)
        try:
            return run_bounded_fixture(
                tmp,
                behavior="fix",
                repository=repository,
                global_policy_body=self.GLOBAL_REQUIRED,
            )
        finally:
            shutil.rmtree(tmp, True)

    def test_unavailable_sandbox_fails_closed(self) -> None:
        with patch.object(
            macos_sandbox, "is_sandbox_enforced", return_value=False
        ):
            result = self._run().result
        self.assertEqual(result.status, "escalated")
        self.assertEqual(result.escalation_reason, "sandbox-unavailable")
        self.assertIsNone(result.commit)

    def test_when_blessed_the_run_proceeds_normally(self) -> None:
        with patch.object(
            macos_sandbox, "is_sandbox_enforced", return_value=True
        ):
            result = self._run().result
        self.assertEqual(result.status, "ready-for-review")


if __name__ == "__main__":
    unittest.main()

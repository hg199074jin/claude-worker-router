"""Named test profiles (V1.3 Task 14).

A task may reference a configured profile (``test_profile``) instead of
inlining ``test_commands``; the two are mutually exclusive. Profiles are
argv arrays (never shell strings), are executed by the router against the
same binary allowlist, and an ``exclusive = true`` profile upgrades the
run to V1.5 batch-exclusivity without needing the transitional request
flag.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from claude_worker_router.config import load_config


def _base_config(tmp: Path, extra_tables: str = "") -> Path:
    path = tmp / "config.toml"
    path.write_text(
        """
[worker]
command = "claude"
provider = "cc-switch-current"
max_turns = 12
timeout_seconds = 1200
correction_limit = 1
max_changed_files = 5
max_diff_lines = 500
allowed_test_binaries = ["uv", "python3"]
"""
        + extra_tables,
        encoding="utf-8",
    )
    return path


class ProfileConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="profiles-cfg-"))
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, True)

    def test_valid_profile_table_parses(self) -> None:
        cfg = load_config(
            _base_config(
                self.tmp,
                """
[test_profiles.python-unit]
commands = [
  ["uv", "run", "python", "-m", "unittest"],
]
exclusive = false
""",
            )
        )
        self.assertIn("python-unit", cfg.test_profiles)
        profile = cfg.test_profiles["python-unit"]
        self.assertFalse(profile.exclusive)
        self.assertEqual(profile.commands[0].argv[0], "uv")

    def test_exclusive_flag_parses(self) -> None:
        cfg = load_config(
            _base_config(
                self.tmp,
                """
[test_profiles.e2e]
commands = [["uv", "run", "pytest"]]
exclusive = true
""",
            )
        )
        self.assertTrue(cfg.test_profiles["e2e"].exclusive)

    def test_missing_profile_table_is_empty(self) -> None:
        cfg = load_config(_base_config(self.tmp))
        self.assertEqual(cfg.test_profiles, {})

    def test_rejects_shell_string_commands(self) -> None:
        path = _base_config(
            self.tmp,
            """
[test_profiles.bad]
commands = ["uv run pytest"]
""",
        )
        with self.assertRaisesRegex(ValueError, "argv"):
            load_config(path)

    def test_rejects_unknown_entry_keys(self) -> None:
        path = _base_config(
            self.tmp,
            """
[test_profiles.bad]
commands = [["uv"]]
shell = true
""",
        )
        with self.assertRaisesRegex(ValueError, "unknown"):
            load_config(path)


class ProfileRequestTests(unittest.TestCase):
    def test_request_accepts_profile_instead_of_commands(self) -> None:
        from claude_worker_router.models import RunMode, TaskRequest

        request = TaskRequest.from_dict(
            {
                "repository": "/tmp/x",
                "task": "use profile",
                "mode": "edit",
                "test_profile": "python-unit",
                "allowed_paths": ["src"],
            }
        )
        self.assertEqual(request.test_profile, "python-unit")
        self.assertEqual(request.to_dict()["test_profile"], "python-unit")

    def test_request_rejects_both_forms_at_once(self) -> None:
        from claude_worker_router.models import RunMode, TaskRequest

        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            TaskRequest.from_dict(
                {
                    "repository": "/tmp/x",
                    "task": "ambiguous",
                    "mode": "edit",
                    "test_commands": [["uv"]],
                    "test_profile": "python-unit",
                    "allowed_paths": ["src"],
                }
            )


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# Executor + scheduler behavior with profiles

from tests.helpers import run_bounded_fixture  # noqa: E402

GOOD_PROFILE = {"python-unit": (("uv", "run", "python", "-c", "pass"),)}
EXCLUSIVE_PROFILE = {"own-machine": (("uv", "run", "python", "-c", "pass"), True)}


class ProfileExecutorTests(unittest.TestCase):
    def test_known_profile_executes_tests(self) -> None:
        import shutil

        tmp = Path(tempfile.mkdtemp(prefix="profiles-run-"))
        from tests.helpers import init_repository, seed_smoke_test

        repository = init_repository(tmp / "prof-repo")
        seed_smoke_test(repository)
        try:
            outcome = run_bounded_fixture(
                tmp,
                behavior="fix",
                repository=repository,
                test_profiles_config=GOOD_PROFILE,
                test_profile_name="python-unit",
            )
            result = outcome.result
            first_test = result.tests[0]
        finally:
            shutil.rmtree(tmp, True)

        self.assertEqual(result.status, "ready-for-review")
        self.assertEqual(first_test["argv"], ["uv", "run", "python", "-c", "pass"])

    def test_unknown_profile_escalates_without_worker_call(self) -> None:
        import shutil

        tmp = Path(tempfile.mkdtemp(prefix="profiles-bad-"))
        from tests.helpers import init_repository, seed_smoke_test

        repository = init_repository(tmp / "prof-bad")
        seed_smoke_test(repository)
        try:
            outcome = run_bounded_fixture(
                tmp,
                behavior="fix",
                repository=repository,
                test_profiles_config={},
                test_profile_name="no-such-profile",
            )
            result = outcome.result
            count = outcome.invocation_count
        finally:
            shutil.rmtree(tmp, True)

        self.assertEqual(result.status, "escalated")
        self.assertEqual(result.escalation_reason, "test-profile-unknown")
        self.assertEqual(count, 0)

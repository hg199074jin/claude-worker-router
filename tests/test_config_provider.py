import json
import tempfile
import unittest
from pathlib import Path

from claude_worker_router.config import load_config
from claude_worker_router.models import RunMode, TaskRequest, TestCommand


class ConfigTests(unittest.TestCase):
    def test_loads_bounded_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
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
allowed_test_binaries = ["uv", "python3", "npm"]
""".strip(),
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.command, "claude")
            self.assertEqual(config.provider, "cc-switch-current")
            self.assertEqual(config.max_turns, 12)
            self.assertEqual(config.correction_limit, 1)

    def test_binary_edit_policy_defaults_to_deny_and_only_accepts_deny(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
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
allowed_test_binaries = ["uv"]
""".strip(),
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.binary_edit_policy, "deny")

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
allowed_test_binaries = ["uv"]

binary_edit_policy = "allow"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, 'must be "deny"'):
                load_config(path)

    def test_rejects_relative_worker_command_with_path_separator(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[worker]
command = "./tools/claude"
provider = "cc-switch-current"
max_turns = 12
timeout_seconds = 1200
correction_limit = 1
max_changed_files = 5
max_diff_lines = 500
allowed_test_binaries = ["uv"]
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError,
                "bare executable name or an absolute path",
            ):
                load_config(path)

    def test_rejects_missing_task_statement(self):
        with self.assertRaisesRegex(ValueError, "task must be non-empty"):
            TaskRequest.from_dict(
                {
                    "repository": "/tmp/example",
                    "task": "",
                    "acceptance_criteria": ["tests pass"],
                    "mode": "edit",
                    "test_commands": [],
                }
            )

    def test_test_commands_are_argv_arrays(self):
        request = TaskRequest.from_dict(
            {
                "repository": "/tmp/example",
                "task": "fix the parser",
                "acceptance_criteria": ["tests pass"],
                "mode": "edit",
                "test_commands": [["uv", "run", "python", "-m", "unittest"]],
                "allowed_paths": ["src"],
            }
        )
        self.assertEqual(request.test_commands[0].argv[0], "uv")

    def test_direct_test_command_cannot_bypass_argv_validation(self):
        for unsafe_argv in ((), ("",), ("uv", "")):
            with self.subTest(unsafe_argv=unsafe_argv):
                with self.assertRaisesRegex(ValueError, "non-empty argv array"):
                    TestCommand(argv=unsafe_argv)

    def test_edit_request_requires_at_least_one_allowed_path(self):
        with self.assertRaisesRegex(ValueError, "edit mode requires allowed_paths"):
            TaskRequest.from_dict(
                {
                    "repository": "/tmp/example",
                    "task": "fix the parser",
                    "acceptance_criteria": ["tests pass"],
                    "mode": "edit",
                    "test_commands": [],
                    "allowed_paths": [],
                }
            )

    def test_edit_request_requires_at_least_one_test_command(self):
        with self.assertRaisesRegex(ValueError, "edit mode requires test_commands"):
            TaskRequest.from_dict(
                {
                    "repository": "/tmp/example",
                    "task": "fix the parser",
                    "acceptance_criteria": ["tests pass"],
                    "mode": "edit",
                    "test_commands": [],
                    "allowed_paths": ["src"],
                }
            )

    def test_read_only_request_allows_empty_allowed_paths(self):
        request = TaskRequest.from_dict(
            {
                "repository": "/tmp/example",
                "task": "review the parser",
                "acceptance_criteria": [],
                "mode": "read-only",
                "test_commands": [],
                "allowed_paths": [],
            }
        )
        self.assertEqual(request.allowed_paths, ())

    def test_read_only_request_rejects_ignored_test_commands(self):
        with self.assertRaisesRegex(ValueError, "read-only mode does not accept"):
            TaskRequest.from_dict(
                {
                    "repository": "/tmp/example",
                    "task": "review the parser",
                    "acceptance_criteria": [],
                    "mode": "read-only",
                    "test_commands": [["uv", "run", "python", "-m", "unittest"]],
                    "allowed_paths": [],
                }
            )

    def test_direct_task_request_cannot_bypass_path_normalization(self):
        for unsafe_path in ("", "src//file.py", "src/./file.py", "../outside.py"):
            with self.subTest(unsafe_path=unsafe_path):
                with self.assertRaisesRegex(ValueError, "allowed_paths"):
                    TaskRequest(
                        repository=Path("/tmp/example"),
                        task="fix the parser",
                        acceptance_criteria=(),
                        mode=RunMode.EDIT,
                        test_commands=(),
                        allowed_paths=(unsafe_path,),
                    )

    def test_rejects_provider_override_fields(self):
        for forbidden in ("provider_profile", "settings", "model"):
            data = {
                "repository": "/tmp/example",
                "task": "fix the parser",
                "acceptance_criteria": ["tests pass"],
                "mode": "edit",
                "test_commands": [],
                forbidden: "alternate-provider",
            }
            with self.subTest(forbidden=forbidden):
                with self.assertRaisesRegex(ValueError, "provider selection is manual-only"):
                    TaskRequest.from_dict(data)

    def test_rejects_unsafe_allowed_paths(self):
        for unsafe_path in (
            "/absolute/path.py",
            "../outside.py",
            "src/../secret.py",
            "src//file.py",
            "src/./file.py",
            "src/",
            ".",
        ):
            with self.subTest(unsafe_path=unsafe_path):
                with self.assertRaisesRegex(ValueError, "allowed_paths must be relative"):
                    TaskRequest.from_dict(
                        {
                            "repository": "/tmp/example",
                            "task": "fix the parser",
                            "acceptance_criteria": ["tests pass"],
                            "mode": "edit",
                            "test_commands": [],
                            "allowed_paths": [unsafe_path],
                        }
                    )


from claude_worker_router.provider import fingerprint_provider, read_provider_snapshot


class ProviderTests(unittest.TestCase):
    def test_provider_snapshot_omits_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_AUTH_TOKEN": "secret-value",
                            "ANTHROPIC_BASE_URL": "https://api.example.test/anthropic",
                            "ANTHROPIC_MODEL": "Example-Model",
                            "ANTHROPIC_DEFAULT_SONNET_MODEL": "Example-Model",
                        }
                    }
                ),
                encoding="utf-8",
            )
            snapshot = read_provider_snapshot(path)
            serialized = json.dumps(snapshot.as_dict(), sort_keys=True)
            self.assertEqual(snapshot.endpoint_host, "api.example.test")
            self.assertEqual(snapshot.model, "Example-Model")
            self.assertNotIn("secret-value", serialized)
            self.assertNotIn("AUTH_TOKEN", serialized)
            self.assertEqual(fingerprint_provider(snapshot), fingerprint_provider(snapshot))


if __name__ == "__main__":
    unittest.main()

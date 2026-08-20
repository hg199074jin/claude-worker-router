import json
import tempfile
import unittest
from pathlib import Path

from claude_worker_router.config import load_config
from claude_worker_router.models import TaskRequest


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
            }
        )
        self.assertEqual(request.test_commands[0].argv[0], "uv")

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

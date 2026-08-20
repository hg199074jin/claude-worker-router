import tempfile
import unittest
from pathlib import Path

from claude_worker_router.git_workspace import DirtyCheckoutError, GitWorkspace, ScopeExceededError
from tests.helpers import init_repository


class GitWorkspaceTests(unittest.TestCase):
    def test_worker_edit_does_not_change_main_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = init_repository(Path(tmp) / "repo")
            workspace = GitWorkspace.create(repository, "run-001")
            (workspace.path / "example.txt").write_text("worker\n", encoding="utf-8")
            self.assertEqual((repository / "example.txt").read_text(encoding="utf-8"), "main\n")
            self.assertEqual(
                workspace.measure_changes(5, 500, ("example.txt",)).files,
                ("example.txt",),
            )

    def test_rejects_dirty_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = init_repository(Path(tmp) / "repo")
            (repository / "example.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaises(DirtyCheckoutError):
                GitWorkspace.create(repository, "run-002")

    def test_enforces_file_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = init_repository(Path(tmp) / "repo")
            workspace = GitWorkspace.create(repository, "run-003")
            (workspace.path / "one.txt").write_text("one\n", encoding="utf-8")
            (workspace.path / "two.txt").write_text("two\n", encoding="utf-8")
            with self.assertRaises(ScopeExceededError):
                workspace.measure_changes(
                    max_files=1,
                    max_diff_lines=500,
                    allowed_paths=("one.txt", "two.txt"),
                )


if __name__ == "__main__":
    unittest.main()

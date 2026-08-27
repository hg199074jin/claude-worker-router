"""Per-repository integration lock tests (V1.5 Task 26).

Integration stays serial per repository even when workers run in
parallel. The lock lives at ``<state-dir>/locks/<sha1(realpath)>.lock``
and is advisory ``flock``: a second integrator for the same repository
gets an immediate refusal (`RepositoryBusy`) instead of deadlocking;
different repositories never block each other.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from claude_worker_router.scheduler import (
    RepositoryBusy,
    repository_integration_lock,
)


class RepositoryLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="repo-lock-"))
        self.addCleanup(self._cleanup)
        self.lock_root = self.tmp / "locks"

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, True)

    def _acquire(self, repo: str):
        return repository_integration_lock(self.lock_root, Path(repo))

    def test_second_acquire_same_repo_refused_until_released(self) -> None:
        repo = self.tmp / "repo-a"
        repo.mkdir()
        with self._acquire(str(repo)):
            with self.assertRaises(RepositoryBusy):
                with self._acquire(str(repo)):
                    pass
        # released -> acquirable again
        with self._acquire(str(repo)):
            pass

    def test_different_repos_do_not_block(self) -> None:
        (self.tmp / "r1").mkdir()
        (self.tmp / "r2").mkdir()
        with self._acquire(str(self.tmp / "r1")):
            with self._acquire(str(self.tmp / "r2")):
                pass

    def test_realpath_normalization_prevents_alias_bypass(self) -> None:
        real = self.tmp / "alias-target"
        real.mkdir()
        alias = self.tmp / "alias-link"
        alias.symlink_to(real, target_is_directory=True)

        with self._acquire(str(alias)):
            with self.assertRaises(RepositoryBusy):
                with self._acquire(str(real)):
                    pass

    def test_cross_process_holders_conflict(self) -> None:
        import os
        import time

        real = self.tmp / "proc-repo"
        real.mkdir()

        project_src = str(
            Path(__file__).resolve().parent.parent / "src"
        )
        holder_code = (
            "import sys, time\n"
            "sys.path.insert(0, r'%s')\n"
            "from pathlib import Path\n"
            "from claude_worker_router.scheduler import repository_integration_lock\n"
            "with repository_integration_lock(Path(r'%s'), Path(r'%s')):\n"
            "    print('HELD', flush=True)\n"
            "    time.sleep(2)\n"
            % (project_src, self.lock_root, real)
        )
        env = dict(os.environ, PYTHONPATH=project_src)
        proc = subprocess.Popen(
            [sys.executable, "-c", holder_code],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            assert proc.stdout is not None
            line = proc.stdout.readline().strip()
            assert line == "HELD", line

            with self.assertRaises(RepositoryBusy):
                with self._acquire(str(real)):
                    pass
        finally:
            proc.wait(timeout=10)

        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with self._acquire(str(real)):
                    break
            except RepositoryBusy:
                time.sleep(0.1)
        else:
            self.fail("lock stayed held after holder exited")


if __name__ == "__main__":
    unittest.main()

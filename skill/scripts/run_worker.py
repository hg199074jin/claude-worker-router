#!/usr/bin/env python3
from pathlib import Path
import os
import subprocess
import sys


def main() -> int:
    project = Path(__file__).resolve().parents[2]
    command = [
        "uv",
        "run",
        "--project",
        str(project),
        "--python",
        "3.12",
        "claude-worker-router",
        *sys.argv[1:],
    ]
    completed = subprocess.run(command, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, env=os.environ.copy())
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
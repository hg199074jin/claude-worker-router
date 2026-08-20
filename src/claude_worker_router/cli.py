"""Command-line entry point for the Claude Code worker router.

The CLI reads exactly one JSON object from standard input, loads the router
configuration, runs the executor, and prints the resulting JSON to standard
output. Exit codes follow the brief:

* ``0`` -- ``ready-for-review`` or read-only success
* ``2`` -- validation/configuration error
* ``3`` -- escalated worker result
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import RouterConfig, default_config_path, load_config
from .executor import execute_task
from .models import TaskRequest


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        raw = sys.stdin.read()
    except OSError as exc:
        print(f"unable to read stdin: {exc}", file=sys.stderr)
        return 2

    try:
        request_data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON on stdin: {exc}", file=sys.stderr)
        return 2

    if not isinstance(request_data, dict):
        print("stdin JSON must decode to an object", file=sys.stderr)
        return 2

    config_path = Path(args.config) if args.config else default_config_path()
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        request = TaskRequest.from_dict(request_data)
    except ValueError as exc:
        print(f"invalid request: {exc}", file=sys.stderr)
        return 2

    result = execute_task(request, config)
    sys.stdout.write(json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False))
    sys.stdout.write("\n")

    if result.status in ("ready-for-review", "read-only"):
        return 0
    return 3


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="claude-worker-router",
        description="Run a bounded Claude worker and emit a reviewable RunResult.",
    )
    parser.add_argument(
        "--config",
        help="path to the router config.toml (defaults to ~/.codex/model-router/config.toml)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
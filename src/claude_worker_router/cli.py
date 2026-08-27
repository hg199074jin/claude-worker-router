"""Command-line entry point for the Claude Code worker router.

Two invocation styles share this entry point:

* Legacy stdin mode (no subcommand) -- reads exactly one JSON object from
  standard input, runs the executor, and prints the resulting JSON. Exit
  codes: ``0`` success, ``2`` validation/configuration error, ``3``
  escalated worker result.
* V1.2 subcommands (``doctor``, ``list``, ``show``, ``integrate``,
  ``cleanup``) -- dispatched through ``_dispatch_command``. Commands that
  their own V1.2 tasks have not implemented yet fail closed with exit
  code ``2`` instead of silently succeeding.
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

    if args.command is None:
        return _run_legacy(Path(args.config) if args.config else default_config_path())
    return _dispatch_command(args)


def _run_legacy(config_path: Path) -> int:
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


def _dispatch_command(args: argparse.Namespace) -> int:
    """Route a parsed subcommand; unbuilt commands fail closed with exit 2."""
    try:
        config = load_config(Path(args.config) if args.config else default_config_path())
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    handler = _COMMAND_HANDLERS.get(args.command)
    if handler is None:
        print(
            f"error: '{args.command}' is not implemented yet "
            "(planned for a later V1.2 task)",
            file=sys.stderr,
        )
        return 2
    return handler(args, config)


def _noop_command(args: argparse.Namespace, config: RouterConfig) -> int:
    print(f"error: '{args.command}' is not implemented yet", file=sys.stderr)
    return 2


#: Placeholder handlers; each V1.2 task replaces its own entry.
_COMMAND_HANDLERS: dict[str, object] = {
    "doctor": _noop_command,
    "list": _noop_command,
    "show": _noop_command,
    "integrate": _noop_command,
    "cleanup": _noop_command,
}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="claude-worker-router",
        description=(
            "Run a bounded Claude worker and emit a reviewable RunResult. "
            "Without a subcommand, one JSON request is read from stdin."
        ),
    )
    parser.add_argument(
        "--config",
        help="path to the router config.toml (defaults to ~/.codex/model-router/config.toml)",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("doctor", help="diagnose the local router environment")
    subparsers.add_parser("list", help="list recorded worker runs")
    show = subparsers.add_parser("show", help="show one recorded run")
    show.add_argument("run_id")
    integrate = subparsers.add_parser("integrate", help="integrate a reviewed run")
    integrate.add_argument("run_id")
    cleanup = subparsers.add_parser("cleanup", help="remove a finished run's worktree")
    cleanup.add_argument("run_id")

    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())

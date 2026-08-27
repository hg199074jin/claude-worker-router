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
import os
import sys
from pathlib import Path

from .config import RouterConfig, default_config_path, load_config
from .doctor import DoctorCheck, overall_status, render_json, run_doctor
from .executor import execute_task
from .models import TaskRequest
from .run_store import validate_run_id


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
        if args.command == "doctor":
            # Doctor must be able to diagnose its own configuration failure.
            failure = [DoctorCheck("router-config", "error", str(exc))]
            return _run_doctor_command_checks(failure, json_mode=args.json)
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


def _doctor_command(args: argparse.Namespace, config: RouterConfig) -> int:
    repository = Path(args.repo).expanduser().resolve() if args.repo else None
    checks = run_doctor(config, repository=repository)
    return _run_doctor_command_checks(checks, json_mode=args.json)


def _run_doctor_command_checks(
    checks: list[DoctorCheck], *, json_mode: bool
) -> int:
    overall = overall_status(checks)
    if json_mode:
        sys.stdout.write(render_json(checks, overall))
        sys.stdout.write("\n")
    else:
        symbols = {"ok": "✓", "warning": "!", "error": "✗"}
        for check in checks:
            sys.stdout.write(
                f"{symbols.get(check.status, '?')} {check.name}: {check.detail}\n"
            )
        verdict = {
            "ok": "READY",
            "warning": "READY WITH WARNINGS",
            "error": "NOT READY",
        }[overall]
        sys.stdout.write(f"\n{verdict}\n")
    if overall == "ok":
        return 0
    return 1 if overall == "warning" else 2


def _list_command(args: argparse.Namespace, config: RouterConfig) -> int:
    """List recorded runs, newest first."""
    from .run_store import RunStore

    try:
        limit = int(args.limit) if args.limit is not None else None
        if limit is not None and limit < 0:
            raise ValueError("--limit must be non-negative")
    except ValueError as exc:
        print(f"invalid --limit: {exc}", file=sys.stderr)
        return 2

    listing = RunStore(config.run_records).list_runs(
        repository=os.path.realpath(os.path.expanduser(args.repo)) if args.repo else None,
        status=args.status,
        limit=limit,
    )
    for warning in listing.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if args.json:
        sys.stdout.write(
            json.dumps(
                {"runs": listing.rows, "warnings": listing.warnings},
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        )
        return 0

    header = f"{'RUN ID':<36} {'TIME':<24} {'STATUS':<18} {'MODE':<10} {'FILES':>5} {'DIFF':>6} REPOSITORY"
    sys.stdout.write(header + "\n")
    for row in listing.rows:
        created = (row.get("created_at") or "-")[:19].replace("T", " ")
        status = str(row.get("status") or "-")
        if row.get("escalation_reason"):
            status = f"{status}:{row['escalation_reason']}"
        repository = str(row.get("repository") or "-")
        sys.stdout.write(
            f"{str(row['run_id']):<36} {created:<24} {status:<18} "
            f"{str(row.get('mode') or '-'):<10} {row.get('changed_files', 0):>5} "
            f"{row.get('diff_lines', 0):>6} {repository}\n"
        )
    return 0


def _show_command(args: argparse.Namespace, config: RouterConfig) -> int:
    """Show one recorded run; validation errors and misses exit 2."""
    from .run_store import RunNotFoundError, RunStore, validate_run_id

    store = RunStore(config.run_records)
    try:
        validate_run_id(args.run_id)
    except ValueError as exc:
        print(f"invalid run id: {exc}", file=sys.stderr)
        return 2
    try:
        record = store.load_run(args.run_id)
    except RunNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"cannot load run {args.run_id}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload = {
            "metadata": record["metadata"],
            "request": record["request"],
            "result": record["result"],
        }
        sys.stdout.write(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
        return 0

    metadata = record["metadata"]
    request = record["request"]
    result = record["result"]

    def emit(label: str, value: object) -> None:
        sys.stdout.write(f"{label:<20} {value}\n")

    run_dir = record.get("run_dir", "-")
    emit("Run", Path(run_dir).name)
    emit("Task", request.get("task") or "-")
    for index, criterion in enumerate(request.get("acceptance_criteria") or [], 1):
        emit(f"Criterion {index}", criterion)
    emit("Repository", metadata.get("repository") or request.get("repository") or "-")
    emit("Base SHA", metadata.get("base_sha") or "-")
    provider = metadata.get("provider") or {}
    if isinstance(provider, dict):
        emit(
            "Provider",
            f"{provider.get('endpoint_host', '-')}/{provider.get('model', '-')}",
        )
    else:
        emit("Provider", "-")
    emit("Attempts", metadata.get("attempts", result.get("attempts", "-")))
    changed = metadata.get("changed_files") or result.get("changed_files") or []
    emit("Changed Files", ", ".join(changed) if changed else "(none)")
    tests = record["result"].get("tests") or []
    for index, test in enumerate(tests, 1):
        argv = test.get("argv") or []
        emit(
            f"Test {index}",
            f"exit={test.get('exit_code')} argv={' '.join(argv)}",
        )
    diff_lines = metadata.get("diff_lines", result.get("diff_lines", "-"))
    emit("Diff Lines", diff_lines)
    outcome = metadata.get("final_status") or result.get("status") or "-"
    emit("Outcome", outcome)
    escalation = (
        metadata.get("escalation_reason")
        if metadata.get("escalation_reason") is not None
        else result.get("escalation_reason")
    )
    emit("Escalation", escalation or "(none)")
    integration_bits = [
        str(bit)
        for bit in (metadata.get("integrated_at"), metadata.get("integrated_sha"))
        if bit
    ]
    emit("Integration", " ".join(integration_bits) if integration_bits else "(not integrated)")
    worktree = metadata.get("worktree") or "-"
    emit("Worktree", worktree)
    return 0


def _integrate_command(args: argparse.Namespace, config: RouterConfig) -> int:
    """Run the verified fast-forward integration for one reviewed run."""
    from .integration import IntegrationError, integrate_run

    try:
        validate_run_id(args.run_id)
    except ValueError as exc:
        print(f"invalid run id: {exc}", file=sys.stderr)
        return 2
    try:
        merged_sha = integrate_run(args.run_id, config)
    except IntegrationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"integrate failed unexpectedly: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(f"integrated {args.run_id} at {merged_sha}\n")
    return 0


def _cleanup_command(args: argparse.Namespace, config: RouterConfig) -> int:
    """Remove one run's isolation artifacts or report stale candidates."""
    from .cleanup import CleanupError, CleanupRefused, find_stale_runs

    if args.stale:
        if args.run_id or args.discard:
            print("--stale cannot be combined with RUN_ID or --discard", file=sys.stderr)
            return 2
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.stale_hours)
        report = find_stale_runs(config, cutoff=cutoff)

        def entry_line(entry, tag: str) -> str:
            return f"[{tag}] {entry.run_id} {entry.detail}"

        lines = [entry_line(e, "auto") for e in report.auto_candidates]
        lines += [entry_line(e, "report") for e in report.report_only]
        if args.json:
            sys.stdout.write(
                json.dumps(
                    {
                        "auto": [vars_like(e) for e in report.auto_candidates],
                        "report": [vars_like(e) for e in report.report_only],
                    },
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
                + "\n"
            )
            return 0
        sys.stdout.write("\n".join(lines) + ("\n" if lines else "") or "(no stale runs)\n")
        if not lines:
            sys.stdout.write("(no stale runs)\n")
        sys.stdout.write(
            f"summary: {len(report.auto_candidates)} auto-cleanable, "
            f"{len(report.report_only)} need decisions\n"
        )
        return 0

    if not args.run_id:
        print("cleanup requires RUN_ID or --stale", file=sys.stderr)
        return 2
    try:
        validate_run_id(args.run_id)
    except ValueError as exc:
        print(f"invalid run id: {exc}", file=sys.stderr)
        return 2

    try:
        from .cleanup import cleanup_run

        outcome = cleanup_run(args.run_id, config, discard=args.discard)
    except CleanupRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except CleanupError as exc:
        print(f"cleanup failed: {exc}", file=sys.stderr)
        return 2

    bits = []
    if outcome.already_cleaned:
        bits.append("already cleaned")
    else:
        if outcome.removed_worktree:
            bits.append("worktree removed")
        if outcome.removed_branch:
            bits.append("branch removed")
    if outcome.discarded:
        bits.append("discarded")
    for note in outcome.notes:
        bits.append(note)
    sys.stdout.write(f"cleanup {args.run_id}: {'; '.join(bits)}\n")
    return 0


def vars_like(entry) -> dict:
    return {"run_id": entry.run_id, "detail": entry.detail}


#: Command handlers; all five V1.2 commands are implemented.
_COMMAND_HANDLERS: dict[str, object] = {
    "doctor": _doctor_command,
    "list": _list_command,
    "show": _show_command,
    "integrate": _integrate_command,
    "cleanup": _cleanup_command,
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

    doctor = subparsers.add_parser("doctor", help="diagnose the local router environment")
    doctor.add_argument(
        "--repo",
        help="also run repository checks against this path",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of text",
    )
    list_parser = subparsers.add_parser("list", help="list recorded worker runs")
    list_parser.add_argument("--repo", help="only runs for this repository path")
    list_parser.add_argument("--status", help="only runs with this final status")
    list_parser.add_argument("--limit", type=int, help="show at most N runs")
    list_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    show = subparsers.add_parser("show", help="show one recorded run")
    show.add_argument("run_id")
    show.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    integrate = subparsers.add_parser("integrate", help="integrate a reviewed run")
    integrate.add_argument("run_id")
    cleanup = subparsers.add_parser(
        "cleanup",
        help="remove one run's worktree, or list stale runs with --stale",
    )
    cleanup.add_argument("run_id", nargs="?", help="run to clean up")
    cleanup.add_argument(
        "--discard",
        action="store_true",
        help="explicitly abandon an unintegrated worker change",
    )
    cleanup.add_argument(
        "--stale", action="store_true", help="list stale runs instead of cleaning"
    )
    cleanup.add_argument(
        "--stale-hours",
        type=int,
        default=168,
        help="staleness threshold in hours (default: 168)",
    )
    cleanup.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON (--stale only)"
    )

    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())

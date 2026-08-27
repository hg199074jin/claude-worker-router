"""Router environment diagnostics.

``run_doctor`` answers one question before a worker run starts:

    is this machine READY?

It never prints credentials; provider output is limited to the endpoint
host, model name, and fingerprint already produced by the redacted
provider snapshot.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import RouterConfig
from .provider import ProviderConfigError, fingerprint_provider, read_provider_snapshot


@dataclass(frozen=True)
class DoctorCheck:
    """One named probe with an ``ok``/``warning``/``error`` verdict."""

    name: str
    status: str
    detail: str


def overall_status(checks: list[DoctorCheck]) -> str:
    """Reduce per-check verdicts: any error wins, then any warning."""
    statuses = {check.status for check in checks}
    if "error" in statuses:
        return "error"
    if "warning" in statuses:
        return "warning"
    return "ok"


def run_doctor(
    config: RouterConfig, repository: Path | None = None
) -> list[DoctorCheck]:
    """Probe runtime, provider, storage, and optional repository health."""
    checks: list[DoctorCheck] = [
        _check_python_runtime(),
        _check_config(config),
        _check_git(),
        _check_claude_command(config),
        *_check_provider_settings(config),
        _check_run_records_storage(config),
        *_check_test_binaries(config),
    ]
    if repository is not None:
        checks.extend(_check_repository(Path(repository)))
    return checks


def _check_python_runtime() -> DoctorCheck:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 12):
        return DoctorCheck("python-runtime", "ok", f"Python {version}")
    return DoctorCheck("python-runtime", "error", f"Python 3.12+ required, found {version}")


def _check_config(config: RouterConfig) -> DoctorCheck:
    detail = (
        f"command={config.command} provider={config.provider} "
        f"max_turns={config.max_turns} timeout_seconds={config.timeout_seconds}"
    )
    return DoctorCheck("router-config", "ok", detail)


def _check_git() -> DoctorCheck:
    git_path = shutil.which("git")
    if git_path is None:
        return DoctorCheck("git-executable", "error", "git not found on PATH")
    try:
        proc = subprocess.run(
            [git_path, "--version"],
            shell=False,
            check=True,
            text=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        return DoctorCheck("git-executable", "error", str(exc))
    first_line = proc.stdout.strip().splitlines()[0] if proc.stdout else ""
    return DoctorCheck("git-executable", "ok", first_line or git_path)


def _check_claude_command(config: RouterConfig) -> DoctorCheck:
    from .executor import _resolve_worker_command

    resolved = _resolve_worker_command(config.command)
    if resolved is None:
        return DoctorCheck(
            "claude-executable",
            "error",
            f"worker command must be a bare executable name or absolute path "
            f"and must be executable: {config.command}",
        )
    return DoctorCheck("claude-executable", "ok", resolved)


def _check_provider_settings(config: RouterConfig) -> list[DoctorCheck]:
    try:
        snapshot = read_provider_snapshot(config.claude_settings)
        fingerprint = fingerprint_provider(snapshot)
    except ProviderConfigError as exc:
        return [
            DoctorCheck("claude-settings", "error", str(exc)),
            DoctorCheck("provider-routing", "error", "unavailable"),
        ]
    routing_detail = (
        f"endpoint={snapshot.endpoint_host} model={snapshot.model} "
        f"fingerprint={fingerprint[:16]}..."
    )
    return [
        DoctorCheck("claude-settings", "ok", str(config.claude_settings)),
        DoctorCheck("provider-routing", "ok", routing_detail),
    ]


def _check_run_records_storage(config: RouterConfig) -> DoctorCheck:
    records = config.run_records
    try:
        records.mkdir(parents=True, exist_ok=True)
        probe = records / ".doctor-write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return DoctorCheck("run-records-storage", "error", str(exc))
    return DoctorCheck("run-records-storage", "ok", str(records))


def _check_test_binaries(config: RouterConfig) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    for binary in config.allowed_test_binaries:
        found = shutil.which(binary)
        if found is None:
            checks.append(
                DoctorCheck(
                    f"test-binary:{binary}",
                    "warning",
                    "not found on PATH; runs using it will escalate",
                )
            )
        else:
            checks.append(DoctorCheck(f"test-binary:{binary}", "ok", found))
    return checks


def _check_repository(repository: Path) -> list[DoctorCheck]:
    repo_root = _git_output(repository, ["rev-parse", "--show-toplevel"])
    if repo_root is None:
        return [
            DoctorCheck(
                "repository",
                "error",
                f"{repository} is not a Git repository",
            )
        ]
    root = Path(repo_root).resolve()
    checks = [DoctorCheck("repository", "ok", str(root))]

    branch = _git_output(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    head = _git_output(root, ["rev-parse", "HEAD"])
    checks.append(
        DoctorCheck(
            "repository-branch",
            "ok" if branch else "warning",
            branch or "unknown (no commits?)",
        )
    )
    checks.append(
        DoctorCheck(
            "repository-head",
            "ok" if head else "warning",
            head or "unknown (no commits?)",
        )
    )

    status_out = _git_output(root, ["status", "--porcelain"])
    if status_out is None:
        checks.append(DoctorCheck("repository-clean", "error", "git status failed"))
    elif status_out.strip():
        entries = len(status_out.strip().splitlines())
        checks.append(
            DoctorCheck(
                "repository-clean",
                "warning",
                f"{entries} uncommitted entr{'y' if entries == 1 else 'ies'}; "
                "edit-mode delegation requires a clean checkout",
            )
        )
    else:
        checks.append(DoctorCheck("repository-clean", "ok", "clean"))

    worktree_listing = _git_worktree_list(root)
    if worktree_listing is None:
        checks.append(
            DoctorCheck("worktree-support", "error", "git worktree unavailable")
        )
    else:
        checks.append(DoctorCheck("worktree-support", "ok", worktree_listing))

    checks.append(_check_stale_worktrees(root))
    return checks


def _check_stale_worktrees(repo_root: Path) -> DoctorCheck:
    router_dir = repo_root.parent / ".codex-worktrees" / repo_root.name
    if not router_dir.is_dir():
        return DoctorCheck("stale-worktrees", "ok", "none")
    remnants = sorted(entry.name for entry in router_dir.iterdir() if entry.is_dir())
    if not remnants:
        return DoctorCheck("stale-worktrees", "ok", "none")
    detail = (
        f"{len(remnants)} prior router worktree(s): {', '.join(remnants[:5])}"
        + (" ..." if len(remnants) > 5 else "")
    )
    return DoctorCheck(
        "stale-worktrees",
        "warning",
        detail + "; review with a future cleanup command",
    )


def _git_output(cwd: Path, args: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            shell=False,
            check=True,
            text=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return proc.stdout.strip()


def _git_worktree_list(cwd: Path) -> str | None:
    listing = _git_output(cwd, ["worktree", "list"])
    if listing is None:
        return None
    lines = listing.splitlines()
    return f"{len(lines)} worktree(s) registered"


def render_json(checks: list[DoctorCheck], overall: str) -> str:
    """Machine-readable doctor payload used by the ``--json`` CLI flag."""
    return json.dumps(
        {
            "status": overall,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail}
                for c in checks
            ],
        },
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )

import tomllib
from pathlib import Path

from .models import RouterConfig


_PROVIDER_MODES = {"cc-switch-current"}


def load_config(path: Path) -> RouterConfig:
    """Load a credential-free router configuration and validate its bounds."""
    with Path(path).open("rb") as fh:
        data = tomllib.load(fh)

    worker = data.get("worker")
    if not isinstance(worker, dict):
        raise ValueError("config must define a [worker] table")

    required = (
        "command",
        "provider",
        "max_turns",
        "timeout_seconds",
        "correction_limit",
        "max_changed_files",
        "max_diff_lines",
        "allowed_test_binaries",
    )
    missing = [name for name in required if name not in worker]
    if missing:
        raise ValueError(f"missing worker keys: {', '.join(missing)}")

    command = worker["command"]
    if not isinstance(command, str) or not command:
        raise ValueError("worker.command must be a non-empty string")

    provider = worker["provider"]
    if not isinstance(provider, str) or provider not in _PROVIDER_MODES:
        raise ValueError(f"worker.provider must be one of {sorted(_PROVIDER_MODES)}")

    max_turns = _positive_int(worker["max_turns"], "worker.max_turns")
    timeout_seconds = _positive_int(worker["timeout_seconds"], "worker.timeout_seconds")
    correction_limit = _non_negative_int(worker["correction_limit"], "worker.correction_limit")
    max_changed_files = _positive_int(worker["max_changed_files"], "worker.max_changed_files")
    max_diff_lines = _positive_int(worker["max_diff_lines"], "worker.max_diff_lines")

    allowed = worker["allowed_test_binaries"]
    if not isinstance(allowed, list) or not all(isinstance(item, str) and item for item in allowed):
        raise ValueError("worker.allowed_test_binaries must be a list of non-empty strings")
    allowed_test_binaries = tuple(allowed)

    run_records = Path(data["run_records"]) if "run_records" in data else Path.home() / ".codex/model-router/runs"
    test_output_limit_bytes = (
        _positive_int(data["test_output_limit_bytes"], "test_output_limit_bytes")
        if "test_output_limit_bytes" in data
        else 65536
    )
    claude_settings = (
        Path(data["claude_settings"])
        if "claude_settings" in data
        else Path.home() / ".claude/settings.json"
    )

    return RouterConfig(
        command=command,
        provider=provider,
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
        correction_limit=correction_limit,
        max_changed_files=max_changed_files,
        max_diff_lines=max_diff_lines,
        allowed_test_binaries=allowed_test_binaries,
        run_records=run_records,
        test_output_limit_bytes=test_output_limit_bytes,
        claude_settings=claude_settings,
    )


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value
import tomllib
from pathlib import Path

from .models import RouterConfig


_PROVIDER_MODES = {"cc-switch-current"}


DEFAULT_CONFIG_PATH = Path.home() / ".codex/model-router/config.toml"


def default_config_path() -> Path:
    """Return the canonical default router configuration path under the user's home."""
    return DEFAULT_CONFIG_PATH


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
    if "/" in command and not Path(command).is_absolute():
        raise ValueError(
            "worker.command must be a bare executable name or an absolute path"
        )

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

    # ``config.example.toml`` historically places the optional keys after the
    # ``[worker]`` header, so accept both scopes; explicit worker values win.
    def _optional(name: str) -> object:
        if name in worker:
            return worker[name]
        return data.get(name)

    raw_run_records = _optional("run_records")
    if isinstance(raw_run_records, str) and raw_run_records:
        run_records = Path(raw_run_records)
    elif "run_records" not in worker and "run_records" not in data:
        run_records = Path.home() / ".codex/model-router/runs"
    else:
        raise ValueError("run_records must be a non-empty path string")

    raw_output_limit = _optional("test_output_limit_bytes")
    if raw_output_limit is None:
        test_output_limit_bytes = 65536
    else:
        test_output_limit_bytes = _positive_int(
            raw_output_limit, "test_output_limit_bytes"
        )

    raw_claude_settings = _optional("claude_settings")
    if isinstance(raw_claude_settings, str) and raw_claude_settings:
        claude_settings = Path(raw_claude_settings)
    elif "claude_settings" not in worker and "claude_settings" not in data:
        claude_settings = Path.home() / ".claude/settings.json"
    else:
        raise ValueError("claude_settings must be a non-empty path string")

    raw_binary_policy = _optional("binary_edit_policy")
    if raw_binary_policy is None or raw_binary_policy == "deny":
        binary_edit_policy = "deny"
    else:
        raise ValueError(
            f"binary_edit_policy must be \"deny\" (got {raw_binary_policy!r})"
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
        binary_edit_policy=binary_edit_policy,
    )


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value

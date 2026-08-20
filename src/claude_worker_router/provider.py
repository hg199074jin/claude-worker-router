"""Read Claude settings metadata without exposing credentials."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ProviderConfigError(ValueError):
    """Raised when provider settings cannot be parsed safely."""


@dataclass(frozen=True)
class ProviderSnapshot:
    endpoint_host: str
    model: str
    default_models: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "endpoint_host": self.endpoint_host,
            "model": self.model,
            "default_models": dict(self.default_models),
        }


def read_provider_snapshot(path: Path) -> ProviderSnapshot:
    """Read only endpoint and model routing fields from Claude settings."""
    settings_path = Path(path)
    try:
        raw = settings_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProviderConfigError(f"settings file not found: {settings_path}") from exc
    except OSError as exc:
        raise ProviderConfigError(f"settings file unreadable: {settings_path}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderConfigError(f"settings file is not valid JSON: {settings_path}") from exc

    if not isinstance(data, dict):
        raise ProviderConfigError("settings root must be a JSON object")

    env = data.get("env")
    if not isinstance(env, dict):
        raise ProviderConfigError("settings must contain an env object")

    endpoint_raw = env.get("ANTHROPIC_BASE_URL")
    if not isinstance(endpoint_raw, str) or not endpoint_raw:
        raise ProviderConfigError("settings.env.ANTHROPIC_BASE_URL is required")

    parsed = urlparse(endpoint_raw)
    endpoint_host = parsed.hostname or ""
    if not endpoint_host:
        raise ProviderConfigError("settings.env.ANTHROPIC_BASE_URL has no host")

    model_raw = env.get("ANTHROPIC_MODEL")
    if not isinstance(model_raw, str) or not model_raw:
        raise ProviderConfigError("settings.env.ANTHROPIC_MODEL is required")

    default_models = _collect_default_models(env)

    return ProviderSnapshot(
        endpoint_host=endpoint_host,
        model=model_raw,
        default_models=tuple(sorted(default_models)),
    )


def fingerprint_provider(snapshot: ProviderSnapshot) -> str:
    """Hash non-secret provider routing metadata for before/after comparison."""
    payload = json.dumps(snapshot.as_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _collect_default_models(env: dict[str, object]) -> list[tuple[str, str]]:
    defaults: list[tuple[str, str]] = []
    for key, value in env.items():
        if not isinstance(key, str) or not key.startswith("ANTHROPIC_DEFAULT_"):
            continue
        if not isinstance(value, str) or not value:
            continue
        defaults.append((key, value))
    return defaults
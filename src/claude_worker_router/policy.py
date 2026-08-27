"""Global and project policy handling (V1.3).

The policy layer answers one question before every run:

    what is the *tightest* rule set this task must obey?

Hierarchy: built-in invariants > global policy > project policy. Numbers
shrink (``min``), deny lists grow (union), boolean safety requirements
turn on (``true wins``). A project file that tries to relax any of those
axes is refused with :class:`PolicyRelaxationRejected` — never silently
clamped.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class PolicyRelaxationRejected(ValueError):
    """Raised when a lower layer tries to loosen a higher layer's rules."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"policy-relaxation-rejected: {detail}")
        self.reason = "policy-relaxation-rejected"
        self.detail = detail


def _normalize_path(entry: str) -> str:
    from pathlib import PurePosixPath

    if not isinstance(entry, str):
        raise ValueError("policy paths must be strings")
    parts = entry.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(
            f"policy path entries must be relative without traversal: {entry!r}"
        )
    pure = PurePosixPath(entry)
    if pure.is_absolute() or not pure.parts:
        raise ValueError(f"policy path entries must be relative: {entry!r}")
    return str(pure)


#: Invariant layer the worker may never touch, regardless of any file.
BUILTIN_DENY_PATHS: tuple[str, ...] = (".git", ".claude-worker-router")


@dataclass(frozen=True)
class RouterPolicy:
    """Immutable policy slice; every layer and the effective result share it."""

    max_turns: int
    timeout_seconds: int
    max_changed_files: int
    max_diff_lines: int
    deny_paths: tuple[str, ...] = ()
    sandbox_required: bool = False

    def __post_init__(self) -> None:
        for numeric in (
            "max_turns",
            "timeout_seconds",
            "max_changed_files",
            "max_diff_lines",
        ):
            value = getattr(self, numeric)
            # timeout_seconds may be fractional (tests use sub-second caps);
            # max_turns/files stay integral in practice but the invariant is
            # uniform: a strictly positive real quantity.
            is_number = isinstance(value, (int, float)) and not isinstance(
                value, bool
            )
            if not is_number or value <= 0:
                raise ValueError(f"policy {numeric} must be > 0 (got {value!r})")
        object.__setattr__(
            self,
            "deny_paths",
            tuple(sorted({_normalize_path(p) for p in self.deny_paths})),
        )
        if not isinstance(self.sandbox_required, bool):
            raise ValueError("sandbox_required must be a boolean")


@dataclass(frozen=True)
class EffectivePolicy(RouterPolicy):
    """Result of merging every active layer; values never exceed a parent."""


def _check_relaxation(global_policy: RouterPolicy, project: RouterPolicy) -> None:
    for numeric in (
        "max_turns",
        "timeout_seconds",
        "max_changed_files",
        "max_diff_lines",
    ):
        g_value = getattr(global_policy, numeric)
        p_value = getattr(project, numeric)
        if p_value > g_value:
            raise PolicyRelaxationRejected(
                f"project {numeric}={p_value} exceeds global {g_value}"
            )


def merge_policy(
    global_policy: RouterPolicy,
    project_policy: RouterPolicy | None,
) -> EffectivePolicy:
    """Fold project into global under tighten-only semantics."""
    if project_policy is None:
        return EffectivePolicy(
            max_turns=global_policy.max_turns,
            timeout_seconds=global_policy.timeout_seconds,
            max_changed_files=global_policy.max_changed_files,
            max_diff_lines=global_policy.max_diff_lines,
            deny_paths=global_policy.deny_paths,
            sandbox_required=global_policy.sandbox_required,
        )
    _check_relaxation(global_policy, project_policy)
    combined = sorted(set(global_policy.deny_paths) | set(project_policy.deny_paths))
    return EffectivePolicy(
        max_turns=min(global_policy.max_turns, project_policy.max_turns),
        timeout_seconds=min(global_policy.timeout_seconds, project_policy.timeout_seconds),
        max_changed_files=min(
            global_policy.max_changed_files, project_policy.max_changed_files
        ),
        max_diff_lines=min(global_policy.max_diff_lines, project_policy.max_diff_lines),
        deny_paths=tuple(combined),
        sandbox_required=(
            global_policy.sandbox_required or project_policy.sandbox_required
        ),
    )


# --------------------------------------------------------------------------
# Task 12: loaders and the effective-policy fold

import tomllib
from dataclasses import dataclass
from pathlib import Path


def default_global_policy_path() -> Path:
    """Canonical global policy location under the user's home."""
    return Path.home() / ".codex/model-router/policy.toml"


PROJECT_POLICY_RELATIVE = Path(".claude-worker-router") / "policy.toml"


def load_policy_file(
    path: Path, defaults: RouterPolicy | None = None
) -> RouterPolicy:
    """Parse one policy TOML strictly; unknown keys are hard errors."""
    path = Path(path)
    raw = path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))

    known_tables = {"limits", "paths"}
    for key in data:
        if key not in known_tables and key != "sandbox_required":
            raise ValueError(f"unknown policy key: {key!r} in {path}")

    limits = data.get("limits", {})
    if not isinstance(limits, dict):
        raise ValueError(f"[limits] must be a table in {path}")
    numeric_defaults = defaults or RouterPolicy(
        max_turns=1,
        timeout_seconds=1,
        max_changed_files=1,
        max_diff_lines=1,
    )
    numerics = {}
    for name in ("max_turns", "timeout_seconds", "max_changed_files", "max_diff_lines"):
        if name in limits:
            numerics[name] = limits[name]
        else:
            numerics[name] = getattr(numeric_defaults, name)
    for key in limits:
        if key not in numerics:
            raise ValueError(f"unknown [limits] key: {key!r} in {path}")

    paths_table = data.get("paths", {})
    if not isinstance(paths_table, dict):
        raise ValueError(f"[paths] must be a table in {path}")
    deny = paths_table.get("deny", ())
    if "deny" in paths_table:
        if not isinstance(deny, list) or not all(isinstance(d, str) for d in deny):
            raise ValueError(f"paths.deny must be a list of strings in {path}")
    for key in paths_table:
        if key != "deny":
            raise ValueError(f"unknown [paths] key: {key!r} in {path}")

    sandbox_required = data.get("sandbox_required", False)
    if not isinstance(sandbox_required, bool):
        raise ValueError(f"sandbox_required must be a boolean in {path}")

    return RouterPolicy(
        **numerics,
        deny_paths=tuple(deny),
        sandbox_required=sandbox_required,
    )


@dataclass(frozen=True)
class ResolvedPolicies:
    base_policy: RouterPolicy
    global_policy: RouterPolicy | None
    project_policy: RouterPolicy | None
    global_path: Path | None
    project_path: Path | None
    effective: EffectivePolicy


def resolve_effective_policy(
    base_policy: RouterPolicy,
    global_path: Path | None,
    repository: Path | None,
) -> ResolvedPolicies:
    """Fold config-floor ← global file ← project file; tighten-only each step.

    Missing files are simply absent layers. A project that tries to relax
    the *already resolved* global layer raises immediately.
    """
    effective = merge_policy(base_policy, None)
    loaded_global = None
    loaded_project = None
    used_global_path = None
    used_project_path = None

    if global_path is not None and Path(global_path).is_file():
        loaded_global = load_policy_file(global_path, defaults=base_policy)
        effective = merge_policy(effective, loaded_global)
        used_global_path = Path(global_path)

    if repository is not None:
        project_path = Path(repository) / PROJECT_POLICY_RELATIVE
        if project_path.is_file():
            floor = EffectivePolicy(
                max_turns=effective.max_turns,
                timeout_seconds=effective.timeout_seconds,
                max_changed_files=effective.max_changed_files,
                max_diff_lines=effective.max_diff_lines,
                deny_paths=effective.deny_paths,
                sandbox_required=effective.sandbox_required,
            )
            loaded_project = load_policy_file(project_path, defaults=floor)
            effective = merge_policy(effective, loaded_project)
            used_project_path = project_path

    return ResolvedPolicies(
        base_policy=base_policy,
        global_policy=loaded_global,
        project_policy=loaded_project,
        global_path=used_global_path,
        project_path=used_project_path,
        effective=effective,
    )

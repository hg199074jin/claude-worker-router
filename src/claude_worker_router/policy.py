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
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"policy {numeric} must be a positive integer")
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

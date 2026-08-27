"""Experimental macOS seatbelt (sandbox-exec) feasibility module.

Status for V1.3: **NOT READY** — see
``docs/superpowers/research/2026-08-27-macos-sandbox-feasibility.md``.

``sandbox-exec`` still exists on current macOS and a trivial profile runs,
but its profile language is undocumented, Apple marks it deprecated, and
the required invariants (provider network reachable, Claude configuration
readable, worktree writable, ``~/.ssh``/``~/.codex``/credentials denied,
no token materialization into temp files) are not provable without deep,
provider-version-coupled iteration. Until that proof exists this module
refuses to bless enforcement: ``is_sandbox_enforced()`` returns False and
runs with ``sandbox_required = true`` fail closed.
"""

from __future__ import annotations

import shutil

EXPERIMENTAL_VERDICT = "NOT READY"
SANDBOX_UNAVAILABLE = "sandbox-unavailable"


def is_sandbox_available() -> bool:
    """Raw host capability: does /usr/bin/sandbox-exec exist?

    Capability alone never enables enforcement; see the module verdict.
    """
    return shutil.which("sandbox-exec") is not None


def is_sandbox_enforced() -> bool:
    """Whether the router will actually wrap workers in a sandbox.

    Fixed to False for V1.3 by the spike verdict. Flipping this to any
    dynamic logic requires the research doc's validation checklist to be
    green on a supported macOS version first.
    """
    return False


class SandboxUnavailable(RuntimeError):
    """Raised internally when required-but-unavailable enforcement bites."""

    def __init__(self) -> None:
        super().__init__(
            "sandbox_required policy is set but macOS sandbox enforcement "
            "is NOT READY in this router version; refusing to run unsandboxed"
        )
        self.reason = SANDBOX_UNAVAILABLE

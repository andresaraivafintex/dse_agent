"""WS-C: per-task ephemeral sandbox.

This package must never raise just from being imported (the WS-B worker
imports `sandbox_runtime.activities` defensively with try/except
ImportError — see services/orchestrator/worker.py). Heavy dependencies
(docker SDK, temporalio, opentelemetry) are imported normally at module top
level because they are declared dependencies of this package itself
(pyproject.toml) — if they are not installed in the importer's venv, that is
the integrator's responsibility (each workstream has its own venv), not
something this package should silently paper over.

What this package actively guarantees so it never breaks a third-party import:
  - No module-level code opens a network connection, connects to the Docker
    daemon, or connects to Postgres at import time. All I/O happens inside
    functions/methods, invoked explicitly.
"""

from .driver import (
    DEFAULT_SANDBOX_DRIVER,
    DockerSandboxDriver,
    SandboxDriver,
    StageExecutionRequest,
    StageExecutionResult,
)
from .runtime_profile import (
    RuntimeProfile,
    RuntimeProfileViolation,
    validate_runtime_startup,
)
from .substrate import AgentSubstrate, FakeSubstrate, TurnLog

__all__ = [
    "AgentSubstrate",
    "DEFAULT_SANDBOX_DRIVER",
    "DockerSandboxDriver",
    "FakeSubstrate",
    "RuntimeProfile",
    "RuntimeProfileViolation",
    "SandboxDriver",
    "StageExecutionRequest",
    "StageExecutionResult",
    "TurnLog",
    "validate_runtime_startup",
]

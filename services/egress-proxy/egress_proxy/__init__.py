"""WS-C: default-deny egress proxy + ephemeral credential injection (WSC-E2).
No module-level code does I/O — importing is always safe."""
from .allowlist import Allowlist, AllowlistEntry
from .proxy import EgressProxy

__all__ = ["Allowlist", "AllowlistEntry", "EgressProxy"]

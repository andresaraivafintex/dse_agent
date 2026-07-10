"""WS-C: egress proxy default-deny + injeção de credenciais efêmeras
(WSC-E2). Nenhum código de nível de módulo faz I/O — import é sempre seguro."""
from .allowlist import Allowlist, AllowlistEntry
from .proxy import EgressProxy

__all__ = ["Allowlist", "AllowlistEntry", "EgressProxy"]

"""WS-C: sandbox efêmero por tarefa.

Este pacote nunca deve levantar exceção só de ser importado (o worker do
WS-B importa `sandbox_runtime.activities` defensivamente com
try/except ImportError — ver services/orchestrator/worker.py). Todos os
imports de dependências pesadas (docker SDK, temporalio, opentelemetry) são
feitos normalmente no topo dos módulos porque são dependências declaradas do
próprio pacote (pyproject.toml) — se não estiverem instaladas no venv de quem
importa, isso é responsabilidade do integrador (cada workstream tem seu
próprio venv), não algo que este pacote deve esconder silenciosamente.

O que este pacote garante ativamente para não quebrar o import de terceiros:
  - Nenhum código de nível de módulo abre conexão de rede, conecta no Docker
    daemon, ou conecta no Postgres no momento do import. Toda I/O acontece
    dentro de funções/métodos, chamada explicitamente.
"""

from .substrate import AgentSubstrate, FakeSubstrate, TurnLog

__all__ = ["AgentSubstrate", "FakeSubstrate", "TurnLog"]

# WS-B — orchestrator worker. Container usa python:3.11-slim per
# CONVENTIONS.md (independente da versao do host usada em dev/test).
FROM python:3.11-slim

# S4 (Fase 5): o worker (control plane) clona o repo alvo via subprocess `git`
# antes de montar o workspace no sandbox — precisa do git no PATH.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia so o que este servico precisa para instalar (contratos compartilhados
# + o proprio pacote). Assume build context = raiz do monorepo (ver
# docker-compose.wsb.yml: build.context: ../.. , build.dockerfile).
COPY packages/contracts /app/packages/contracts
COPY packages/dse_audit /app/packages/dse_audit
COPY packages/dse_identity /app/packages/dse_identity
COPY services/orchestrator /app/services/orchestrator

# Activities cross-workstream (WS-C, WS-E) registradas no worker unico desta
# Fase 1 (ver worker.py:_load_cross_workstream_activities) — precisam estar
# instaladas neste mesmo venv para o import defensivo achar `ACTIVITIES`.
COPY services/sandbox-runtime /app/services/sandbox-runtime
COPY services/validation /app/services/validation

RUN pip install --no-cache-dir \
        -e /app/packages/contracts \
        -e /app/packages/dse_audit \
        -e /app/packages/dse_identity \
        -e /app/services/sandbox-runtime \
        -e /app/services/validation \
        -e /app/services/orchestrator

ENV DSE_ORCHESTRATOR_HEALTH_PORT=8900
EXPOSE 8900

CMD ["python", "-m", "dse_orchestrator.worker"]

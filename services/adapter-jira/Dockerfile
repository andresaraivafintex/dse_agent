# WS-A Fase 2 — adapter-jira (porta 8804). Build a partir da RAIZ do monorepo:
#   docker build -f services/adapter-jira/Dockerfile -t dse/adapter-jira .
FROM python:3.11-slim

WORKDIR /app

COPY packages/contracts packages/contracts
COPY packages/dse_audit packages/dse_audit
COPY packages/dse_identity packages/dse_identity
COPY services/platform services/platform
COPY services/ingest-gateway services/ingest-gateway
COPY services/adapter-jira services/adapter-jira

RUN pip install --no-cache-dir \
        -e packages/contracts \
        -e packages/dse_audit \
        -e packages/dse_identity \
        -e services/platform \
        -e services/ingest-gateway \
        -e services/adapter-jira

WORKDIR /app/services/adapter-jira
EXPOSE 8804

CMD ["uvicorn", "adapter_jira.app:app", "--host", "0.0.0.0", "--port", "8804"]

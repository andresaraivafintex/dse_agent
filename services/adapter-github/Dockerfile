# WS-A — adapter-github (port 8802). Build from the monorepo ROOT:
#   docker build -f services/adapter-github/Dockerfile -t dse/adapter-github .
FROM python:3.11-slim

WORKDIR /app

COPY packages/contracts packages/contracts
COPY packages/dse_audit packages/dse_audit
COPY packages/dse_identity packages/dse_identity
COPY services/platform services/platform
COPY services/ingest-gateway services/ingest-gateway
COPY services/adapter-github services/adapter-github

RUN pip install --no-cache-dir \
        -e packages/contracts \
        -e packages/dse_audit \
        -e packages/dse_identity \
        -e services/platform \
        -e services/ingest-gateway \
        -e services/adapter-github

WORKDIR /app/services/adapter-github
EXPOSE 8802

CMD ["uvicorn", "adapter_github.app:app", "--host", "0.0.0.0", "--port", "8802"]

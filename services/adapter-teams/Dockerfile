# WS-A — adapter-teams (port 8808), PROVISIONAL. Build from the monorepo ROOT:
#   docker build -f services/adapter-teams/Dockerfile -t dse/adapter-teams .
FROM python:3.11-slim

WORKDIR /app

COPY packages/contracts packages/contracts
COPY packages/dse_audit packages/dse_audit
COPY packages/dse_identity packages/dse_identity
COPY services/platform services/platform
COPY services/ingest-gateway services/ingest-gateway
COPY services/adapter-teams services/adapter-teams

RUN pip install --no-cache-dir \
        -e packages/contracts \
        -e packages/dse_audit \
        -e packages/dse_identity \
        -e services/platform \
        -e services/ingest-gateway \
        -e services/adapter-teams

WORKDIR /app/services/adapter-teams
EXPOSE 8808

CMD ["uvicorn", "adapter_teams.app:app", "--host", "0.0.0.0", "--port", "8808"]

# WS-A — adapter-slack (port 8801). Build from the monorepo ROOT:
#   docker build -f services/adapter-slack/Dockerfile -t dse/adapter-slack .
FROM python:3.11-slim

WORKDIR /app

COPY packages/contracts packages/contracts
COPY packages/dse_audit packages/dse_audit
COPY packages/dse_identity packages/dse_identity
COPY services/platform services/platform
COPY services/ingest-gateway services/ingest-gateway
COPY services/adapter-slack services/adapter-slack

RUN pip install --no-cache-dir \
        -e packages/contracts \
        -e packages/dse_audit \
        -e packages/dse_identity \
        -e services/platform \
        -e services/ingest-gateway \
        -e services/adapter-slack

WORKDIR /app/services/adapter-slack
EXPOSE 8801

CMD ["uvicorn", "adapter_slack.app:app", "--host", "0.0.0.0", "--port", "8801"]

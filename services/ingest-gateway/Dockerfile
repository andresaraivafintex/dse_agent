# WS-A — ingest-gateway (port 8803). Build from the monorepo ROOT:
#   docker build -f services/ingest-gateway/Dockerfile -t dse/ingest-gateway .
FROM python:3.11-slim

WORKDIR /app

COPY packages/contracts packages/contracts
COPY packages/dse_audit packages/dse_audit
COPY packages/dse_identity packages/dse_identity
COPY services/ingest-gateway services/ingest-gateway

RUN pip install --no-cache-dir \
        -e packages/contracts \
        -e packages/dse_audit \
        -e packages/dse_identity \
        -e services/ingest-gateway

WORKDIR /app/services/ingest-gateway
EXPOSE 8803

# Two ways to run the image (see docker-compose.wsa.yml):
#   - FastAPI app (health + admin kill switch):  uvicorn ingest_gateway.app:app
#   - Dispatcher (drains ingest_events -> Temporal): python -m ingest_gateway.dispatcher_main
CMD ["uvicorn", "ingest_gateway.app:app", "--host", "0.0.0.0", "--port", "8803"]

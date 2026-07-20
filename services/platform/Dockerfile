# WS-F — imagem do queue board admin (WSF-E6-T3). Instala os pacotes da
# fundação + este serviço e roda o ASGI em uvicorn na porta 8890.
FROM python:3.12-slim

WORKDIR /app

# contexto de build é a raiz do monorepo (ver docker-compose.wsf.yml)
COPY packages/contracts /app/packages/contracts
COPY packages/dse_audit /app/packages/dse_audit
COPY packages/dse_identity /app/packages/dse_identity
COPY services/platform /app/services/platform

RUN pip install --no-cache-dir \
      -e packages/contracts \
      -e packages/dse_audit \
      -e packages/dse_identity \
      -e "services/platform[temporal]"

EXPOSE 8890
CMD ["uvicorn", "dse_platform.queue_board.asgi:app", "--host", "0.0.0.0", "--port", "8890"]

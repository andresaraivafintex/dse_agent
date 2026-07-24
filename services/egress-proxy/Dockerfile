# WS-C — egress-proxy (porta 8806). Build a partir da RAIZ do monorepo:
#   docker build -f services/egress-proxy/Dockerfile -t dse/egress-proxy .
# No compose (dev) roda de python:3.11-slim com o código montado; esta imagem
# é a versão publicável para o chart (K8s), com o código COPIADO (não montado)
# e usuário não-root (o chart exige runAsNonRoot + readOnlyRootFilesystem).
FROM python:3.11-slim

WORKDIR /app

COPY packages/contracts packages/contracts
COPY packages/dse_audit packages/dse_audit
COPY packages/dse_identity packages/dse_identity
COPY services/egress-proxy services/egress-proxy

RUN pip install --no-cache-dir \
        -e packages/contracts \
        -e packages/dse_audit \
        -e packages/dse_identity \
        -e services/egress-proxy

# não-root fixo (mesmo UID dos demais serviços do chart)
RUN groupadd -g 10001 dse && useradd -u 10001 -g 10001 -m -s /usr/sbin/nologin dse

WORKDIR /app/services/egress-proxy
ENV HOME=/tmp PYTHONUNBUFFERED=1 DSE_EGRESS_PORT=8806
EXPOSE 8806

USER 10001:10001
CMD ["python", "-m", "egress_proxy.server"]

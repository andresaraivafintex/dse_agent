# WS-C — egress-proxy (port 8806). Build from the monorepo ROOT:
#   docker build -f services/egress-proxy/Dockerfile -t dse/egress-proxy .
# Under compose (dev) it runs from python:3.11-slim with the code bind-mounted;
# this image is the publishable version for the chart (K8s), with the code
# COPIED (not mounted) and a non-root user (the chart requires runAsNonRoot +
# readOnlyRootFilesystem).
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

# fixed non-root user (same UID as the chart's other services)
RUN groupadd -g 10001 dse && useradd -u 10001 -g 10001 -m -s /usr/sbin/nologin dse

WORKDIR /app/services/egress-proxy
ENV HOME=/tmp PYTHONUNBUFFERED=1 DSE_EGRESS_PORT=8806
EXPOSE 8806

USER 10001:10001
CMD ["python", "-m", "egress_proxy.server"]

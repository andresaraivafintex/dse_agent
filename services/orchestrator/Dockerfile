# WS-B — orchestrator worker. Container usa python:3.11-slim per
# CONVENTIONS.md (independente da versao do host usada em dev/test).
FROM python:3.11-slim

# S4 (Fase 5): git para clonar o repo alvo no control plane.
# S5 (Fase 5): Node.js para o CLI do Claude Code que o claude-agent-sdk dispara
# (o Coder real). curl/ca-certificates para o NodeSource.
# Node 22 LTS (achado do disparo real 2026-07-22): repos-alvo usam
# `node --test "glob"` — o glob NATIVO do test runner só existe no Node 21+;
# no 20 o padrão quoted não expande e o runner não acha nenhum teste.
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Plano 08 §D — o trigger_preview roda NESTE worker:
#  - kubectl (pinado) fala com o cluster k3d via kubeconfig-internal (compose);
#  - docker CLI (pinado, só o binário estático) builda/pusha a imagem do PR
#    (D4) via o socket do host que o compose já monta (docker-outside-of-docker).
ARG KUBECTL_VERSION=v1.31.4
ARG DOCKER_CLI_VERSION=27.4.1
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in amd64) karch=amd64; darch=x86_64 ;; arm64) karch=arm64; darch=aarch64 ;; *) echo "arch $arch nao suportada" && exit 1 ;; esac \
    && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${karch}/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${darch}/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && kubectl version --client --output=yaml >/dev/null && docker --version

WORKDIR /app

# Copia so o que este servico precisa para instalar (contratos compartilhados
# + o proprio pacote). Assume build context = raiz do monorepo (ver
# docker-compose.wsb.yml: build.context: ../.. , build.dockerfile).
# scripts/migrate.py: usado pelo Job de migração do chart (Fase 5). Os .sql
# vivem em migrations/ e PRECISAM ir na imagem — migrate.py resolve
# MIGRATIONS_DIR = <script>/../../migrations = /app/migrations. Sem este COPY o
# hook "sucede" sem aplicar nada ("Nenhuma migração encontrada").
COPY scripts /app/scripts
COPY migrations /app/migrations
COPY packages/contracts /app/packages/contracts
COPY packages/dse_audit /app/packages/dse_audit
COPY packages/dse_identity /app/packages/dse_identity
COPY services/orchestrator /app/services/orchestrator

# Activities cross-workstream (WS-C, WS-E) registradas no worker unico desta
# Fase 1 (ver worker.py:_load_cross_workstream_activities) — precisam estar
# instaladas neste mesmo venv para o import defensivo achar `ACTIVITIES`.
COPY services/sandbox-runtime /app/services/sandbox-runtime
COPY services/validation /app/services/validation
# Planner real: o proposer chama o modelo via model_gateway_client (enforcement
# client-side + LEDGER de custo do stage=planner no caminho — WSD).
COPY services/model-gateway /app/services/model-gateway

RUN pip install --no-cache-dir \
        -e /app/packages/contracts \
        -e /app/packages/dse_audit \
        -e /app/packages/dse_identity \
        -e /app/services/sandbox-runtime \
        -e /app/services/validation \
        -e /app/services/model-gateway \
        -e /app/services/orchestrator

# S5 (Fase 5): só o Claude Agent SDK (o substrato do Coder real). NÃO uso o
# extra [substrates] porque ele arrasta openhands-sdk, que exige Python 3.12
# (esta imagem é 3.11). claude-agent-sdk roda em 3.11 e embute o CLI (Node).
RUN pip install --no-cache-dir "claude-agent-sdk>=0.2.124"

# Não-root (uid 10001): o chart exige runAsNonRoot no perfil estrito. Sob
# readOnlyRootFilesystem o app só escreve em /tmp (emptyDir) — HOME=/tmp cobre
# o cache de discovery do kubectl ($HOME/.kube/cache) e o state dir do sandbox
# (DSE_SANDBOX_STATE_DIR default /tmp/dse-sandboxes). No driver K8s não há
# clone host-side, então nada mais precisa de escrita.
RUN groupadd -g 10001 dse && useradd -u 10001 -g 10001 -m -s /usr/sbin/nologin dse

ENV DSE_ORCHESTRATOR_HEALTH_PORT=8900 HOME=/tmp PYTHONUNBUFFERED=1
EXPOSE 8900

USER 10001:10001
CMD ["python", "-m", "dse_orchestrator.worker"]

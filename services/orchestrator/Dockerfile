# WS-B — orchestrator worker. The container uses python:3.11-slim per
# CONVENTIONS.md (independent of the host version used in dev/test).
FROM python:3.11-slim

# S4 (Phase 5): git, to clone the target repo on the control plane.
# S5 (Phase 5): Node.js, for the Claude Code CLI that claude-agent-sdk spawns
# (the real Coder). curl/ca-certificates for NodeSource.
# Node 22 LTS (found in the real run of 2026-07-22): target repos use
# `node --test "glob"` — the test runner's NATIVE glob only exists on Node 21+;
# on 20 the quoted pattern does not expand and the runner finds no test at all.
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Plan 08 §D — trigger_preview runs in THIS worker:
#  - kubectl (pinned) talks to the k3d cluster via kubeconfig-internal (compose);
#  - the docker CLI (pinned, static binary only) builds/pushes the PR image
#    (D4) through the host socket compose already mounts (docker-outside-of-docker).
ARG KUBECTL_VERSION=v1.31.4
ARG DOCKER_CLI_VERSION=27.4.1
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in amd64) karch=amd64; darch=x86_64 ;; arm64) karch=arm64; darch=aarch64 ;; *) echo "arch $arch not supported" && exit 1 ;; esac \
    && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${karch}/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${darch}/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && kubectl version --client --output=yaml >/dev/null && docker --version

WORKDIR /app

# Copy only what this service needs in order to install (the shared contracts
# + its own package). Assumes build context = monorepo root (see
# docker-compose.wsb.yml: build.context: ../.. , build.dockerfile).
# scripts/migrate.py: used by the chart's migration Job (Phase 5). The .sql
# files live in migrations/ and MUST be in the image — migrate.py resolves
# MIGRATIONS_DIR = <script>/../../migrations = /app/migrations. Without this
# COPY the hook "succeeds" while applying nothing ("No migration found in ...").
COPY scripts /app/scripts
COPY migrations /app/migrations
COPY packages/contracts /app/packages/contracts
COPY packages/dse_audit /app/packages/dse_audit
COPY packages/dse_identity /app/packages/dse_identity
COPY services/orchestrator /app/services/orchestrator

# Cross-workstream activities (WS-C, WS-E) registered in the single worker of
# this Phase 1 (see worker.py:_load_cross_workstream_activities) — they have to
# be installed in this same venv for the defensive import to find `ACTIVITIES`.
COPY services/sandbox-runtime /app/services/sandbox-runtime
COPY services/validation /app/services/validation
# Real Planner: the proposer calls the model through model_gateway_client
# (client-side enforcement + the stage=planner cost LEDGER on the path — WSD).
COPY services/model-gateway /app/services/model-gateway

RUN pip install --no-cache-dir \
        -e /app/packages/contracts \
        -e /app/packages/dse_audit \
        -e /app/packages/dse_identity \
        -e /app/services/sandbox-runtime \
        -e /app/services/validation \
        -e /app/services/model-gateway \
        -e /app/services/orchestrator

# S5 (Phase 5): the Claude Agent SDK only (the real Coder's substrate). The
# [substrates] extra is NOT used because it drags in openhands-sdk, which
# requires Python 3.12 (this image is 3.11). claude-agent-sdk runs on 3.11 and
# embeds the CLI (Node).
RUN pip install --no-cache-dir "claude-agent-sdk>=0.2.124"

# Non-root (uid 10001): the chart requires runAsNonRoot in the strict profile.
# Under readOnlyRootFilesystem the app only writes to /tmp (emptyDir) — HOME=/tmp
# covers kubectl's discovery cache ($HOME/.kube/cache) and the sandbox state dir
# (DSE_SANDBOX_STATE_DIR, default /tmp/dse-sandboxes). With the K8s driver there
# is no host-side clone, so nothing else needs to write.
RUN groupadd -g 10001 dse && useradd -u 10001 -g 10001 -m -s /usr/sbin/nologin dse

ENV DSE_ORCHESTRATOR_HEALTH_PORT=8900 HOME=/tmp PYTHONUNBUFFERED=1
EXPOSE 8900

USER 10001:10001
CMD ["python", "-m", "dse_orchestrator.worker"]

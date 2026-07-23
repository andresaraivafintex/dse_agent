# Plano 08 §G — imagem do agent-runner (runtime isolado K8s).
#
# Base mínima, NÃO-root, sem toolchain de build supérfluo. Roda UM estágio do
# agente por invocação (python -m agent_runner --stage <stage>), lendo o payload
# do stdin e escrevendo o resultado no stdout (o KubernetesSandboxDriver faz
# `kubectl exec`). Nenhuma credencial de longo prazo na imagem — a virtual key e
# o token de git são injetados por invocação (env efêmero), nunca em camada.
#
# Postura: casa com o build_pod_manifest (runAsNonRoot=10001, readOnlyRootFs,
# cap drop ALL). O RuntimeClass de isolamento forte (gVisor/Kata) é aplicado no
# Pod, não na imagem.
FROM python:3.12-slim AS base

# usuário não-root fixo (mesmo UID do securityContext do Pod)
RUN groupadd -g 10001 dse && useradd -u 10001 -g 10001 -m -s /usr/sbin/nologin dse

WORKDIR /app

# deps do runtime do agente (substrato openhands + git). Pinar em produção.
# git é necessário para o ScopedGitSession (checkpoint/push do branch da tarefa).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY services/sandbox-runtime/agent-runner/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# contrato tipado da fronteira worker→runner (AgentTurnRequest/Result): o
# runner valida com o modelo REAL (spec §5), nunca com dict solto.
COPY packages/contracts /app/packages/contracts
RUN pip install --no-cache-dir /app/packages/contracts

# substrato openhands é OPT-IN (a imagem default fica enxuta p/ VPS 2vCPU):
# docker build --build-arg INSTALL_OPENHANDS=1 ...
ARG INSTALL_OPENHANDS=0
COPY services/sandbox-runtime/agent-runner/requirements-openhands.txt /app/requirements-openhands.txt
RUN if [ "$INSTALL_OPENHANDS" = "1" ]; then \
      pip install --no-cache-dir -r /app/requirements-openhands.txt; \
    fi

# código do runner (o módulo agent_runner + o sandbox_runtime compartilhado)
COPY services/sandbox-runtime/agent-runner/agent_runner /app/agent_runner
# fonte única de verdade do git escopado: vendora o módulo do worker na
# imagem (gitops.py importa com fallback para esta cópia)
COPY services/sandbox-runtime/sandbox_runtime/scoped_git.py /app/agent_runner/_scoped_git.py

USER 10001:10001
# /workspace e /tmp vêm de emptyDir graváveis (o rootfs é read-only no Pod)
# PYTHONPATH: o exec roda com working_dir=/workspace (o driver monta o repo
# lá) — sem isto `python -m agent_runner` não resolveria o módulo em /app.
ENV HOME=/tmp PYTHONUNBUFFERED=1 PYTHONPATH=/app
# O container fica VIVO e ocioso; cada turno chega por exec
# (`docker exec -i` / `kubectl exec -i` → python -m agent_runner). ENTRYPOINT
# de runner aqui seria bug: o processo leria stdin EOF no boot e sairia antes
# de qualquer exec (pod restartPolicy=Never morreria "Completed").
CMD ["sleep", "infinity"]

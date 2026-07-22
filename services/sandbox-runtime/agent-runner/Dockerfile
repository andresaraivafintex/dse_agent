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

# código do runner (o módulo agent_runner + o sandbox_runtime compartilhado)
COPY services/sandbox-runtime/agent-runner/agent_runner /app/agent_runner

USER 10001:10001
# /workspace e /tmp vêm de emptyDir graváveis (o rootfs é read-only no Pod)
ENV HOME=/tmp PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "agent_runner"]

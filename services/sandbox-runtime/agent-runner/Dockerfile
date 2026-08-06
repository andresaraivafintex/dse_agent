# Plan 08 §G — agent-runner image (isolated K8s runtime).
#
# Minimal base, NON-root, no superfluous build toolchain. Runs ONE agent stage
# per invocation (python -m agent_runner --stage <stage>), reading the payload
# from stdin and writing the result to stdout (KubernetesSandboxDriver does the
# `kubectl exec`). No long-lived credential in the image — the virtual key and
# the git token are injected per invocation (ephemeral env), never into a layer.
#
# Posture: matches build_pod_manifest (runAsNonRoot=10001, readOnlyRootFs,
# cap drop ALL). The strong-isolation RuntimeClass (gVisor/Kata) is applied on
# the Pod, not in the image.
FROM python:3.12-slim AS base

# fixed non-root user (same UID as the Pod's securityContext)
RUN groupadd -g 10001 dse && useradd -u 10001 -g 10001 -m -s /usr/sbin/nologin dse

WORKDIR /app

# agent runtime deps (openhands substrate + git). Pin these in production.
# git is required by ScopedGitSession (checkpoint/push of the task branch).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl gnupg \
    && rm -rf /var/lib/apt/lists/*

# node 20 + npm: the Tester runs the target repo's test suite INSIDE the Pod
# (e.g. `node --test` for Node repos, `npm test`). claude-agent-sdk embeds Node
# internally but does NOT expose it on the PATH — without this the Tester fails
# with "node: not found" on Node repos (observed empirically: fintex-wallet is
# Node). NodeSource for node 22 LTS (satisfies `engines.node >=20`). Node 22
# (not 20) because `node --test` only EXPANDS GLOBS from 21 on — repos with
# `"test": "node --test \"test/*.test.js\""` (e.g. fintex-wallet) fail on 20
# with "Could not find 'test/*.test.js'" (the glob is taken as a literal path).
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

# JDK 17: the customer's backend is Spring Boot 3.4 with <java.version>17</java.version>
# (pom.xml), and without a JDK in the Pod every L1 gate on that repo lands
# NOT_CONFIGURED — which SCORES AS FAILURE. The DSE would clone a Java repo,
# edit it, and be structurally unable to validate a line of it.
#
# Only the JDK, not Maven: the repo ships `mvnw` and
# `.mvn/wrapper/maven-wrapper.properties` pins Maven 3.9.9, so the wrapper
# fetches its own. Adding Maven to the image would just pick a fight with the
# version the repo already decided on.
#
# headless: no AWT, no fonts. Roughly half the size of the full JDK and nothing
# here draws.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jdk-headless \
    && rm -rf /var/lib/apt/lists/* \
    && java -version && javac -version

COPY services/sandbox-runtime/agent-runner/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# bandit: the L1 pipeline's SAST (dse_validation.l1.sast runs `bandit -r <dir>`
# INSIDE the Pod via kubectl exec). Without bandit, L1 reports sast=ERROR and
# fails the gate. It is a Python tool; in a repo with no .py it runs, finds
# nothing and returns PASS.
RUN pip install --no-cache-dir bandit

# typed contract for the worker→runner boundary (AgentTurnRequest/Result): the
# runner validates against the REAL model (spec §5), never against a loose dict.
COPY packages/contracts /app/packages/contracts
RUN pip install --no-cache-dir /app/packages/contracts

# the openhands substrate is OPT-IN (the default image stays lean for a 2vCPU VPS):
# docker build --build-arg INSTALL_OPENHANDS=1 ...
ARG INSTALL_OPENHANDS=0
COPY services/sandbox-runtime/agent-runner/requirements-openhands.txt /app/requirements-openhands.txt
RUN if [ "$INSTALL_OPENHANDS" = "1" ]; then \
      pip install --no-cache-dir -r /app/requirements-openhands.txt; \
    fi

# runner code (the agent_runner module + the shared sandbox_runtime)
COPY services/sandbox-runtime/agent-runner/agent_runner /app/agent_runner
# single source of truth for scoped git and post-turn hygiene: vendor the
# worker's modules into the image (gitops/postturn import with a fallback to
# this copy)
COPY services/sandbox-runtime/sandbox_runtime/scoped_git.py /app/agent_runner/_scoped_git.py
COPY services/sandbox-runtime/sandbox_runtime/workspace_hygiene.py /app/agent_runner/_workspace_hygiene.py

USER 10001:10001
# /workspace and /tmp come from writable emptyDirs (the Pod rootfs is read-only)
# PYTHONPATH: the exec runs with working_dir=/workspace (the driver mounts the
# repo there) — without this `python -m agent_runner` would not resolve the
# module under /app.
ENV HOME=/tmp PYTHONUNBUFFERED=1 PYTHONPATH=/app
# The container stays ALIVE and idle; each turn arrives through an exec
# (`docker exec -i` / `kubectl exec -i` → python -m agent_runner). A runner
# ENTRYPOINT here would be a bug: the process would read stdin EOF at boot and
# exit before any exec (a pod with restartPolicy=Never would die "Completed").
CMD ["sleep", "infinity"]

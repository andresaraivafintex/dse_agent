#!/usr/bin/env bash
# Bootstrap for the DSE pilot VPS (plan 09, Phase 5) — Ubuntu 22.04+/Debian 12,
# 2 vCPU / 8 GB. Idempotent: safe to re-run after a partial failure.
#
# What this script DOES: swap, k3s (pinned version), gVisor (runsc) + RuntimeClass,
# helm, sops+age. What it does NOT do (steps in the README.md runbook next to it):
# DNS/ingress TLS, managed Postgres DSN, SOPS secrets, helm install.
set -euo pipefail

K3S_VERSION="${K3S_VERSION:-v1.31.4+k3s1}"
GVISOR_URL="${GVISOR_URL:-https://storage.googleapis.com/gvisor/releases/release/latest/$(uname -m)}"
SWAP_GB="${SWAP_GB:-4}"

log() { printf '\n== %s\n' "$*"; }

# --- swap (peak buffer on the small VPS; plan 09 §Fase 5) --------------------
if ! swapon --show | grep -q '/swapfile'; then
  log "criando swap de ${SWAP_GB}G"
  fallocate -l "${SWAP_GB}G" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
else
  log "swap already present"
fi

# --- k3s single-node (traefik on — it is the pilot's ingress) ----------------
if ! command -v k3s >/dev/null 2>&1; then
  log "instalando k3s ${K3S_VERSION}"
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="${K3S_VERSION}" sh -s - \
    --write-kubeconfig-mode 644
else
  log "k3s already installed: $(k3s --version | head -1)"
fi

# --- gVisor (runsc) + RuntimeClass — the isolation that authorizes the pilot -
if ! command -v runsc >/dev/null 2>&1; then
  log "instalando gVisor (runsc)"
  (
    cd /tmp
    curl -sfLO "${GVISOR_URL}/runsc" -O "${GVISOR_URL}/runsc.sha512" \
      -O "${GVISOR_URL}/containerd-shim-runsc-v1" -O "${GVISOR_URL}/containerd-shim-runsc-v1.sha512"
    sha512sum -c runsc.sha512 -c containerd-shim-runsc-v1.sha512
    install -m 755 runsc containerd-shim-runsc-v1 /usr/local/bin/
  )
else
  log "runsc already installed"
fi

# k3s uses an embedded containerd: config template to register the runsc
# handler. IMPORTANT (finding from the real VPS, containerd v1.7.x-k3s2): the
# file is `config.toml.tmpl` (not config-v3) and the CRI plugin is
# `io.containerd.grpc.v1.cri` — the v3/cri.v1.runtime path does NOT register the
# handler (the Pod stays ContainerCreating with "no runtime for runsc is configured").
K3S_CONTAINERD_TMPL=/var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl
if ! grep -q 'runtimes.runsc' "$K3S_CONTAINERD_TMPL" 2>/dev/null; then
  log "registrando handler runsc no containerd do k3s"
  mkdir -p "$(dirname "$K3S_CONTAINERD_TMPL")"
  cat > "$K3S_CONTAINERD_TMPL" <<'EOF'
{{ template "base" . }}

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
EOF
  systemctl restart k3s
  sleep 15
else
  log "runsc handler already registered"
fi

log "aplicando RuntimeClass gvisor + namespace de sandboxes (PSA restricted)"
k3s kubectl apply -f "$(dirname "$0")/../k8s/sandbox-isolation.yaml"

# --- helm + sops + age --------------------------------------------------------
command -v helm >/dev/null 2>&1 || { log "instalando helm"; curl -sfL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash; }
# sops: the release asset carries the version in its name — "latest/download"
# with an unversioned name returns 404 (found during the real VPS bootstrap,
# 2026-07-23).
SOPS_VERSION="${SOPS_VERSION:-v3.11.0}"
command -v sops >/dev/null 2>&1 || { log "instalando sops ${SOPS_VERSION}"; curl -sfLo /usr/local/bin/sops "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.$( [ "$(uname -m)" = aarch64 ] && echo arm64 || echo amd64 )"; chmod +x /usr/local/bin/sops; }
command -v age >/dev/null 2>&1 || { log "instalando age"; apt-get update -qq && apt-get install -y -qq age; }

log "bootstrap complete. Next steps: deploy/vps/README.md"

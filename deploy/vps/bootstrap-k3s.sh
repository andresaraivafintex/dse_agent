#!/usr/bin/env bash
# Bootstrap da VPS do piloto DSE (plano 09, Fase 5) — Ubuntu 22.04+/Debian 12,
# 2 vCPU / 8 GB. Idempotente: pode rodar de novo após falha parcial.
#
# O que este script FAZ: swap, k3s (versão pinada), gVisor (runsc) + RuntimeClass,
# helm, sops+age. O que ele NÃO faz (passos do runbook README.md ao lado):
# DNS/ingress TLS, DSN do Postgres gerenciado, secrets SOPS, helm install.
set -euo pipefail

K3S_VERSION="${K3S_VERSION:-v1.31.4+k3s1}"
GVISOR_URL="${GVISOR_URL:-https://storage.googleapis.com/gvisor/releases/release/latest/$(uname -m)}"
SWAP_GB="${SWAP_GB:-4}"

log() { printf '\n== %s\n' "$*"; }

# --- swap (amortecedor de pico na VPS pequena; plano 09 §Fase 5) -------------
if ! swapon --show | grep -q '/swapfile'; then
  log "criando swap de ${SWAP_GB}G"
  fallocate -l "${SWAP_GB}G" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
else
  log "swap já existe"
fi

# --- k3s single-node (traefik ligado — é o ingress do piloto) ----------------
if ! command -v k3s >/dev/null 2>&1; then
  log "instalando k3s ${K3S_VERSION}"
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="${K3S_VERSION}" sh -s - \
    --write-kubeconfig-mode 644
else
  log "k3s já instalado: $(k3s --version | head -1)"
fi

# --- gVisor (runsc) + RuntimeClass — o isolamento que autoriza o piloto ------
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
  log "runsc já instalado"
fi

# k3s usa containerd embutido: template de config para registrar o handler runsc
K3S_CONTAINERD_TMPL=/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.tmpl
if [ ! -f "$K3S_CONTAINERD_TMPL" ]; then
  log "registrando handler runsc no containerd do k3s"
  mkdir -p "$(dirname "$K3S_CONTAINERD_TMPL")"
  cat > "$K3S_CONTAINERD_TMPL" <<'EOF'
{{ template "base" . }}

[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
EOF
  systemctl restart k3s
else
  log "handler runsc já registrado"
fi

log "aplicando RuntimeClass gvisor + namespace de sandboxes (PSA restricted)"
k3s kubectl apply -f "$(dirname "$0")/../k8s/sandbox-isolation.yaml"

# --- helm + sops + age --------------------------------------------------------
command -v helm >/dev/null 2>&1 || { log "instalando helm"; curl -sfL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash; }
# sops: o asset do release carrega a versão no nome — "latest/download" com
# nome sem versão dá 404 (achado do bootstrap real na VPS, 2026-07-23).
SOPS_VERSION="${SOPS_VERSION:-v3.11.0}"
command -v sops >/dev/null 2>&1 || { log "instalando sops ${SOPS_VERSION}"; curl -sfLo /usr/local/bin/sops "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.$( [ "$(uname -m)" = aarch64 ] && echo arm64 || echo amd64 )"; chmod +x /usr/local/bin/sops; }
command -v age >/dev/null 2>&1 || { log "instalando age"; apt-get update -qq && apt-get install -y -qq age; }

log "bootstrap concluído. Próximos passos: deploy/vps/README.md"

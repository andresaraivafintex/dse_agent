#!/usr/bin/env bash
# Plano 08 §D — kubeconfig para DENTRO da rede docker (dse_net).
#
# O kubeconfig do host aponta para 0.0.0.0:<porta-aleatória> (publicada pelo
# serverlb) — inútil de dentro de um container. Este script gera a variante
# interna: server https://k3d-<cluster>-serverlb:6443 (nome resolvível na
# dse_net; o cert do k3s inclui o serverlb no SAN). O worker do orchestrator
# monta o arquivo e é assim que o trigger_preview fala com o cluster.
#
# Credencial de DEV efêmera (cluster k3d local, recriável) — o arquivo é
# gitignored; re-rode após recriar o cluster. Produção usa o kubeconfig do
# cluster real do cliente (Vault/CSI), nunca este.
set -euo pipefail
CLUSTER_NAME="${DSE_K3D_CLUSTER:-dse-preview}"
OUT="$(cd "$(dirname "$0")" && pwd)/kubeconfig-internal.yaml"

k3d kubeconfig get "${CLUSTER_NAME}" \
  | sed -E "s|server: https://.*|server: https://k3d-${CLUSTER_NAME}-serverlb:6443|" \
  > "${OUT}"
chmod 600 "${OUT}"
echo "OK: ${OUT} (server interno https://k3d-${CLUSTER_NAME}-serverlb:6443)"

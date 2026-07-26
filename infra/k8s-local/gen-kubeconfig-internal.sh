#!/usr/bin/env bash
# Plan 08 §D — kubeconfig for use INSIDE the docker network (dse_net).
#
# The host kubeconfig points at 0.0.0.0:<random-port> (published by the
# serverlb) — useless from inside a container. This script generates the
# internal variant: server https://k3d-<cluster>-serverlb:6443 (a name
# resolvable on dse_net; the k3s cert includes the serverlb in its SAN). The
# orchestrator worker mounts the file, and that is how trigger_preview talks
# to the cluster.
#
# Ephemeral DEV credential (local k3d cluster, re-creatable) — the file is
# gitignored; re-run it after re-creating the cluster. Production uses the
# customer's real cluster kubeconfig (Vault/CSI), never this one.
set -euo pipefail
CLUSTER_NAME="${DSE_K3D_CLUSTER:-dse-preview}"
OUT="$(cd "$(dirname "$0")" && pwd)/kubeconfig-internal.yaml"

k3d kubeconfig get "${CLUSTER_NAME}" \
  | sed -E "s|server: https://.*|server: https://k3d-${CLUSTER_NAME}-serverlb:6443|" \
  > "${OUT}"
chmod 600 "${OUT}"
echo "OK: ${OUT} (server interno https://k3d-${CLUSTER_NAME}-serverlb:6443)"
